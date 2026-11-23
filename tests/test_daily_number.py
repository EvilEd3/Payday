"""The figures behind "a day until payday": refunds, bills against category limits."""
from datetime import date

from sqlmodel import select

from app.categorise import recategorise_all
from app.db import Account, Bill, Category, Transaction
from app.forecast import build_dashboard
from app.insights import Ctx, category_limits, usual_spend

TODAY = date(2026, 11, 24)  # pay period 15 Nov to 14 Dec


def _cat(s, name, kind="expense"):
    c = s.exec(select(Category).where(Category.name == name)).first()
    if not c:
        c = Category(name=name, kind=kind)
        s.add(c)
        s.flush()
    return c


def _txn(s, id, d, desc, amount, group="", merchant=""):
    s.add(Transaction(id=id, account_id="acc", date=d, description=desc, merchant=merchant, amount=amount,
                      akahu_group=group))


def _setup(s):
    s.add(Account(id="acc", name="Everyday", type="CHECKING", balance_current=3000, balance_available=3000))
    _txn(s, "pay", date(2026, 11, 15), "ACME LTD SALARY", 5000)
    _txn(s, "buy1", date(2026, 10, 20), "KMART", -80, "Household", "Kmart")
    _txn(s, "buy2", date(2026, 11, 16), "KMART", -200, "Household", "Kmart")
    _txn(s, "back", date(2026, 11, 18), "KMART", 200, "", "Kmart")  # the return
    _txn(s, "gift", date(2026, 11, 19), "TFR FROM NANA", 50)
    s.commit()
    recategorise_all(s)


def test_refund_goes_back_to_the_payees_category(session):
    _setup(session)
    back, pay, gift = (session.get(Transaction, i) for i in ("back", "pay", "gift"))
    household, income = _cat(session, "Household"), _cat(session, "Income", "income")
    assert (back.category_id, back.category_source) == (household.id, "refund")
    assert pay.category_id == income.id  # salary is still income
    assert gift.category_id == income.id  # money in from someone you don't spend with is still income


def test_refund_reduces_spending_not_adds_income(session):
    _setup(session)
    d = build_dashboard(session, TODAY)
    assert d["income"] == 5050  # salary + gift, not the $200 return
    assert d["day_to_day"] == 0  # $200 bought, $200 returned


def test_refund_is_flagged_for_review(session):
    from app.insights import needs_review_query
    _setup(session)
    assert "back" in {t.id for t in session.exec(needs_review_query())}


def test_your_rule_still_wins_over_refund_guess(session):
    from app.db import Rule
    _setup(session)
    gifts = _cat(session, "Gifts")
    session.add(Rule(pattern="KMART", category_id=gifts.id, direction="in"))
    session.commit()
    recategorise_all(session)
    assert session.get(Transaction, "back").category_id == gifts.id


def test_quarterly_bill_is_expected_not_overspending(session):
    session.add(Account(id="acc", name="Everyday", type="CHECKING", balance_current=3000))
    housing = _cat(session, "Housing")
    session.add(Bill(name="Rates", amount=900, frequency="quarterly", anchor_date=date(2026, 8, 20),
                     match_pattern="COUNCIL RATES", category_id=housing.id))
    session.add(Bill(name="Mortgage", amount=2000, frequency="monthly", anchor_date=date(2026, 8, 20),
                     match_pattern="HOME LOAN", category_id=housing.id))
    for m in (8, 9, 10, 11):
        _txn(session, f"loan{m}", date(2026, m, 20), "HOME LOAN", -2000)
        _txn(session, f"fix{m}", date(2026, m, 25), "HARDWARE", -30, "Housing")
    _txn(session, "rates8", date(2026, 8, 20), "COUNCIL RATES", -900)
    _txn(session, "rates11", date(2026, 11, 20), "COUNCIL RATES", -900)
    session.commit()
    recategorise_all(session)

    ctx = Ctx(session, TODAY)
    # This pay: mortgage + rates due, plus the usual $30 of day-to-day Housing spending
    assert round(usual_spend(ctx)[housing.id], 2) == 2930
    limit = category_limits(ctx)[housing.id]["limit"]
    d = build_dashboard(session, TODAY)
    spent = dict((cid, amt) for cid, _, amt in d["categories"])[housing.id]
    assert spent == 2930 and spent <= limit  # not flagged as over

    # Next pay has no rates bill, so the usual amount doesn't carry it over
    ctx_next = Ctx(session, date(2026, 12, 20))
    assert round(usual_spend(ctx_next)[housing.id], 2) == 2030
