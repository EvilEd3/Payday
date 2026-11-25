"""Each sync saves an account's balance when it changed, and the account's page graphs it."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app import auth, history
from app.db import Account, BalanceReading, BalanceSnapshot, engine_for, site_engine
from test_security import _signed_in, _user

TZ = ZoneInfo("Pacific/Auckland")
T0 = datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)


def _shares(s, value=1000.0):
    acc = s.get(Account, "shares") or Account(id="shares", name="Sharesies", type="INVESTMENT")
    acc.balance_current = value
    s.add(acc)
    s.commit()
    return acc


def test_a_reading_only_when_the_balance_changed(session):
    _shares(session, 1000)
    assert history.record(session, T0) == 1
    assert history.record(session, T0 + timedelta(hours=1)) == 0  # same value, nothing new
    _shares(session, 1042.5)
    assert history.record(session, T0 + timedelta(hours=2)) == 1
    rows = session.exec(select(BalanceReading).order_by(BalanceReading.at)).all()
    assert [r.balance for r in rows] == [1000, 1042.5]


def test_points_start_from_the_daily_balances_then_each_sync(session):
    acc = _shares(session, 1100)
    session.add(BalanceSnapshot(account_id="shares", date=date(2026, 10, 20), balance=900))
    session.add(BalanceSnapshot(account_id="shares", date=date(2026, 10, 21), balance=900))  # same: skipped
    session.add(BalanceSnapshot(account_id="shares", date=date(2026, 10, 25), balance=950))
    session.add(BalanceSnapshot(account_id="shares", date=date(2026, 11, 1), balance=999))  # a reading covers it
    session.add(BalanceReading(account_id="shares", at=T0, balance=1000))
    session.add(BalanceReading(account_id="shares", at=T0 + timedelta(days=2), balance=1100))
    session.commit()
    pts = history.points(session, acc, TZ)
    assert [p["balance"] for p in pts] == [900, 950, 1000, 1100]
    assert [p["daily"] for p in pts] == [True, True, False, False]
    assert [p["change"] for p in pts] == [None, 50, 50, 100]


def test_chart_steps_at_each_change_and_runs_to_now(session):
    acc = _shares(session)
    for i, v in enumerate([1000, 1200, 800]):
        session.add(BalanceReading(account_id="shares", at=T0 + timedelta(days=10 * i), balance=v))
    session.commit()
    now = (T0 + timedelta(days=30)).astimezone(TZ)
    c = history.chart(history.points(session, acc, TZ), now, "all")
    assert c["start_balance"] == 1000 and c["end_balance"] == 800 and c["change"] == -200
    assert c["high"] == 1200 and c["low"] == 800
    assert c["line"].startswith("0.00,") and c["line"].split()[-1].startswith("100.00,")
    assert [round(d["x"]) for d in c["dots"]] == [33, 67]  # the first reading is the start of the range
    # a shorter range starts from the balance at that time
    c = history.chart(history.points(session, acc, TZ), now, "3m")
    assert c["start"] == T0.astimezone(TZ)


def test_nothing_to_draw_without_readings(session):
    acc = _shares(session)
    assert history.chart(history.points(session, acc, TZ), datetime.now(TZ)) is None


def test_account_page_shows_the_graph(client):
    user = _user(client, "grapher")
    with Session(site_engine) as s:
        pid = s.exec(select(auth.Membership).where(auth.Membership.user_id == user.id)).first().profile_id
    now = datetime.now(timezone.utc)
    with Session(engine_for(pid)) as s:
        s.add(Account(id="bh-shares", name="Sharesies", type="INVESTMENT", balance_current=1500))
        s.add(Account(id="bh-empty", name="New savings", type="SAVINGS", balance_current=10))
        s.add(BalanceReading(account_id="bh-shares", at=now - timedelta(days=20), balance=1400))
        s.add(BalanceReading(account_id="bh-shares", at=now - timedelta(days=2), balance=1500))
        s.commit()
    _signed_in(client, user)
    try:
        html = client.get("/accounts/bh-shares?chart=3m").text
        empty = client.get("/accounts/bh-empty").text
        odd = client.get("/accounts/bh-shares?chart=nonsense").status_code
    finally:
        with Session(engine_for(pid)) as s:
            for acc_id in ("bh-shares", "bh-empty"):
                s.delete(s.get(Account, acc_id))
            for r in s.exec(select(BalanceReading).where(BalanceReading.account_id.like("bh-%"))):
                s.delete(r)
            s.commit()
    assert "Balance over time" in html and 'class="nw-line"' in html
    assert "+$100.00" in html and "Latest 2 of 2 changes" in html
    assert 'aria-current="page">3 months' in html
    assert 'data-tip="' in html and '<span class="m">' not in html.split('data-tip="')[1].split('"')[0]
    assert "No balances saved yet" in empty
    assert odd == 200
