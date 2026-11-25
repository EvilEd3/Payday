"""The bills calendar: every bill in a pay period or a calendar month, past and to come, laid out by day."""
from collections import defaultdict
from datetime import date, timedelta

from sqlmodel import Session

from .forecast import bill_schedule, history_start
from .periods import _add_months, get_cycle, pay_period

VIEWS = ("pay", "month")
MAX_BACK, MAX_AHEAD = 60, 24  # how far the arrows go, in periods


def month_range(today: date, offset: int) -> tuple[date, date]:
    y, m = _add_months(today.year, today.month, offset)
    ny, nm = _add_months(y, m, 1)
    return date(y, m, 1), date(ny, nm, 1)


def paydays_between(cycle, start: date, end: date) -> list[date]:
    """Paydays falling in [start, end)."""
    out, k = [], 0
    first, _ = pay_period(start, cycle)
    while k < 40:
        s, _ = pay_period(first, cycle, offset=k)
        if s >= end:
            break
        if s >= start:
            out.append(s)
        k += 1
    return out


def build_calendar(session: Session, today: date, view: str = "pay", offset: int = 0) -> dict:
    cycle = get_cycle(session)
    view = "month" if cycle.calendar else (view if view in VIEWS else "pay")  # a calendar-month pay is both
    offset = max(-MAX_BACK, min(offset, MAX_AHEAD))
    start, end = month_range(today, offset) if view == "month" else pay_period(today, cycle, offset=offset)

    # Unpaid bills from before your transactions begin can't be known to be missed: leave them out.
    data_start = history_start(session, today)
    entries = [e for e in bill_schedule(session, start, end, today, paused=True)
               if e["txn"] or e["due"] >= data_start]

    by_day: dict[date, list] = defaultdict(list)
    for e in entries:
        by_day[e["due"]].append(e)
    paydays = set(paydays_between(cycle, start, end + timedelta(days=1)))

    # Monday to Sunday rows covering the range; days outside it are blank.
    first = start - timedelta(days=start.weekday())
    last = end - timedelta(days=1)
    last += timedelta(days=6 - last.weekday())
    weeks, d = [], first
    while d <= last:
        days = []
        for _ in range(7):
            inside = start <= d < end
            items = by_day.get(d, []) if inside else []
            days.append({"date": d, "inside": inside, "bills": items, "total": sum(e["amount"] for e in items),
                         "today": d == today, "payday": d in paydays and not cycle.calendar})
            d += timedelta(days=1)
        weeks.append({"days": days, "total": sum(x["total"] for x in days)})

    totals = {k: 0.0 for k in ("paid", "pending", "due", "missed")}
    for e in entries:
        totals[e["status"]] += e["amount"]
    totals["all"] = sum(totals.values())

    per_bill: dict[int, dict] = {}
    for e in entries:
        row = per_bill.setdefault(e["bill"].id, {"bill": e["bill"], "count": 0, "paid": 0.0, "due": 0.0,
                                                 "missed": 0.0, "total": 0.0, "dates": []})
        row["count"] += 1
        row["total"] += e["amount"]
        row["paid" if e["status"] in ("paid", "pending") else e["status"]] += e["amount"]
        row["dates"].append(e["due"])
    bills = sorted(per_bill.values(), key=lambda r: (-r["total"], r["bill"].name.lower()))

    return dict(view=view, offset=offset, start=start, end=end, today=today, calendar=cycle.calendar,
                is_past=end <= today, is_future=start > today, weeks=weeks, entries=entries, totals=totals,
                bills=bills, counts={k: sum(1 for e in entries if e["status"] == k) for k in totals},
                min_offset=-MAX_BACK, max_offset=MAX_AHEAD, paydays=sorted(paydays),
                switch=_switch_offsets(today, cycle, start, end))


def _switch_offsets(today: date, cycle, start: date, end: date) -> dict:
    """Changing view keeps you near the same dates: {view: offset} for the other view."""
    anchor = today if start <= today < end else start + (end - start) / 2
    month = (anchor.year - today.year) * 12 + anchor.month - today.month
    target, _ = pay_period(anchor, cycle)
    pay, step = 0, (1 if target > today else -1)
    while pay_period(today, cycle, offset=pay)[0] != target and abs(pay) <= MAX_BACK + MAX_AHEAD:
        pay += step
    return {"month": month, "pay": pay}
