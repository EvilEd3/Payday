from collections import defaultdict
from datetime import date, timedelta

from sqlmodel import Session, select

from .bills import match_pending, occurrences
from .db import Account, Bill, Category, PendingTransaction, Transaction, get_setting
from .insights import Ctx, category_limits
from .periods import get_cycle, pay_period
from .splits import expand


OWED_TYPES = ("CREDITCARD", "LOAN")


def bank_balance(acc: Account) -> float:
    """The bank's figure for an account, as money that is really yours.

    - Credit cards and loans: what you owe (negative). Their "available" amount is credit you could
      still borrow, not money you have.
    - Everything else: the available balance. Banks take pending card purchases off this straight away.
      If that includes an overdraft (it's higher than the current balance and the bank reports a
      limit), the overdraft is taken back out."""
    if acc.type in OWED_TYPES or acc.balance_available is None:
        return acc.balance_current
    if acc.balance_limit and acc.balance_available > acc.balance_current:
        return acc.balance_available - acc.balance_limit
    return acc.balance_available


def pending_included(acc: Account) -> bool:
    """Does the balance we use for this account already have pending purchases taken off?
    Yes for an available balance; no for a credit card's current balance or your own opening balance."""
    own = acc.opening_balance is not None and acc.opening_date is not None
    return not own and acc.type not in OWED_TYPES and acc.balance_available is not None


def account_balance(session: Session, acc: Account) -> float:
    """What's really in the account now, with pending purchases taken off.

    Your opening balance plus everything since, if you've set one; otherwise the bank's figure.
    Where that figure doesn't already include pending transactions, they're applied here."""
    if acc.opening_balance is not None and acc.opening_date is not None:
        moved = sum(t.amount for t in session.exec(
            select(Transaction).where(Transaction.account_id == acc.id,
                                      Transaction.date >= acc.opening_date)))
        bal = acc.opening_balance + moved
    else:
        bal = bank_balance(acc)
    if not pending_included(acc):
        bal += sum(p.amount for p in session.exec(
            select(PendingTransaction).where(PendingTransaction.account_id == acc.id)))
    return bal


def pending_spending(session: Session, acc_map: dict, start: date, end: date) -> list[PendingTransaction]:
    """Unsettled purchases that count as day-to-day spending: money out of a spending account that
    isn't a transfer and hasn't been matched to a bill."""
    billed = {p.id for p in match_pending(session).values()}
    out = []
    for p in session.exec(select(PendingTransaction).where(PendingTransaction.date >= start,
                                                           PendingTransaction.date < end)):
        acc = acc_map.get(p.account_id)
        if p.amount >= 0 or p.type == "TRANSFER" or (acc and not acc.count_spending) or p.id in billed:
            continue
        out.append(p)
    return out


def bill_accounts(session: Session, bills) -> dict[int, str]:
    """{bill id: the account it comes out of}: the one you chose, else the account its latest payment came from."""
    chosen = {b.id: b.account_id for b in bills if b.account_id}
    latest: dict[int, str] = {}
    for t in session.exec(select(Transaction).where(Transaction.bill_id != None)  # noqa: E711
                          .order_by(Transaction.date)):
        latest[t.bill_id] = t.account_id
    return {**latest, **chosen}


def account_forecast(session: Session, upcoming: list[dict], accounts: list[Account], balances: dict,
                     today: date) -> dict[str, dict]:
    """Each account's balance as the bills still to come before payday come out of it, before any day-to-day
    spending. {account id: {"low", "low_date", "end", "bills": [(due, bill)], "short": bool}}, only for
    accounts with a bill to come. Credit cards and loans are left out: a bill there adds to what's owed."""
    owners = bill_accounts(session, [u["bill"] for u in upcoming])
    by_acc: dict[str, list] = defaultdict(list)
    for u in upcoming:
        acc = owners.get(u["bill"].id)
        if acc:
            by_acc[acc].append((max(u["due"], today), u["bill"]))
    out = {}
    for a in accounts:
        if a.type in OWED_TYPES or a.id not in by_acc:
            continue
        bal = balances.get(a.id, 0.0)
        low, low_date = bal, today
        for due, bill in sorted(by_acc[a.id], key=lambda x: (x[0], x[1].id)):
            bal -= bill.amount
            if bal < low - 0.005:
                low, low_date = bal, due
        out[a.id] = {"low": low, "low_date": low_date, "end": bal, "short": low < -0.005,
                     "bills": sorted(by_acc[a.id], key=lambda x: (x[0], x[1].id))}
    return out


def counts_as_day_to_day(t: Transaction, kind: str, accounts: dict) -> bool:
    return kind == "expense" and t.bill_id is None and counts(t, accounts)


def counts(t, accounts: dict) -> bool:
    """Does this transaction's account count in the budget ("Spending counts" ticked)? Unknown accounts do."""
    acc = accounts.get(t.account_id)
    return acc is None or acc.count_spending


def build_dashboard(session: Session, today: date, offset: int = 0) -> dict:
    offset = min(offset, 0)
    is_past = offset < 0
    cycle = get_cycle(session)
    buffer = float(get_setting(session, "buffer") or 0)
    start, payday = pay_period(today, cycle, offset=offset)
    period_days = (payday - start).days
    days_left = 0 if is_past else max((payday - today).days, 1)  # includes today, excludes payday

    all_accounts = session.exec(select(Account).order_by(Account.in_budget.desc(), Account.name)).all()
    acc_map = {a.id: a for a in all_accounts}
    balances = {a.id: account_balance(session, a) for a in all_accounts}
    balance = sum(balances[a.id] for a in all_accounts if a.in_budget)

    cats = {c.id: c for c in session.exec(select(Category))}
    kind = lambda t: cats[t.category_id].kind if t.category_id in cats else "expense"  # noqa: E731
    txns = session.exec(select(Transaction).where(Transaction.date >= start,
                                                  Transaction.date < payday)).all()
    txns = [t for t in txns if counts(t, acc_map)]  # leave out accounts that aren't part of the budget
    txns = expand(session, txns)  # a split transaction counts each part in its own category
    income = sum(t.amount for t in txns if t.amount > 0 and kind(t) == "income")

    first = session.exec(select(Transaction).order_by(Transaction.date)).first()
    data_start = first.date if first else today  # don't flag bills from before your history begins
    paid_links = {(t.bill_id, t.bill_due_date): t for t in
                  session.exec(select(Transaction).where(Transaction.bill_id != None))}  # noqa: E711
    upcoming, paid = [], []
    pending_bills = match_pending(session)  # bill payments the bank hasn't settled yet
    for b in session.exec(select(Bill).where(Bill.active == True)):  # noqa: E712
        for d in occurrences(b, max(start, data_start), payday):
            t = paid_links.get((b.id, d))
            p = pending_bills.get((b.id, d))
            if t:
                paid.append({"bill": b, "due": d, "txn": t, "pending": False})
            elif p:
                paid.append({"bill": b, "due": d, "txn": p, "pending": True})
            else:
                upcoming.append({"bill": b, "due": d, "overdue": is_past or d < today})
    upcoming.sort(key=lambda x: x["due"])
    paid.sort(key=lambda x: x["due"])
    to_come = sum(u["bill"].amount for u in upcoming)
    bills_paid = sum(-p["txn"].amount for p in paid)
    bills_pending = sum(-p["txn"].amount for p in paid if p["pending"])

    # Pay-period budget: this pay's income minus every bill due this period.
    # Day-to-day = spending (net of refunds) not linked to a bill, from accounts set to count spending.
    bills_total = to_come + bills_paid
    from .goals import all_goals, budget_for_goals
    goals = all_goals(session, today, balances)
    goals_total = 0.0 if is_past else budget_for_goals(goals)  # set aside for goals, like a bill
    budget = income - bills_total - goals_total
    day_to_day = -sum(t.amount for t in txns if counts_as_day_to_day(t, kind(t), acc_map))
    pending = pending_spending(session, acc_map, start, payday) if not is_past else []
    pending_total = -sum(p.amount for p in pending)
    day_to_day += pending_total
    budget_left = budget - day_to_day
    planned_per_day = budget / period_days if period_days else 0
    days_gone = period_days - days_left
    free = balance - to_come - buffer

    runway = []
    for i in range(days_left):
        d = today + timedelta(days=i)
        runway.append({"date": d, "bills": [u for u in upcoming
                                             if u["due"] == d or (i == 0 and u["overdue"])]})

    by_cat = defaultdict(float)
    for t in txns:
        if kind(t) == "expense":
            by_cat[t.category_id] -= t.amount
    top = sorted(((cid, cats[cid].name if cid in cats else "Uncategorised", amt)
                  for cid, amt in by_cat.items() if amt > 0), key=lambda x: -x[2])

    return dict(
        calendar=cycle.calendar, frequency=cycle.frequency,
        today=today, start=start, payday=payday, offset=offset, is_past=is_past,
        days_left=days_left, period_days=period_days,
        balance=balance, balances=balances, buffer=buffer, income=income,
        to_come=to_come, bills_paid=bills_paid, bills_pending=bills_pending, free=free,
        per_day=free / days_left if days_left else 0,
        upcoming=upcoming, paid=paid, runway=runway, all_accounts=all_accounts,
        bills_total=bills_total, goals=goals, goals_total=goals_total, budget=budget, day_to_day=day_to_day, budget_left=budget_left,
        planned_per_day=planned_per_day,
        budget_per_day=budget_left / days_left if days_left else 0,
        avg_per_day=day_to_day / period_days if period_days else 0,
        pace=planned_per_day * days_gone - day_to_day,
        categories=top, cat_max=top[0][2] if top else 1, cat_total=sum(x[2] for x in top) or 1,
        limits=category_limits(Ctx(session, today), offset), pending_total=pending_total,
        forecast={} if is_past else account_forecast(session, upcoming, all_accounts, balances, today),
        pending_count=len(pending),
        last_sync=get_setting(session, "last_sync"),
        last_sync_status=get_setting(session, "last_sync_status"),
    )
