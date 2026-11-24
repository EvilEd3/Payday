"""Alerts sent to people's phones: what to say, to whom, and never the same thing twice.

Each person chooses their own alerts per household (Settings, Notifications). Payday looks for things worth
saying after every sync and once an hour, keeps a note of each alert it sent, and holds alerts back during the
person's quiet hours until the morning.
"""
import json
import logging
import os
import zlib
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlmodel import Session, select

from . import webpush
from .db import (Category, Goal, Membership, NotifySent, Profile, PushDevice, Transaction, current_engine,
                 get_setting, site_engine, use_profile)

log = logging.getLogger("payday.notify")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Pacific/Auckland"))
MAX_PER_RUN = 5  # more than this in one go are rolled into a single "and N more" message

DEFAULTS = {
    "bill_due": True, "due_days": 2, "bill_missed": True, "payday": True,
    "day_rate": True, "limit_over": True, "limit_near": False, "big": False, "big_amount": 200,
    "sync_failed": True, "stale": True, "short": True,
    "weekly": False, "period_end": True, "goal": True, "review": False,
    "quiet_start": 21, "quiet_end": 7,
}
SWITCHES = [k for k, v in DEFAULTS.items() if isinstance(v, bool)]


def prefs_of(m: Membership) -> dict:
    try:
        saved = json.loads(m.notify) if m.notify else {}
    except ValueError:
        saved = {}
    out = dict(DEFAULTS)
    out.update({k: saved[k] for k in DEFAULTS if k in saved and type(saved[k]) is type(DEFAULTS[k])})
    return out


def clean_prefs(form) -> dict:
    """Preferences from the settings form, kept to known keys and sensible numbers."""
    out = {k: form.get(k) == "true" for k in SWITCHES}

    def num(key, lo, hi):
        try:
            return max(lo, min(hi, int(float(str(form.get(key, DEFAULTS[key])).replace("$", "").replace(",", "")))))
        except ValueError:
            return DEFAULTS[key]
    out.update(due_days=num("due_days", 0, 14), big_amount=num("big_amount", 1, 1_000_000),
               quiet_start=num("quiet_start", 0, 23), quiet_end=num("quiet_end", 0, 23))
    return out


def quiet(prefs: dict, now: datetime) -> bool:
    start, end, h = prefs["quiet_start"], prefs["quiet_end"], now.hour
    if start == end:
        return False
    return (start <= h or h < end) if start > end else (start <= h < end)


def _money(v: float) -> str:
    return f"${v:,.2f}" if v != int(v) else f"${v:,.0f}"


def _day(d: date) -> str:
    return d.strftime("%a %-d %b")


def find(s: Session, today: date, now: datetime) -> list[dict]:
    """Everything that could be worth telling someone about this household right now.

    Each alert: kind (matches a preference switch), key (unique, so it's sent once), title, body, url, and for
    some a threshold the person's own preference decides (days, amount).
    """
    from .forecast import build_dashboard
    from .insights import needs_review_query
    out: list[dict] = []
    d = build_dashboard(s, today)
    cal = d.get("calendar")
    pay_word = "month" if cal else "pay"

    # bills and payday
    for u in d["upcoming"]:
        b = u["bill"]
        if u["overdue"]:
            out.append(dict(kind="bill_missed", key=f"missed:{b.id}:{u['due']}", url=f"/bills/{b.id}",
                            title=f"{b.name} hasn’t been paid",
                            body=f"{_money(b.amount)} was due {_day(u['due'])}, and no payment has come through."))
        else:
            out.append(dict(kind="bill_due", key=f"due:{b.id}:{u['due']}", url=f"/bills/{b.id}", days=(u["due"] - today).days,
                            title=f"{b.name} is due {'today' if u['due'] == today else ('tomorrow' if (u['due'] - today).days == 1 else _day(u['due']))}",
                            body=f"{_money(b.amount)}. You have {_money(max(d['budget_per_day'], 0))} a day until payday."))
    if d["income"] > 0:
        out.append(dict(kind="payday", key=f"pay:{d['start']}", url="/",
                        title="Your pay has landed",
                        body=f"{_money(d['income'])} in. That’s {_money(max(d['budget_per_day'], 0))} a day until "
                             f"{'the 1st' if cal else _day(d['payday'])}, after bills."))

    # an account that won't cover the bills coming out of it before payday
    names = {a.id: a.name for a in d["all_accounts"]}
    for acc_id, f in d.get("forecast", {}).items():
        if f["short"]:
            due = [b.name for when, b in f["bills"] if when <= f["low_date"]]
            out.append(dict(kind="short", key=f"short:{acc_id}:{f['low_date']}", url=f"/accounts/{acc_id}",
                            title=f"Your {names.get(acc_id, '')} account will be {_money(-f['low'])} short".replace("  ", " "),
                            body=f"{' and '.join(filter(None, [', '.join(due[:-1]), due[-1]]))} come{'s' if len(due) == 1 else ''} "
                                 f"out of it by {_day(f['low_date'])}. "
                                 "Move money in before then."))

    # spending
    try:
        warn = float(get_setting(s, "day_rate_warning") or 0)
    except ValueError:
        warn = 0
    if warn > 0 and d["income"] > 0 and d["budget_per_day"] < warn:
        out.append(dict(kind="day_rate", key=f"rate:{today}", url="/summary#day-rate",
                        title=f"Below your {_money(warn)} a day",
                        body=f"You have {_money(d['budget_per_day'])} a day for the next {d['days_left']} days."))
    for cid, name, amt in d["categories"]:
        lim = d["limits"].get(cid)
        if not lim or not lim.get("limit"):
            continue
        used = amt / lim["limit"]
        if used > 1:
            out.append(dict(kind="limit_over", key=f"over:{cid}:{d['start']}", url=f"/transactions?category={cid}",
                            title=f"{name} is over its limit",
                            body=f"{_money(amt)} spent against {_money(lim['limit'])} this {pay_word}."))
        elif used >= 0.8:
            out.append(dict(kind="limit_near", key=f"near:{cid}:{d['start']}", url=f"/transactions?category={cid}",
                            title=f"{name} is at {round(used * 100)}% of its limit",
                            body=f"{_money(lim['limit'] - amt)} left of {_money(lim['limit'])} this {pay_word}."))
    recent = s.exec(select(Transaction).where(Transaction.date >= today - timedelta(days=2), Transaction.amount < 0,
                                              Transaction.bill_id == None, Transaction.transfer_id == None)  # noqa: E711
                    ).all()
    transfer_cats = {c.id for c in s.exec(select(Category).where(Category.kind == "transfer"))}
    for t in recent:
        if t.category_id in transfer_cats:
            continue
        out.append(dict(kind="big", key=f"big:{t.id}", url=f"/transactions?search={(t.merchant or t.description)[:40]}",
                        amount=-t.amount, title=f"{_money(-t.amount)} at {t.merchant or t.description}",
                        body=f"{_day(t.date)}. You have {_money(max(d['budget_per_day'], 0))} a day until payday."))

    # bank and sync
    status = get_setting(s, "last_sync_status") or ""
    if status.startswith("Sync failed"):
        out.append(dict(kind="sync_failed", key=f"fail:{today}:{zlib.crc32(status.encode()) & 0xffff}", url="/",
                        title="Payday couldn’t sync with your bank", body=status.removeprefix("Sync failed. ")[:180]))
    from .main import bank_freshness, bank_stale  # noqa: PLC0415 - shared with the pages
    late = [a for a in d["all_accounts"] if bank_stale(a)]
    if late:
        oldest = min((a.refreshed_txns for a in late if a.refreshed_txns), default="")
        banks = " and ".join(sorted({a.institution or a.name for a in late}))
        out.append(dict(kind="stale", key=f"stale:{today}", url="/settings?tab=accounts",
                        title=f"{banks} hasn’t sent new data",
                        body=f"Last data {bank_freshness(oldest)}. Open Payday and press Sync now, or check my.akahu.nz."))

    # summaries and goals
    if now.weekday() == 6 and now.hour >= 17:
        week = [t for t in s.exec(select(Transaction).where(Transaction.date > today - timedelta(days=7),
                                                             Transaction.date <= today, Transaction.amount < 0,
                                                             Transaction.bill_id == None,  # noqa: E711
                                                             Transaction.transfer_id == None))  # noqa: E711
                if t.category_id not in transfer_cats]
        spent = -sum(t.amount for t in week)
        out.append(dict(kind="weekly", key=f"week:{today.isocalendar()[0]}-{today.isocalendar()[1]}", url="/summary",
                        title="Your week",
                        body=f"{_money(spent)} spent day to day in the last 7 days. "
                             f"{_money(max(d['budget_per_day'], 0))} a day for the next {d['days_left']} days."))
    if (today - d["start"]).days <= 2:
        last = build_dashboard(s, today, -1)
        if last["income"] > 0:
            kept = last["budget_left"]
            out.append(dict(kind="period_end", key=f"end:{last['start']}", url="/summary?offset=-1",
                            title=f"Last {pay_word}: {'kept' if kept >= 0 else 'overspent'} {_money(abs(kept))}",
                            body=f"{_money(last['day_to_day'])} day to day and {_money(last['bills_total'])} of bills, "
                                 f"from {_money(last['income'])} of income."))
    for st in d.get("goals") or []:
        g = st["goal"]
        if st["target"] > 0 and st["saved"] >= st["target"] - 0.004:
            out.append(dict(kind="goal", key=f"goal:{g.id}:{g.target}", url=f"/goals#g-{g.id}",
                            title=f"Goal reached: {g.name}", body=f"{_money(st['saved'])} saved of {_money(st['target'])}."))
    waiting = len(s.exec(needs_review_query()).all())
    if waiting:
        out.append(dict(kind="review", key=f"review:{today}", url="/review",
                        title=f"{waiting} transaction{'s' if waiting != 1 else ''} to check",
                        body="Payday guessed their categories. A quick look keeps your budget right."))
    return out


def wanted(alert: dict, prefs: dict) -> bool:
    kind = alert["kind"]
    if not prefs.get(kind):
        return False
    if kind == "bill_due" and alert["days"] > prefs["due_days"]:
        return False
    if kind == "big" and alert["amount"] < prefs["big_amount"]:
        return False
    return True


def deliver(s: Session, user_id: int, message: dict, client: httpx.Client | None = None) -> int:
    """Send to every device the person has. Returns how many took it; forgets devices that have gone."""
    ok = 0
    for dev in s.exec(select(PushDevice).where(PushDevice.user_id == user_id)).all():
        try:
            webpush.send(dev.endpoint, dev.p256dh, dev.auth, message, client=client)
            dev.last_sent = datetime.now(timezone.utc)
            s.add(dev)
            ok += 1
        except webpush.Gone:
            s.delete(dev)
        except httpx.HTTPError as e:
            log.warning("push to device %s failed: %s", dev.id, e)
    s.commit()
    return ok


def run_for_profile(pid: int, now: datetime | None = None, client: httpx.Client | None = None) -> int:
    """Send this household's new alerts to each member who wants them. Returns alerts sent."""
    now = now or datetime.now(TZ)
    with Session(site_engine) as site:
        members = site.exec(select(Membership).where(Membership.profile_id == pid)).all()
        with_devices = {d.user_id for d in site.exec(select(PushDevice))}
        members = [m for m in members if m.user_id in with_devices]
        if not members:
            return 0
        try:
            with use_profile(pid), Session(current_engine()) as s:
                alerts = find(s, now.date(), now)
        except Exception:  # noqa: BLE001 - one household's problem must not stop the others
            log.exception("notifications: couldn't check household %s", pid)
            return 0
        name = (site.get(Profile, pid).name if site.get(Profile, pid) else "")
        several = len({m.profile_id for m in site.exec(select(Membership))}) > 1
        sent_total = 0
        for m in members:
            prefs = prefs_of(m)
            if quiet(prefs, now):
                continue
            done = {r.key for r in site.exec(select(NotifySent).where(NotifySent.user_id == m.user_id,
                                                                       NotifySent.profile_id == pid))}
            todo = [a for a in alerts if wanted(a, prefs) and a["key"] not in done]
            if not todo:
                continue
            batch = todo[:MAX_PER_RUN]
            if len(todo) > MAX_PER_RUN:
                rest = len(todo) - MAX_PER_RUN + 1
                batch = todo[:MAX_PER_RUN - 1] + [dict(key="", url="/", title=f"{rest} more updates",
                                                      body="Open Payday to see them all.")]
            took_any = False
            for a in batch:
                title = f"{a['title']}" + (f" · {name}" if several and name else "")
                msg = {"title": title, "body": a["body"], "url": a["url"], "tag": a["key"] or "more"}
                if deliver(site, m.user_id, msg, client):
                    took_any = True
                    sent_total += 1
            if took_any:
                stamp = datetime.now(timezone.utc)
                for a in todo:  # everything in this run counts as said, the rolled-up ones too
                    site.add(NotifySent(user_id=m.user_id, profile_id=pid, key=a["key"], sent_at=stamp))
                site.commit()
        return sent_total


def run_all() -> int:
    with Session(site_engine) as site:
        pids = sorted({m.profile_id for m in site.exec(select(Membership))})
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        for old in site.exec(select(NotifySent).where(NotifySent.sent_at < cutoff)).all():
            site.delete(old)
        site.commit()
    return sum(run_for_profile(pid) for pid in pids)


def test_message(site: Session, user_id: int) -> int:
    return deliver(site, user_id, {"title": "Payday notifications are on",
                                   "body": "This is how your alerts will look on this device.", "url": "/settings?tab=notify",
                                   "tag": "test"})
