"""The activity log: money in and out, allocations, notifications (and where tapping one goes), admin settings."""
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app import activity, notify
from app.categorise import pair_transfers, recategorise_all
from app.db import (Account, Activity, Bill, Category, Membership, PushDevice, Rule, Transaction, site_engine,
                    use_profile)

HEADERS = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}


def _entries(s, kind=None):
    q = select(Activity).order_by(Activity.id)
    if kind:
        q = q.where(Activity.kind == kind)
    return s.exec(q).all()


def test_a_kind_an_admin_switched_off_isnt_recorded(session):
    activity.save_settings(session, ["money", "notification", "sync", "change"], 90)
    assert activity.record(session, "allocation", "Moved") is None
    assert activity.record(session, "money", "In", amount=5, who="Payday") is not None
    assert activity.keep_days(session) == 90
    old = Activity(at=datetime.now(timezone.utc) - timedelta(days=120), kind="money", title="old")
    session.add(old)
    session.commit()
    assert activity.prune(session) == 1
    assert [e.title for e in _entries(session)] == ["In"]


def test_transfers_and_re_sorts_are_logged_with_what_moved(session):
    session.add(Account(id="a", name="Everyday"))
    session.add(Account(id="b", name="Savings"))
    session.add(Transaction(id="t1", account_id="a", date=date(2026, 11, 2), description="TO SAVINGS", amount=-250))
    session.add(Transaction(id="t2", account_id="b", date=date(2026, 11, 2), description="FROM EVERYDAY", amount=250))
    session.add(Transaction(id="t3", account_id="a", date=date(2026, 11, 3), description="COUNTDOWN", amount=-80))
    session.commit()
    assert pair_transfers(session) == 1
    t = _entries(session, "allocation")[-1]
    assert t.title == "Transfer matched: $250.00 from Everyday to Savings" and t.ref == "t1"

    groceries = Category(name="Groceries")
    session.add(groceries)
    session.commit()
    recategorise_all(session)  # nothing moved yet: no entry
    before = len(_entries(session, "allocation"))
    session.add(Rule(pattern="COUNTDOWN", category_id=groceries.id))
    session.commit()
    recategorise_all(session, "Rule added: COUNTDOWN goes in Groceries.")
    rows = _entries(session, "allocation")
    assert len(rows) == before + 1
    assert rows[-1].title == "1 transaction re-allocated"
    assert "Rule added" in rows[-1].body and "COUNTDOWN: from Uncategorised to Groceries" in rows[-1].body


def test_a_bill_payment_reads_as_allocated_to_the_bill(session):
    bills = session.exec(select(Category).where(Category.name == "Bills")).one()
    session.add(Account(id="a", name="Everyday"))
    session.commit()
    session.add(Bill(id=1, name="Power", amount=180, anchor_date=date(2026, 11, 20), match_pattern="MERIDIAN",
                     category_id=bills.id))
    session.add(Transaction(id="p1", account_id="a", date=date(2026, 11, 20), description="MERIDIAN ENERGY",
                            amount=-181.5))
    session.commit()
    recategorise_all(session)
    t = session.get(Transaction, "p1")
    names = activity.Names(session)
    assert activity.allocation(t, names) == "Allocated to the bill Power, due Fri 20 Nov (Bills)"
    activity.new_transaction(session, t, names)
    e = _entries(session, "money")[-1]
    assert e.title == "−$181.50 at MERIDIAN ENERGY" and "Account: Everyday" in e.body


def _member_with_device(client, name):
    from test_security import _signed_in, _user
    user = _user(client, name)
    _signed_in(client, user)
    with Session(site_engine) as s:
        m = s.exec(select(Membership).where(Membership.user_id == user.id)).first()
        if not s.exec(select(PushDevice).where(PushDevice.user_id == user.id)).first():
            s.add(PushDevice(user_id=user.id, endpoint=f"https://fcm.googleapis.com/fcm/send/{name}", p256dh="x",
                             auth="y"))
            s.commit()
        return user, m.profile_id


def test_a_notification_is_logged_and_tapping_it_opens_its_entry(client, monkeypatch):
    user, pid = _member_with_device(client, "logalerts")
    sent = []
    monkeypatch.setattr(notify, "find", lambda s, today, now: [
        dict(kind="bill_due", key="due:9:2026-11-26", url="/bills/9", days=1, title="Rates is due tomorrow",
             body="$420. You have $55 a day until payday.")])
    monkeypatch.setattr(notify.webpush, "send", lambda endpoint, p256dh, auth, message, client=None: sent.append(message))
    noon = datetime(2026, 11, 25, 12, tzinfo=ZoneInfo("Pacific/Auckland"))
    assert notify.run_for_profile(pid, now=noon) == 1
    with use_profile(pid) as eng, Session(eng) as s:
        e = s.exec(select(Activity).where(Activity.ref == "notify:due:9:2026-11-26")).one()
        assert e.kind == "notification" and e.url == "/bills/9"
        assert "$420. You have $55 a day until payday." in e.body
        rec = json.loads(e.data)["recipients"]
        assert rec[0]["who"] == "logalerts" and rec[0]["devices"] == 1 and rec[0]["status"] == "sent"
    assert sent[0]["url"] == f"/activity/{e.id}?household={pid}"

    r = client.get(sent[0]["url"])
    assert r.status_code == 200
    assert "Rates is due tomorrow" in r.text and "What the notification said" in r.text
    assert 'href="/bills/9"' in r.text and "Open the bill" in r.text and "logalerts" in r.text
    assert "Rates is due tomorrow" in client.get("/activity?kind=notification").text


def test_with_notification_logging_off_a_tap_opens_the_page_itself(client, monkeypatch):
    user, pid = _member_with_device(client, "lognolog")
    with use_profile(pid) as eng, Session(eng) as s:
        activity.save_settings(s, ["money", "allocation", "sync", "change"], 365)
    sent = []
    monkeypatch.setattr(notify, "find", lambda s, today, now: [
        dict(kind="goal", key="goal:3:500", url="/goals#g-3", title="Goal reached: Bach", body="$500 saved of $500.")])
    monkeypatch.setattr(notify.webpush, "send", lambda endpoint, p256dh, auth, message, client=None: sent.append(message))
    notify.run_for_profile(pid, now=datetime(2026, 11, 25, 12, tzinfo=ZoneInfo("Pacific/Auckland")))
    assert sent and sent[-1]["url"] == "/goals#g-3"
    with use_profile(pid) as eng, Session(eng) as s:
        activity.save_settings(s, [k for k, _ in activity.KINDS], 365)


def test_an_admin_changes_the_log_settings_and_fixes_a_category_from_an_entry(client):
    from test_security import _signed_in, _user
    user = _user(client, "logadmin")
    _signed_in(client, user)
    with Session(site_engine) as s:
        pid = s.exec(select(Membership).where(Membership.user_id == user.id)).first().profile_id
    with use_profile(pid) as eng, Session(eng) as s:
        s.merge(Account(id="lg", name="Everyday"))
        s.merge(Transaction(id="lgt", account_id="lg", date=date.today(), description="PAK N SAVE", amount=-64.2))
        cat = Category(name="Food shop")
        s.add(cat)
        s.commit()
        cat_id = cat.id
        e = activity.record(s, "money", "−$64.20 at PAK N SAVE", "Account: Everyday", amount=-64.2, who="Payday",
                            url="/transactions?search=PAK", ref="lgt", commit=True)
        entry = e.id
    page = client.get(f"/activity/{entry}")
    assert "Change the category" in page.text
    r = client.post("/transactions/lgt/category", headers=HEADERS, follow_redirects=False,
                    data={"category_id": str(cat_id), "next": f"/activity/{entry}"})
    assert r.headers["location"].startswith(f"/activity/{entry}")
    assert "Allocated to Food shop, set by hand" in client.get(f"/activity/{entry}").text
    with use_profile(pid) as eng, Session(eng) as s:
        change = s.exec(select(Activity).where(Activity.kind == "change", Activity.ref == "lgt")).one()
        assert change.who == "logadmin" and change.title == "Category changed: −$64.20 PAK N SAVE"

    r = client.post("/activity/settings", headers=HEADERS, follow_redirects=False, data={"on": ["money"], "keep": "30"})
    assert r.status_code == 303
    with use_profile(pid) as eng, Session(eng) as s:
        assert activity.off_kinds(s) == {"allocation", "notification", "sync", "change"}
        assert activity.keep_days(s) == 30
    assert "Activity log settings changed" in client.get("/activity").text  # an admin's own change is always kept
    client.post("/activity/settings", headers=HEADERS, data={"on": [k for k, _ in activity.KINDS], "keep": "365"})

    assert client.post("/activity/clear", headers=HEADERS, follow_redirects=False,
                       data={"confirm": "no"}).headers["location"].endswith("msg=noclear")
    client.post("/activity/clear", headers=HEADERS, data={"confirm": "clear"})
    with use_profile(pid) as eng, Session(eng) as s:
        assert [x.title for x in _entries(s)] == ["Activity log cleared"]


def test_a_viewer_reads_the_log_but_cant_change_it(client):
    from app import auth
    from test_security import _signed_in, _user
    admin = _user(client, "logowner2")
    viewer = _user(client, "logviewer")
    with Session(site_engine) as s:
        m = s.exec(select(Membership).where(Membership.user_id == viewer.id)).first()
        m.role = "viewer"
        s.add(m)
        s.commit()
    _signed_in(client, viewer)
    assert client.get("/activity").status_code == 200
    assert client.post("/activity/clear", headers=HEADERS, data={"confirm": "clear"}).status_code == 403
    assert client.post("/activity/settings", headers=HEADERS, data={"on": []}).status_code == 403
    _signed_in(client, admin)
    assert auth  # the admin signs back in for later tests


def test_a_sync_logs_each_new_transaction_and_its_own_steps(client, monkeypatch):
    from app import akahu
    from test_security import _user
    user = _user(client, "logsync")
    with Session(site_engine) as s:
        pid = s.exec(select(Membership).where(Membership.user_id == user.id)).first().profile_id
    accounts = [{"_id": "acc_s1", "name": "BNZ Everyday", "type": "CHECKING", "balance": {"current": 900},
                 "connection": {"name": "BNZ"}, "refreshed": {"transactions": "2026-11-24T23:15:00Z"}}]
    txns = [{"_id": "trans_s1", "_account": "acc_s1", "date": "2026-11-24T02:00:00Z", "description": "NEW WORLD",
             "amount": -42.1, "type": "EFTPOS"}]
    replies = {"/accounts": accounts, "/transactions": txns, "/transactions/pending": []}
    monkeypatch.setattr(akahu, "_headers", lambda *a, **k: {})
    monkeypatch.setattr(akahu, "_get_all", lambda client, path, params=None, label="": replies[path])
    with use_profile(pid) as eng, Session(eng) as s:
        s.merge(Transaction(id="trans_old", account_id="acc_s1", date=date(2026, 10, 1), description="OLD", amount=-1))
        s.commit()
        msg = akahu.sync("scheduled")
        assert msg.startswith("Finished: 1 new transaction")
        money = s.exec(select(Activity).where(Activity.ref == "trans_s1")).one()
        assert money.title == "−$42.10 at NEW WORLD" and "Account: BNZ Everyday" in money.body
        synced = s.exec(select(Activity).where(Activity.kind == "sync")).all()[-1]
        assert synced.title == "Synced with your bank" and "BNZ Everyday: balance $900.00" in synced.body
        assert "Finished: 1 new transaction" in synced.body
