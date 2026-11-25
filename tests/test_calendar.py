"""The bills calendar: every bill in a pay period or month, paid, missed or to come, with totals."""
from datetime import date

from app.billcal import build_calendar, month_range
from app.db import Account, Bill, Transaction, set_setting
from app.forecast import build_dashboard

TODAY = date(2026, 11, 24)  # pay period 15 Nov to 14 Dec (payday the 15th, no weekend shift)


def _setup(s):
    s.add(Account(id="acc", name="Everyday", type="CHECKING", balance_current=3000, balance_available=3000))
    power = Bill(name="Power", amount=180, frequency="monthly", anchor_date=date(2026, 1, 20), match_pattern="MERCURY")
    gym = Bill(name="Gym", amount=20, frequency="weekly", anchor_date=date(2026, 1, 5), match_pattern="GYM")
    old = Bill(name="Old phone", amount=50, frequency="monthly", anchor_date=date(2026, 1, 16), match_pattern="PHONE",
               active=False)
    s.add_all([power, gym, old])
    s.flush()
    s.add(Transaction(id="first", account_id="acc", date=date(2026, 9, 1), description="START", amount=-1))
    s.add(Transaction(id="p", account_id="acc", date=date(2026, 11, 20), description="MERCURY", amount=-175.5,
                      bill_id=power.id, bill_due_date=date(2026, 11, 20)))
    s.add(Transaction(id="g", account_id="acc", date=date(2026, 11, 16), description="GYM", amount=-20,
                      bill_id=gym.id, bill_due_date=date(2026, 11, 16)))
    s.add(Transaction(id="o", account_id="acc", date=date(2026, 11, 16), description="PHONE", amount=-50,
                      bill_id=old.id, bill_due_date=date(2026, 11, 16)))
    s.commit()
    return power, gym, old


def test_pay_period_view_sorts_bills_by_status(session):
    _setup(session)
    c = build_calendar(session, TODAY, "pay")
    assert (c["start"], c["end"]) == (date(2026, 11, 15), date(2026, 12, 15))
    status = {(e["bill"].name, e["due"]): e["status"] for e in c["entries"]}
    assert status[("Power", date(2026, 11, 20))] == "paid"
    assert status[("Gym", date(2026, 11, 16))] == "paid"
    assert status[("Gym", date(2026, 11, 23))] == "missed"
    assert status[("Gym", date(2026, 11, 30))] == "due"
    assert status[("Old phone", date(2026, 11, 16))] == "paid"  # a paused bill's payment still shows
    assert ("Old phone", date(2026, 12, 16)) not in status
    t = c["totals"]
    assert t["paid"] == 175.5 + 20 + 50  # what was actually paid
    assert t["missed"] == 20
    assert t["due"] == 20 * 3  # gym 30 Nov, 7 and 14 Dec
    assert t["all"] == t["paid"] + t["missed"] + t["due"]
    assert sum(w["total"] for w in c["weeks"]) == t["all"]
    assert all(len(w["days"]) == 7 and w["days"][0]["date"].weekday() == 0 for w in c["weeks"])


def test_month_view_and_switching(session):
    _setup(session)
    c = build_calendar(session, TODAY, "month")
    assert (c["start"], c["end"]) == (date(2026, 11, 1), date(2026, 12, 1))
    assert date(2026, 11, 15) in c["paydays"]
    assert c["switch"]["pay"] == 0
    back = build_calendar(session, TODAY, "month", -2)  # September
    assert back["start"] == date(2026, 9, 1)
    assert back["switch"]["pay"] == -2  # mid September is in the pay period from 15 Sep
    assert month_range(date(2026, 12, 5), 1) == (date(2027, 1, 1), date(2027, 2, 1))


def test_nothing_missed_before_history_and_future_is_all_to_come(session):
    _setup(session)
    early = build_calendar(session, TODAY, "month", -4)  # July: before the first transaction
    assert early["entries"] == []
    ahead = build_calendar(session, TODAY, "pay", 2)
    assert ahead["entries"] and {e["status"] for e in ahead["entries"]} == {"due"}


def test_calendar_month_pay_has_one_view(session):
    set_setting(session, "pay_frequency", "calendar")
    _setup(session)
    c = build_calendar(session, TODAY, "pay")
    assert c["view"] == "month" and c["start"] == date(2026, 11, 1)


def test_overview_still_counts_the_same_bills(session):
    _setup(session)
    d = build_dashboard(session, TODAY)
    c = build_calendar(session, TODAY, "pay")
    active = [e for e in c["entries"] if e["bill"].active]
    assert len(d["upcoming"]) + len(d["paid"]) == len(active)
    assert d["to_come"] == c["totals"]["due"] + c["totals"]["missed"]
    assert "runway" not in d


def test_page_renders(client):
    from app import main
    client.cookies.clear()  # other tests share this client and may have signed it in
    assert client.get("/calendar", follow_redirects=False).status_code in (303, 307)  # signed out
    assert main.app.url_path_for("calendar_page") == "/calendar"
