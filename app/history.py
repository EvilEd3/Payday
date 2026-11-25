"""Each account's balance over time, from what every sync saw, for the graph on the account's page.

A sync saves a reading for an account when its balance differs from the last one saved (record()), so a shares or
KiwiSaver account's value moving between syncs shows even though no transaction explains it. Before the first
reading, the once-a-day balances saved for net worth (BalanceSnapshot) fill in, so the graph has history from
the day those started rather than from the day this feature arrived."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from .db import Account, BalanceReading, BalanceSnapshot

RANGES = {"3m": ("3 months", 92), "1y": ("1 year", 366), "all": ("All", None)}


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def record(s: Session, when: datetime | None = None) -> int:
    """Save a reading for every account whose balance changed since its last one. Returns how many were saved."""
    when = _utc(when or datetime.now(timezone.utc))
    saved = 0
    for acc in s.exec(select(Account)):
        last = s.exec(select(BalanceReading).where(BalanceReading.account_id == acc.id)
                      .order_by(BalanceReading.at.desc())).first()
        if last and abs(last.balance - acc.balance_current) < 0.005 and (
                (last.available is None) == (acc.balance_available is None)
                and abs((last.available or 0) - (acc.balance_available or 0)) < 0.005):
            continue
        s.add(BalanceReading(account_id=acc.id, at=when, balance=acc.balance_current, available=acc.balance_available))
        saved += 1
    s.commit()
    return saved


def points(s: Session, acc: Account, tz: ZoneInfo) -> list[dict]:
    """The account's balance at each change, oldest first: {"at": aware datetime, "balance", "change", "daily"}.

    "daily" marks a point taken from the older once-a-day balances rather than a sync's own reading."""
    readings = s.exec(select(BalanceReading).where(BalanceReading.account_id == acc.id)
                      .order_by(BalanceReading.at)).all()
    first = _utc(readings[0].at) if readings else None
    out: list[dict] = []
    for snap in s.exec(select(BalanceSnapshot).where(BalanceSnapshot.account_id == acc.id)
                       .order_by(BalanceSnapshot.date)):
        if first and snap.date >= first.astimezone(tz).date():
            break  # the syncs' own readings take over from here
        at = datetime.combine(snap.date, time(12), tz)
        if not out or abs(out[-1]["balance"] - snap.balance) >= 0.005:
            out.append({"at": at, "balance": snap.balance, "daily": True})
    for r in readings:
        out.append({"at": _utc(r.at).astimezone(tz), "balance": r.balance, "daily": False})
    for prev, p in zip([None] + out, out):
        p["change"] = p["balance"] - prev["balance"] if prev else None
    return out


def _pad(lo: float, hi: float) -> tuple[float, float]:
    pad = (hi - lo) * 0.12 or abs(hi) * 0.05 or 1
    return lo - pad, hi + pad


def chart(pts: list[dict], now: datetime, range_key: str = "1y", height: int = 200) -> dict | None:
    """An SVG step line of the balance over the chosen range, with x proportional to time.

    The balance holds level between syncs and steps at each change, then runs on to now. Returns None when
    there's nothing to draw."""
    if not pts:
        return None
    days = RANGES.get(range_key, RANGES["1y"])[1]
    start = max(pts[0]["at"], now - timedelta(days=days)) if days else pts[0]["at"]
    if (now - start).total_seconds() < 3600:
        start = now - timedelta(days=1)  # one reading only: show the last day so the line has some length
    # the balance at the start of the range is the last change at or before it
    before = [p for p in pts if p["at"] <= start]
    shown = [p for p in pts if p["at"] > start]
    level = before[-1]["balance"] if before else (shown[0]["balance"] if shown else pts[-1]["balance"])
    vals = [level] + [p["balance"] for p in shown]
    lo, hi = _pad(min(vals), max(vals))
    span = (now - start).total_seconds() or 1

    def x(at: datetime) -> float:
        return max(0.0, min(100.0, (at - start).total_seconds() / span * 100))

    def y(v: float) -> float:
        return (hi - v) / (hi - lo) * 100

    path = [(0.0, y(level))]
    dots = []
    for p in shown:
        px = x(p["at"])
        path += [(px, path[-1][1]), (px, y(p["balance"]))]
        dots.append({"x": px, "y": y(p["balance"]), "p": p})
    path.append((100.0, path[-1][1]))
    line = " ".join(f"{a:.2f},{b:.2f}" for a, b in path)
    ticks = []
    for i in range(4):  # four dates along the bottom: the start, two between, and now
        at = start + (now - start) * (i / 3)
        ticks.append({"x": i / 3 * 100, "at": at, "last": i == 3})
    return {"line": line, "dots": dots, "low": min(vals), "high": max(vals), "y_low": y(min(vals)),
            "y_high": y(max(vals)), "zero": y(0) if lo < 0 < hi else None, "height": height, "ticks": ticks,
            "start": start, "start_balance": level, "end_balance": vals[-1], "change": vals[-1] - level,
            "short": (now - start) <= timedelta(days=3)}
