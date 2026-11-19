import re
from datetime import date, timedelta
from typing import Optional

from dateutil.relativedelta import relativedelta
from sqlmodel import Session, select

from .db import Bill, Transaction

APPROX_DAYS = {"weekly": 7, "fortnightly": 14, "monthly": 30, "quarterly": 91, "yearly": 365}
MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12}


def matches_pattern(pattern: str, text: str) -> bool:
    if not pattern:
        return False
    if pattern.startswith("re:"):
        try:
            return re.search(pattern[3:], text, re.I) is not None
        except re.error:
            return False
    return pattern.lower() in text.lower()


def valid_pattern(pattern: str) -> bool:
    """Non-empty, and if it's a regular expression (re:...) it must compile."""
    pattern = pattern.strip()
    if not pattern or pattern == "re:":
        return False
    if pattern.startswith("re:"):
        try:
            re.compile(pattern[3:])
        except re.error:
            return False
    return True


def _scheduled(bill: Bill, n: int) -> date:
    """The n-th date on the bill's calendar, before any weekend adjustment."""
    a = bill.anchor_date
    if bill.frequency == "weekly":
        return a + timedelta(weeks=n)
    if bill.frequency == "fortnightly":
        return a + timedelta(weeks=2 * n)
    # computed from the anchor each time so month-ends don't drift (31st stays 31st)
    return a + relativedelta(months=MONTHS[bill.frequency] * n)


def next_business_day(d: date) -> date:
    """Saturday and Sunday move to the Monday after."""
    return d + timedelta(days=(7 - d.weekday()) % 7) if d.weekday() >= 5 else d


def nth_due(bill: Bill, n: int) -> date:
    """The n-th due date. A bill that falls on a weekend is due the Monday after."""
    return next_business_day(_scheduled(bill, n))


def occurrences(bill: Bill, start: date, end: date) -> list[date]:
    """Due dates in [start, end)."""
    if bill.frequency not in APPROX_DAYS or end <= start:
        return []
    n = (start - bill.anchor_date).days // APPROX_DAYS[bill.frequency]
    while nth_due(bill, n) >= start:  # the estimate can land late (months aren't all 30 days): walk back
        n -= 1
    out = []
    while True:
        n += 1
        d = nth_due(bill, n)
        if d >= end:
            return out
        if d >= start:
            out.append(d)


def bill_matches(bill: Bill, text: str, paid_amount: float) -> bool:
    """Does this payment look like this bill (text and amount)? Timing is checked separately."""
    return (matches_pattern(bill.match_pattern, text)
            and abs(paid_amount - bill.amount) <= bill.amount * bill.tolerance_pct / 100 + 0.005)


def closest_free_due(bill: Bill, on: date, taken: set, window_days: int | None = None) -> Optional[date]:
    """The due date nearest `on` (within the bill's window) that no other payment has claimed."""
    w = timedelta(days=bill.window_days if window_days is None else window_days)
    free = [d for d in occurrences(bill, on - w, on + w + timedelta(days=1)) if d not in taken]
    return min(free, key=lambda d: abs((d - on).days), default=None)


def match_pending(session: Session, bills: list[Bill] | None = None) -> dict:
    """Pending payments that look like a bill: same text and amount rules as settled ones, landing within
    the bill's window of a due date nobody has paid yet. Returns {(bill id, due date): pending transaction}.

    Pending items are replaced on every sync, so this is worked out fresh each time rather than stored."""
    from .db import PendingTransaction  # local import keeps this module's imports small
    bills = bills if bills is not None else session.exec(select(Bill).where(Bill.active == True)  # noqa: E712
                                                         .order_by(Bill.id)).all()
    taken = {(t.bill_id, t.bill_due_date) for t in
             session.exec(select(Transaction).where(Transaction.bill_id != None))}  # noqa: E711
    out: dict = {}
    for p in session.exec(select(PendingTransaction).where(PendingTransaction.amount < 0)
                          .order_by(PendingTransaction.date, PendingTransaction.id)):
        for bill in bills:
            if not bill.active or not bill_matches(bill, p.description, -p.amount):
                continue
            used = {d for (bid, d) in taken | set(out) if bid == bill.id}
            due = closest_free_due(bill, p.date, used)
            if due:
                out[(bill.id, due)] = p
                break
    return out


def _clean_pattern(txn: Transaction) -> str:
    if txn.merchant:
        return txn.merchant
    words = [w for w in re.split(r"\s+", txn.description) if w and not any(ch.isdigit() for ch in w)]
    return " ".join(words[:3]) or txn.description


def suggest_bill(session: Session, txn: Transaction) -> dict:
    """Pre-fill a bill from a transaction, guessing how often it repeats from past payments."""
    pattern = _clean_pattern(txn)
    history = sorted(
        {t.date for t in session.exec(select(Transaction).where(Transaction.amount < 0))
         if matches_pattern(pattern, f"{t.description} {t.merchant}")})
    frequency = "monthly"
    if len(history) >= 2:
        gaps = sorted((b - a).days for a, b in zip(history, history[1:]))
        gap = gaps[len(gaps) // 2]
        frequency = ("weekly" if gap <= 9 else "fortnightly" if gap <= 18 else "monthly" if gap <= 45
                     else "quarterly" if gap <= 120 else "yearly")
    return {"name": pattern.title(), "amount": f"{-txn.amount:.2f}", "frequency": frequency,
            "anchor_date": txn.date.isoformat(), "match_pattern": pattern,
            "category_id": txn.category_id, "seen": len(history)}


def nearest_unpaid_due(session: Session, bill: Bill, txn: Transaction) -> date:
    """For a link you make by hand: the closest due date not already paid, looking a whole cycle either side."""
    span = APPROX_DAYS[bill.frequency]
    taken = {t.bill_due_date for t in session.exec(select(Transaction).where(Transaction.bill_id == bill.id))
             if t.id != txn.id}
    return (closest_free_due(bill, txn.date, taken, span)
            or closest_free_due(bill, txn.date, set(), span)  # every nearby date is taken: share the closest
            or txn.date)
