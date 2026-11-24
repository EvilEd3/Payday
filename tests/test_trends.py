"""Several pay periods side by side."""
from datetime import date

from app.categorise import recategorise_all
from app.db import Account, Transaction
from app.insights import Ctx, trends

TODAY = date(2026, 11, 24)  # pay period 15 Nov to 14 Dec


def test_trends_lists_periods_oldest_first_and_averages_finished_ones(session):
    session.add(Account(id="acc", name="Everyday", type="CHECKING", balance_current=0))
    for m, spend in [(9, 100), (10, 300), (11, 50)]:
        session.add(Transaction(id=f"pay{m}", account_id="acc", date=date(2026, m, 15), description="SALARY",
                                amount=1000))
        session.add(Transaction(id=f"shop{m}", account_id="acc", date=date(2026, m, 20), description="SHOP",
                                amount=-spend, akahu_group="Food"))
    session.commit()
    recategorise_all(session)

    t = trends(Ctx(session, TODAY), 3)
    assert [p["start"] for p in t["periods"]] == [date(2026, 9, 15), date(2026, 10, 15), date(2026, 11, 15)]
    assert [p["current"] for p in t["periods"]] == [False, False, True]
    assert t["finished"] == 2
    assert t["average"]["income"] == 1000 and t["average"]["spent"] == 200  # this pay so far isn't averaged
    assert t["average"]["rate"] == 0.8
    food = t["categories"][0]
    assert food["name"] == "Food" and food["amounts"] == [100, 300, 50] and food["average"] == 200


def test_trends_page(client):
    from test_security import _signed_in, _user
    _signed_in(client, _user(client, "trender"))
    r = client.get("/trends?pays=12")
    assert r.status_code == 200 and "Pay by pay" in r.text
