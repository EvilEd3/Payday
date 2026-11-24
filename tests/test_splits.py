"""A transaction split across categories counts each part in its own category."""
from datetime import date

from sqlmodel import select

from app.categorise import recategorise_all
from app.db import Account, Category, Split, Transaction
from app.forecast import build_dashboard
from app.insights import Ctx, category_spend, period_summary
from app.splits import expand

TODAY = date(2026, 11, 24)


def _cat(s, name):
    c = s.exec(select(Category).where(Category.name == name)).first() or Category(name=name)
    s.add(c)
    s.flush()
    return c


def _setup(s):
    s.add(Account(id="acc", name="Everyday", type="CHECKING", balance_current=1000))
    s.add(Transaction(id="pay", account_id="acc", date=date(2026, 11, 15), description="SALARY", amount=3000))
    s.add(Transaction(id="shop", account_id="acc", date=date(2026, 11, 20), description="THE WAREHOUSE",
                      amount=-80, akahu_group="Household"))
    s.commit()
    recategorise_all(s)
    food, household = _cat(s, "Food"), _cat(s, "Household")
    s.add(Split(txn_id="shop", amount=-50, category_id=food.id))
    s.add(Split(txn_id="shop", amount=-30, category_id=household.id))
    s.commit()
    return food, household


def test_parts_count_in_their_own_categories(session):
    food, household = _setup(session)
    d = build_dashboard(session, TODAY)
    cats = {cid: amt for cid, _, amt in d["categories"]}
    assert cats == {food.id: 50, household.id: 30}
    assert d["day_to_day"] == 80  # the whole purchase is still spent once
    start, end = Ctx(session, TODAY).period(0)
    assert dict(category_spend(Ctx(session, TODAY), start, end)) == {food.id: 50, household.id: 30}


def test_summary_counts_the_purchase_once_for_payees(session):
    _setup(session)
    p = period_summary(Ctx(session, TODAY), 0)
    assert p["spent"] == 80 and p["count"] == 1
    assert [g["count"] for g in p["payees"]] == [1]


def test_parts_that_no_longer_add_up_are_ignored(session):
    food, household = _setup(session)
    session.get(Transaction, "shop").amount = -85  # the bank changed the amount
    session.commit()
    assert [t.id for t in expand(session, session.exec(select(Transaction)).all())].count("shop") == 1


def test_split_form_saves_and_unsplit_clears(client):
    from app.db import use_profile
    from sqlmodel import Session
    from test_security import _signed_in, _user
    user = _user(client, "splitter")
    _signed_in(client, user)
    pid = user.last_profile_id or 1
    with use_profile(pid) as eng, Session(eng) as s:
        s.add(Transaction(id="w1", account_id="x", date=TODAY, description="KMART", amount=-100))
        food, house = _cat(s, "Food"), _cat(s, "Household")
        s.commit()
        fid, hid = food.id, house.id
    headers = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
    r = client.post("/transactions/w1/split", headers=headers, follow_redirects=False,
                    data={"category_id": [str(fid), str(hid), ""], "amount": ["60", "30", ""], "note": ["", "", ""]})
    assert "bad=sum" in r.headers["location"]  # 90 isn't 100
    r = client.post("/transactions/w1/split", headers=headers, follow_redirects=False,
                    data={"category_id": [str(fid), str(hid)], "amount": ["60", "40"], "note": ["", "kids"]})
    assert "bad" not in r.headers["location"]
    with use_profile(pid) as eng, Session(eng) as s:
        parts = s.exec(select(Split).where(Split.txn_id == "w1")).all()
        assert sorted(p.amount for p in parts) == [-60, -40]
        assert s.get(Transaction, "w1").category_id == fid and s.get(Transaction, "w1").category_source == "manual"
    assert "Part of a" not in client.get("/transactions").text
    page = client.get(f"/transactions?category={hid}").text
    assert "Part of a <span class=\"m\">$100.00</span> split" in page and "$40.00" in page
    client.post("/transactions/w1/unsplit", headers=headers, data={"next": "/transactions"})
    with use_profile(pid) as eng, Session(eng) as s:
        assert not s.exec(select(Split).where(Split.txn_id == "w1")).all()
