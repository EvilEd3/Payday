"""Loans: weekly and fortnightly repayments, a car's resale value in net worth, and repayments set aside as a bill."""
from datetime import date, timedelta

import pytest
from sqlmodel import Session, select

from app import mortgage
from app.db import Account, Bill, Loan, LoanRate, PropertyValue, Transaction
from app.networth import build

TODAY = date(2026, 11, 24)
HEADERS = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}


def test_fortnightly_repayments_step_from_any_known_date():
    dates = mortgage.repayment_dates(date(2026, 11, 24), "fortnightly", anchor=date(2026, 1, 1))
    first = next(dates)
    assert first == date(2026, 12, 3) and next(dates) == date(2026, 12, 17)  # 1 Jan + 24 and 25 fortnights
    weekly = mortgage.repayment_dates(date(2026, 11, 20), "weekly", anchor=date(2026, 12, 8))
    assert next(weekly) == date(2026, 11, 24)  # dates before the anchor work too
    assert next(mortgage.repayment_dates(date(2026, 11, 24), "monthly", 20)) == date(2026, 12, 20)


def test_a_fortnightly_car_loan_forecast(session):
    session.add(Loan(id=1, name="Car loan", kind="car", frequency="fortnightly", repay_date=date(2026, 11, 19),
                     original_amount=18400, start_date=TODAY, term_months=48, repayment=490))
    session.add(LoanRate(loan_id=1, effective=TODAY, rate=12.9))
    session.commit()
    m = mortgage.summary(session, session.get(Loan, 1), TODAY)
    rows = m["base"].rows
    assert rows[0].date == date(2026, 12, 3) and (rows[1].date - rows[0].date).days == 14
    assert 40 <= len(rows) <= 44 and m["base"].payoff == rows[-1].date
    assert 1900 < m["base"].interest < 2100  # roughly 18,400 at 12.9% over 21 months
    assert m["per"] == "fortnight" and m["schedule"] == "Repaid every fortnight"
    extra = mortgage.summary(session, session.get(Loan, 1), TODAY, mortgage.WhatIf(extra=100))
    assert extra["alt"].payoff < m["base"].payoff and extra["alt"].interest < m["base"].interest


def test_a_car_loses_value_each_year():
    loan = Loan(kind="car", depreciation=15, start_date=date(2025, 11, 24), original_amount=30000, deposit=5000)
    pts = mortgage.asset_points(loan, [])
    assert pts == [(date(2025, 11, 24), 35000)]  # what you paid, when the loan started
    assert round(mortgage.value_on(loan, pts, date(2026, 11, 24))) == round(35000 * 0.85 ** (365 / 365.25))
    assert mortgage.value_on(loan, pts, date(2025, 1, 1)) is None  # not yours yet
    later = mortgage.asset_points(loan, [PropertyValue(loan_id=1, date=date(2026, 6, 1), value=26000)])
    assert later == [(date(2026, 6, 1), 26000)]  # your own estimate replaces the price paid
    assert mortgage.asset_points(Loan(kind="personal", original_amount=5000, start_date=TODAY), []) == []


def test_net_worth_counts_the_car_and_the_linked_loan_once(session):
    session.add(Account(id="day", name="Everyday", type="CHECKING", balance_current=1000, balance_available=1000))
    session.add(Account(id="car", name="Car loan", type="LOAN", balance_current=-18400, in_budget=False,
                        count_spending=False))
    session.add(Loan(id=1, name="Car loan", kind="car", account_id="car", depreciation=15,
                     original_amount=26000, deposit=4000, start_date=date(2024, 3, 1)))
    session.add(PropertyValue(loan_id=1, date=TODAY, value=24100))
    session.commit()
    n = build(session, TODAY, 12)
    assert n["total"] == 1000 - 18400 + 24100
    car = next(r for r in n["assets"] if r["name"] == "Car")
    assert car["amount"] == 24100 and car["href"] == "/loans/1"
    assert [r["name"] for r in n["debts"]] == ["Car loan"]  # the bank account, not the loan a second time


@pytest.fixture
def tidy():
    """Take the accounts, transactions and loans these tests add back out, so other tests see a new household."""
    yield
    from app.db import LoanEntry, use_profile
    with use_profile(1) as eng, Session(eng) as s:
        for model in (Account, Transaction, Bill, Loan, LoanRate, LoanEntry, PropertyValue):
            for row in s.exec(select(model)):
                s.delete(row)
        s.commit()


def _client_with_car_loan(client, username):
    from app.db import use_profile
    from test_security import _signed_in, _user
    user = _user(client, username)
    _signed_in(client, user)
    pid = user.last_profile_id or 1
    today = date.today()
    with use_profile(pid) as eng, Session(eng) as s:
        for model in (Bill, Loan, LoanRate, PropertyValue):
            for row in s.exec(select(model)):
                s.delete(row)
        s.merge(Account(id="lday", name="Everyday", type="CHECKING", balance_current=3000, balance_available=3000))
        s.merge(Account(id="lcar", name="Car loan", type="LOAN", balance_current=-18400))
        for i in range(3):
            d = today - timedelta(days=14 * i + 5)
            s.merge(Transaction(id=f"out{i}", account_id="lday", date=d, description="MTF CAR LOAN", amount=-490,
                              transfer_id=f"in{i}"))
            s.merge(Transaction(id=f"in{i}", account_id="lcar", date=d, description="REPAYMENT", amount=490,
                              transfer_id=f"out{i}"))
        s.commit()
    return pid


def test_a_car_loan_set_up_end_to_end(client, tidy):
    from app.db import use_profile
    pid = _client_with_car_loan(client, "carowner")
    r = client.post("/loans/new", headers=HEADERS, follow_redirects=False, data={
        "kind": "car", "name": "Car loan", "account_id": "lcar", "pay_account_id": "lday", "original_amount": "26000",
        "deposit": "4000", "start_date": "2024-03-01", "term": "60", "term_unit": "months", "rate": "12.9",
        "frequency": "fortnightly", "repayment": "490", "depreciation": "15", "count_asset": "true",
        "leave_out": "true"})
    assert r.status_code == 303 and r.headers["location"].startswith("/loans/")
    loan_url = r.headers["location"]
    with use_profile(pid) as eng, Session(eng) as s:
        loan = s.exec(select(Loan)).one()
        assert (loan.kind, loan.frequency, loan.term_months, loan.repayment) == ("car", "fortnightly", 60, 490)
        assert s.exec(select(LoanRate)).one().rate == 12.9
        assert not s.get(Account, "lcar").in_budget and s.get(Account, "lday").in_budget  # everyday stays in
    page = client.get(loan_url).text
    assert "Paid off" in page and "Extra each fortnight" in page and "Resale estimate" in page
    assert "Add as a bill" in page  # no bill yet, and a repayment to copy
    assert client.get("/loans").status_code == 200 and "Car loan" in client.get("/loans").text
    assert "Car" in client.get("/networth").text

    r = client.post(f"{loan_url}/bill", headers=HEADERS, follow_redirects=False, data={})
    assert "bill=added" in r.headers["location"]
    with use_profile(pid) as eng, Session(eng) as s:
        bill = s.exec(select(Bill)).one()
        assert (bill.amount, bill.frequency, bill.match_pattern, bill.account_id) == (490, "fortnightly", "MTF CAR LOAN", "lday")
        assert s.get(Transaction, "out0").bill_id == bill.id  # past repayments now count as paying it
    client.post(f"{loan_url}/bill", headers=HEADERS, data={})  # asking again doesn't make a second one
    with use_profile(pid) as eng, Session(eng) as s:
        assert len(s.exec(select(Bill)).all()) == 1
    assert "Repayments are matched to your bill" in client.get(loan_url).text


def test_an_existing_bill_is_found_not_duplicated(client, tidy):
    from app.db import use_profile
    from app.main import recategorise_all
    pid = _client_with_car_loan(client, "hasabill")
    with use_profile(pid) as eng, Session(eng) as s:
        s.add(Bill(name="Car repayment", amount=490, frequency="fortnightly", anchor_date=date.today(),
                   match_pattern="MTF CAR"))
        s.add(Loan(id=7, name="Car loan", kind="car", account_id="lcar", frequency="fortnightly", repayment=490))
        s.commit()
        recategorise_all(s)
    page = client.get("/loans/7").text
    assert "already matches these repayments" in page and "Add as a bill" not in page
    client.post("/loans/7/bill", headers=HEADERS, data={})
    with use_profile(pid) as eng, Session(eng) as s:
        assert len(s.exec(select(Bill)).all()) == 1
        bill_id = s.exec(select(Bill)).one().id
    client.post("/loans/7/bill", headers=HEADERS, data={"bill_id": str(bill_id)})
    with use_profile(pid) as eng, Session(eng) as s:
        assert s.get(Loan, 7).bill_id == bill_id
    assert "Repayments are matched to your bill" in client.get("/loans/7").text


def test_old_mortgage_links_still_work(client):
    from test_security import _signed_in, _user
    _signed_in(client, _user(client, "carowner"))
    assert client.get("/mortgage/setup", follow_redirects=False).headers["location"] == "/loans/new?kind=home"


def test_a_mortgage_still_shows_equity(client, tidy):
    from app.db import use_profile
    from test_security import _signed_in, _user
    _signed_in(client, _user(client, "homeowner"))
    with use_profile(1) as eng, Session(eng) as s:
        s.add(Loan(id=3, name="Mortgage", original_amount=500000, start_date=date(2020, 1, 1), payment_day=20))
        s.add(LoanRate(loan_id=3, effective=date(2020, 1, 1), rate=6.0))
        s.add(PropertyValue(loan_id=3, date=date(2025, 1, 1), value=800000))
        s.commit()
    page = client.get("/loans/3").text
    assert "Mortgage-free" in page and "Loan to value" in page and "Repaid on the 20th of each month" in page
    assert client.get("/mortgage", follow_redirects=False).headers["location"] == "/loans/3"
    assert "Rate after refixing" in page
