"""Pay-cycle periods: a period runs from one payday up to (not including) the next.

Frequencies: monthly (on a day of the month), fortnightly or weekly (counted from one known payday), or calendar
(the 1st to the end of each month, for people who'd rather budget by month).
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta

FREQUENCIES = ("monthly", "fortnightly", "weekly", "calendar")


@dataclass
class Cycle:
    frequency: str = "monthly"
    day: int = 15  # monthly: day of the month
    anchor: date | None = None  # weekly / fortnightly: any one payday
    adjust: str = "previous_business_day"  # what happens when payday lands on a weekend

    @property
    def calendar(self) -> bool:
        return self.frequency == "calendar"


def _shift(d: date, adjust: str) -> date:
    if adjust == "previous_business_day":
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    elif adjust == "next_business_day":
        while d.weekday() >= 5:
            d += timedelta(days=1)
    return d


def payday_in_month(year: int, month: int, day: int, adjust: str) -> date:
    last = calendar.monthrange(year, month)[1]
    return _shift(date(year, month, min(day, last)), adjust)  # e.g. "31st" becomes 30th / 28th


def _add_months(y: int, m: int, n: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) + n
    return idx // 12, idx % 12 + 1


def _monthly(today: date, day: int, adjust: str, offset: int) -> tuple[date, date]:
    y, m = today.year, today.month
    while payday_in_month(y, m, day, adjust) > today:
        y, m = _add_months(y, m, -1)
    y, m = _add_months(y, m, offset)
    ny, nm = _add_months(y, m, 1)
    return payday_in_month(y, m, day, adjust), payday_in_month(ny, nm, day, adjust)


def _every(today: date, anchor: date, step: int, adjust: str, offset: int) -> tuple[date, date]:
    def payday(k: int) -> date:
        return _shift(anchor + timedelta(days=step * k), adjust)
    k = (today - anchor).days // step
    while payday(k) > today:
        k -= 1
    while payday(k + 1) <= today:
        k += 1
    return payday(k + offset), payday(k + offset + 1)


def pay_period(today: date, cycle: "Cycle | int", adjust: str | None = None, offset: int = 0) -> tuple[date, date]:
    """(start, next payday) for the period containing today, moved by offset periods.
    Also accepts the older (today, day_of_month, adjust, offset) form."""
    if not isinstance(cycle, Cycle):
        cycle = Cycle("monthly", int(cycle), None, adjust or "none")
    if cycle.frequency == "calendar":
        return _monthly(today, 1, "none", offset)
    if cycle.frequency in ("weekly", "fortnightly") and cycle.anchor:
        return _every(today, cycle.anchor, 7 if cycle.frequency == "weekly" else 14, cycle.adjust, offset)
    return _monthly(today, cycle.day, cycle.adjust, offset)


def get_cycle(session) -> Cycle:
    from .db import get_setting
    freq = get_setting(session, "pay_frequency") or "monthly"
    try:
        anchor = date.fromisoformat(get_setting(session, "pay_anchor")) if get_setting(session, "pay_anchor") else None
    except ValueError:
        anchor = None
    return Cycle(freq if freq in FREQUENCIES else "monthly", int(get_setting(session, "payday_day") or 15),
                 anchor, get_setting(session, "payday_adjust") or "none")


def period_of(session, today: date, offset: int = 0) -> tuple[date, date]:
    return pay_period(today, get_cycle(session), offset=offset)
