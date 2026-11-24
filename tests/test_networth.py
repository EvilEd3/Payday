"""Net worth now and month by month."""
from datetime import date

from app.db import Account, BalanceSnapshot, Loan, PropertyValue, Transaction
from app.networth import build, snapshot

TODAY = date(2026, 11, 24)


def _setup(s):
    s.add(Account(id="day", name="Everyday", type="CHECKING", balance_current=1000, balance_available=1000))
    s.add(Account(id="card", name="Visa", type="CREDITCARD", balance_current=-400))
    s.add(Account(id="ks", name="KiwiSaver", type="KIWISAVER", balance_current=20000, in_budget=False))
    s.add(Transaction(id="a", account_id="day", date=date(2026, 10, 20), description="PAY", amount=500))
    s.add(Transaction(id="b", account_id="day", date=date(2026, 11, 10), description="SHOP", amount=-300))
    s.commit()


def test_net_worth_now_and_worked_back(session):
    _setup(session)
    n = build(session, TODAY, 12)
    assert n["total"] == 20600 and n["assets_total"] == 21000 and n["debts_total"] == -400
    by_date = {h["date"]: h["total"] for h in n["history"]}
    assert by_date[date(2026, 10, 31)] == 20900  # before the $300 shop on 10 Nov
    assert by_date[date(2026, 9, 30)] == 20400  # before the $500 pay on 20 Oct
    assert date(2026, 8, 31) not in by_date  # nothing from before the first transaction
    assert n["since_month"] == -300


def test_a_saved_balance_beats_working_back(session):
    _setup(session)
    session.add(BalanceSnapshot(account_id="ks", date=date(2026, 10, 31), balance=19000))
    session.commit()
    by_date = {h["date"]: h["total"] for h in build(session, TODAY, 12)["history"]}
    assert by_date[date(2026, 10, 31)] == 19900


def test_home_and_mortgage_from_the_mortgage_page(session):
    _setup(session)
    session.add(Loan(id=1, name="Mortgage", original_amount=500000, start_date=date(2020, 1, 1)))
    session.add(PropertyValue(loan_id=1, date=date(2025, 1, 1), value=800000))
    session.commit()
    n = build(session, TODAY, 12)
    assert n["total"] == 20600 + 800000 - 500000
    assert {r["name"] for r in n["assets"]} >= {"Home"} and {r["name"] for r in n["debts"]} >= {"Mortgage"}


def test_snapshot_keeps_one_row_a_day(session):
    _setup(session)
    snapshot(session, TODAY)
    snapshot(session, TODAY)
    from sqlmodel import select
    assert len(session.exec(select(BalanceSnapshot)).all()) == 3
