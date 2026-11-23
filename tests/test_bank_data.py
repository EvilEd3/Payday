"""The "Bank data from ..." label on the Overview must move forward after a sync brings new data."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.main import bank_data, bank_freshness

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _acc(name, hours_ago, seen_hours_ago=0.0):
    return SimpleNamespace(name=name, refreshed_txns=(NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z"),
                           updated_at=NOW - timedelta(hours=seen_hours_ago))


def test_shows_newest_data_not_the_oldest_account():
    got = bank_data([_acc("Everyday", 0.5), _acc("Savings", 20)])
    assert got["when"] == (NOW - timedelta(hours=0.5)).isoformat()
    assert [b["name"] for b in got["behind"]] == ["Savings"]


def test_moves_forward_after_a_sync():
    before = bank_data([_acc("Everyday", 20), _acc("Savings", 21)])
    after = bank_data([_acc("Everyday", 0.1), _acc("Savings", 21)])
    assert after["when"] > before["when"]
    assert bank_freshness(after["when"]).startswith(("today", "yesterday"))


def test_accounts_in_step_are_not_behind():
    assert bank_data([_acc("Everyday", 1), _acc("Credit card", 1.2)])["behind"] == []


def test_account_the_last_sync_did_not_see_is_not_named():
    # e.g. an account removed from Akahu: still in Payday, never updated again
    got = bank_data([_acc("Everyday", 0.2), _acc("Old account", 300, seen_hours_ago=300)])
    assert got["behind"] == []


def test_no_dates_yet():
    assert bank_data([SimpleNamespace(name="A", refreshed_txns="", updated_at=None)]) is None
    assert bank_data([]) is None
