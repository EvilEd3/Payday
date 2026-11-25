"""The activity log: a full record of what happened in a household, newest first.

Every money movement the bank sends, where it was allocated (a category, a bill, a transfer, a split), each
notification Payday sent and to whom, each sync, and the changes people make by hand. Tapping a notification
opens its entry here, so the whole message and the page it's about are one tap away.

An admin chooses what's recorded and how long it's kept (Settings, Activity log). Entries are written in the same
database session as the change they describe, so they're saved, or not, together with it.
"""
import json
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, delete, select

from .db import Account, Activity, Bill, Category, Transaction, get_setting, set_setting

KINDS = [("money", "Money in and out"), ("allocation", "Allocations"), ("notification", "Notifications"),
         ("sync", "Bank syncs"), ("change", "Changes people made")]
KIND_NAMES = dict(KINDS)
KEEP_CHOICES = [(30, "30 days"), (90, "3 months"), (365, "1 year"), (730, "2 years"), (0, "Forever")]
DEFAULT_KEEP = 365
FIRST_IMPORT = 40  # a sync bringing more new transactions than this into an empty household is logged as one entry

SOURCES = {  # how a category was chosen, in words
    "bill": "matched to a bill", "transfer": "a transfer between your accounts", "user": "one of your rules",
    "learned": "learned from you", "akahu": "the bank's category", "refund": "a refund from a payee you spend with",
    "default": "Payday's default", "manual": "set by hand",
}


def off_kinds(s: Session) -> set[str]:
    try:
        return set(json.loads(get_setting(s, "activity_off") or "[]"))
    except ValueError:
        return set()


def keep_days(s: Session) -> int:
    try:
        return max(0, int(get_setting(s, "activity_keep_days") or DEFAULT_KEEP))
    except ValueError:
        return DEFAULT_KEEP


def save_settings(s: Session, on: list[str], keep: int) -> None:
    set_setting(s, "activity_off", json.dumps(sorted(k for k in KIND_NAMES if k not in on)))
    set_setting(s, "activity_keep_days", str(keep if keep in dict(KEEP_CHOICES) else DEFAULT_KEEP))


def actor() -> str:
    """Who is making this change: the signed-in person, or Payday itself (a sync, the hourly check)."""
    from .auth import current_user  # local import: auth imports this package's db too
    u = current_user.get()
    return u.username if u else "Payday"


def record(s: Session, kind: str, title: str, body: str | list = "", *, amount: float | None = None,
           who: str | None = None, url: str = "", ref: str = "", data: dict | None = None,
           commit: bool = False, force: bool = False) -> Activity | None:
    """Add one entry, unless an admin has switched this kind off. The caller's commit saves it."""
    if not force and kind in off_kinds(s):
        return None
    if isinstance(body, list):
        body = "\n".join(line for line in body if line)
    row = Activity(at=datetime.now(timezone.utc), kind=kind, title=title[:300], body=body[:20000], amount=amount,
                   who=who if who is not None else actor(), url=url[:500], ref=ref[:200],
                   data=json.dumps(data, default=str) if data else "")
    s.add(row)
    if commit:
        s.commit()
        s.refresh(row)
    else:
        s.flush()
    return row


def prune(s: Session) -> int:
    days = keep_days(s)
    if not days:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    n = s.exec(delete(Activity).where(Activity.at < cutoff)).rowcount or 0
    s.commit()
    return n


def money(v: float) -> str:
    return f"{'−' if v < 0 else ''}${abs(v):,.2f}"


class Names:
    """Account, category and bill names, loaded once for a batch of entries."""

    def __init__(self, s: Session):
        self.accounts = {a.id: a.name for a in s.exec(select(Account))}
        self.cats = {c.id: c.name for c in s.exec(select(Category))}
        self.bills = {b.id: b.name for b in s.exec(select(Bill))}

    def account(self, acc_id) -> str:
        return self.accounts.get(acc_id, "an account")

    def category(self, cat_id) -> str:
        return self.cats.get(cat_id, "no category")

    def bill(self, bill_id) -> str:
        return self.bills.get(bill_id, "a bill")


def payee(t: Transaction) -> str:
    return (t.merchant or t.description or "").strip()


def allocation(t: Transaction, names: Names) -> str:
    """Where a transaction's money is allocated, in one line."""
    if t.bill_id:
        due = f", due {t.bill_due_date.strftime('%a %-d %b')}" if t.bill_due_date else ""
        return f"Allocated to the bill {names.bill(t.bill_id)}{due} ({names.category(t.category_id)})"
    if t.transfer_id:
        return f"A transfer between your accounts ({names.category(t.category_id)})"
    how = SOURCES.get(t.category_source, "")
    return f"Allocated to {names.category(t.category_id)}" + (f", {how}" if how else "")


def txn_lines(t: Transaction, names: Names) -> list[str]:
    lines = [f"{'Paid out' if t.amount < 0 else 'Received'}: {money(t.amount)}",
             f"Account: {names.account(t.account_id)}",
             f"Date: {t.date.strftime('%A %-d %B %Y')}",
             f"Bank description: {t.description}"]
    if t.merchant and t.merchant != t.description:
        lines.append(f"Merchant: {t.merchant}")
    if t.akahu_type:
        lines.append(f"Type: {t.akahu_type.replace('_', ' ').lower()}")
    lines.append(allocation(t, names))
    return lines


def txn_url(t: Transaction) -> str:
    from urllib.parse import quote
    return f"/transactions?search={quote(payee(t)[:40])}"


def new_transaction(s: Session, t: Transaction, names: Names) -> None:
    word = "from" if t.amount > 0 else "at"
    record(s, "money", f"{money(t.amount)} {word} {payee(t)}", txn_lines(t, names), amount=t.amount,
           who="Payday", url=txn_url(t), ref=t.id)


def snapshot(txns) -> dict:
    return {t.id: (t.category_id, t.bill_id, t.bill_due_date, t.transfer_id) for t in txns}


def resorted(s: Session, before: dict, txns, reason: str = "") -> None:
    """After Payday re-sorts transactions (a rule or bill changed), log each one that moved, in one entry."""
    names = Names(s)
    moved = []
    for t in txns:
        old = before.get(t.id)
        if old is None or old == (t.category_id, t.bill_id, t.bill_due_date, t.transfer_id):
            continue
        was = (f"the bill {names.bill(old[1])}" if old[1] else names.category(old[0]))
        moved.append(f"{t.date.strftime('%-d %b')} {money(t.amount)} {payee(t)}: from {was} to "
                     + allocation(t, names).removeprefix("Allocated to ").replace("A transfer", "a transfer"))
    if not moved:
        return
    record(s, "allocation", f"{len(moved)} transaction{'s' if len(moved) != 1 else ''} re-allocated",
           ([reason] if reason else []) + moved[:500] + ([f"…and {len(moved) - 500} more"] if len(moved) > 500 else []),
           url="/transactions")


def label_for(url: str) -> str:
    """The button that takes you from an entry to the page it's about."""
    path = url.split("?")[0].split("#")[0]
    for prefix, label in (("/bills/", "Open the bill"), ("/bills", "Open bills"), ("/accounts/", "Open the account"),
                          ("/transactions", "See the transactions"), ("/summary", "Open the summary"),
                          ("/goals", "Open goals"), ("/review", "Check the transactions"),
                          ("/settings", "Open settings"), ("/loans", "Open the loan"), ("/activity", "See the log")):
        if path.startswith(prefix):
            return label
    return "Open the overview" if path == "/" else "Open"
