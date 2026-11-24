"""Each account's balance as the bills still to come before payday come out of it."""
from datetime import date

from app.db import Account, Bill, Transaction
from app.forecast import build_dashboard
from app.notify import find

TODAY = date(2026, 11, 24)  # pay period 15 Nov to 14 Dec


def _setup(s, bills_balance):
    s.add(Account(id="bills", name="Bills", type="CHECKING", balance_current=bills_balance,
                  balance_available=bills_balance))
    s.add(Account(id="card", name="Visa", type="CREDITCARD", balance_current=-300))
    s.add(Bill(id=1, name="Power", amount=210, anchor_date=date(2026, 10, 3), match_pattern="MERCURY"))
    s.add(Bill(id=2, name="Broadband", amount=95, anchor_date=date(2026, 10, 8), match_pattern="ONE NZ"))
    s.add(Bill(id=3, name="Netflix", amount=20, anchor_date=date(2026, 10, 12), match_pattern="NETFLIX",
               account_id="card"))  # chosen by hand; a card, so not forecast
    s.add(Bill(id=4, name="Gym", amount=40, anchor_date=date(2026, 10, 1), match_pattern="GYM"))  # never paid yet
    # last month's payments show which account each bill comes out of
    s.add(Transaction(id="p", account_id="bills", date=date(2026, 11, 3), description="MERCURY", amount=-205,
                      bill_id=1, bill_due_date=date(2026, 11, 3)))
    s.add(Transaction(id="b", account_id="bills", date=date(2026, 11, 9), description="ONE NZ", amount=-95,
                      bill_id=2, bill_due_date=date(2026, 11, 9)))
    s.commit()


def test_forecast_follows_bills_out_of_their_account(session):
    _setup(session, 1000)
    f = build_dashboard(session, TODAY)["forecast"]
    assert set(f) == {"bills"}  # the card and the bill with no account yet are left out
    assert [b.name for _, b in f["bills"]["bills"]] == ["Power", "Broadband"]
    assert round(f["bills"]["end"], 2) == 695 and not f["bills"]["short"]


def test_short_account_is_flagged_with_the_date(session):
    _setup(session, 250)
    f = build_dashboard(session, TODAY)["forecast"]["bills"]
    assert f["short"] and round(f["low"], 2) == -55 and f["low_date"] == date(2026, 12, 8)
    from datetime import datetime
    alerts = [a for a in find(session, TODAY, datetime(2026, 11, 24, 9)) if a["kind"] == "short"]
    assert alerts and alerts[0]["title"] == "Your Bills account will be $55 short"


def test_chosen_account_wins_over_history(session):
    _setup(session, 1000)
    session.add(Account(id="everyday", name="Everyday", type="CHECKING", balance_current=100, balance_available=100))
    bill = session.get(Bill, 1)
    bill.account_id = "everyday"
    session.commit()
    f = build_dashboard(session, TODAY)["forecast"]
    assert f["everyday"]["short"] and round(f["everyday"]["low"], 2) == -110
