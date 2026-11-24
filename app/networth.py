"""Net worth: everything you have less everything you owe, now and month by month.

Account balances in the past are worked back from today's balance and the transactions since. Where a sync has
saved a balance for an account (see snapshot()), that is used instead: it also catches changes that never show
as transactions, like a KiwiSaver fund's value moving."""
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta
from sqlmodel import Session, select

from .db import Account, BalanceSnapshot, Loan, LoanEntry, PropertyValue, Transaction
from .forecast import OWED_TYPES, account_balance, bank_balance


def snapshot(s: Session, today: date) -> None:
    """Save today's balance for every account (one row per account per day, replaced by later syncs)."""
    for acc in s.exec(select(Account)):
        row = s.exec(select(BalanceSnapshot).where(BalanceSnapshot.account_id == acc.id,
                                                   BalanceSnapshot.date == today)).first()
        row = row or BalanceSnapshot(account_id=acc.id, date=today, balance=0)
        row.balance = bank_balance(acc)
        s.add(row)
    s.commit()


def _month_ends(today: date, months: int) -> list[date]:
    first = today.replace(day=1)
    ends = [first - timedelta(days=1)]
    for _ in range(months - 1):
        ends.append(ends[-1].replace(day=1) - timedelta(days=1))
    return sorted(ends) + [today]


def build(s: Session, today: date, months: int = 12) -> dict:
    accounts = s.exec(select(Account).order_by(Account.name)).all()
    now = {a.id: account_balance(s, a) for a in accounts}
    first = s.exec(select(Transaction).order_by(Transaction.date)).first()
    data_start = first.date if first else today
    ends = _month_ends(today, months)
    # from the last month end before your history starts (working back from it is exact), to today
    before = [d for d in ends if d < data_start]
    points = before[-1:] + [d for d in ends if d >= data_start]

    # money moved after each date, per account, to work balances back from today's
    moved: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for t in s.exec(select(Transaction).where(Transaction.date > points[0]).order_by(Transaction.date)):
        moved[t.account_id].append((t.date, t.amount))
    snaps: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for r in s.exec(select(BalanceSnapshot).order_by(BalanceSnapshot.date)):
        snaps[r.account_id].append((r.date, r.balance))

    def balance_on(acc: Account, d: date) -> float:
        if d >= today:
            return now[acc.id]
        own = snaps.get(acc.id, [])
        i = bisect_right([x[0] for x in own], d)
        if i:
            return own[i - 1][1]
        return now[acc.id] - sum(a for when, a in moved.get(acc.id, []) if when > d)

    # a home and a mortgage tracked on the Mortgage page, if there is one
    loan = s.exec(select(Loan).order_by(Loan.id)).first()
    values = sorted((v.date, v.value) for v in s.exec(select(PropertyValue))) if loan else []
    entries = sorted((e.date, e.balance) for e in s.exec(select(LoanEntry))) if loan else []
    loan_in_accounts = bool(loan and loan.account_id and any(a.id == loan.account_id for a in accounts))

    def home_on(d: date) -> float | None:
        i = bisect_right([v[0] for v in values], d)
        return values[i - 1][1] if i else None

    def mortgage_on(d: date) -> float | None:
        if not loan or loan_in_accounts:
            return None
        i = bisect_right([e[0] for e in entries], d)
        if i:
            return entries[i - 1][1]
        return loan.original_amount if loan.start_date and loan.start_date <= d else None

    def total_on(d: date) -> float:
        total = sum(balance_on(a, d) for a in accounts)
        return total + (home_on(d) or 0) - (mortgage_on(d) or 0)

    assets, debts = [], []
    for a in accounts:
        row = {"name": a.name, "detail": a.institution, "amount": now[a.id], "href": f"/accounts/{a.id}"}
        (debts if a.type in OWED_TYPES or now[a.id] < 0 else assets).append(row)
    if home_on(today) is not None:
        assets.append({"name": "Home", "detail": "Latest value on the Mortgage page", "amount": home_on(today),
                       "href": "/mortgage"})
    if mortgage_on(today) is not None:
        debts.append({"name": loan.name or "Mortgage", "detail": "From the Mortgage page",
                      "amount": -mortgage_on(today), "href": "/mortgage"})
    assets.sort(key=lambda r: -r["amount"])
    debts.sort(key=lambda r: r["amount"])
    history = [{"date": d, "total": total_on(d)} for d in points]
    total = history[-1]["total"]
    year_ago = today - relativedelta(years=1)
    base = next((h for h in history if h["date"] >= year_ago - timedelta(days=1)), history[0])
    month = history[-2] if len(history) > 1 else None
    return {
        "total": total, "assets": assets, "debts": debts,
        "assets_total": sum(r["amount"] for r in assets), "debts_total": sum(r["amount"] for r in debts),
        "history": history,
        "since_month": total - month["total"] if month else None, "month_date": month["date"] if month else None,
        "since_start": total - base["total"] if base is not history[-1] else None, "start_date": base["date"],
    }


def line_chart(history: list[dict], height: int = 180) -> dict:
    """Points for an SVG line: x 0..100, y 0..100 from the top, with the low and high values for labels."""
    vals = [h["total"] for h in history]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.1 or abs(hi) * 0.05 or 1
    lo, hi = lo - pad, hi + pad
    n = max(len(vals) - 1, 1)
    pts = [(i / n * 100, (hi - v) / (hi - lo) * 100) for i, v in enumerate(vals)]
    return {"points": " ".join(f"{x:.2f},{y:.2f}" for x, y in pts), "dots": pts, "low": lo + pad, "high": hi - pad,
            "zero": (hi / (hi - lo) * 100) if lo < 0 < hi else None, "height": height}
