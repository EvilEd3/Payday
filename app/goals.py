"""Savings goals: how far along each one is, and what it takes to get there on time."""
from datetime import date, timedelta

from sqlmodel import Session, select

from .db import Account, Goal
from .periods import get_cycle, pay_period


def pays_between(session: Session, today: date, until: date) -> int:
    """Pay periods from the current one up to the goal date, the current one included."""
    cycle = get_cycle(session)
    n = 0
    for k in range(0, 1200):
        start, _ = pay_period(today, cycle, offset=k)
        if start > until:
            break
        n += 1
    return max(n, 1)


def goal_status(session: Session, g: Goal, today: date, balances: dict[str, float] | None = None) -> dict:
    acc = session.get(Account, g.account_id) if g.account_id else None
    if acc is not None:
        if balances is None or acc.id not in balances:
            from .forecast import account_balance
            bal = account_balance(session, acc)
        else:
            bal = balances[acc.id]
        saved = max(bal - (g.start_amount or 0), 0)
    else:
        saved = max(g.manual_saved or 0, 0)
    target = max(g.target or 0, 0)
    remaining = max(target - saved, 0)
    done = g.done or (target > 0 and remaining <= 0.004)
    out = {"goal": g, "account": acc, "saved": saved, "target": target, "remaining": remaining,
           "pct": min(saved / target, 1) if target else 0, "done": done,
           "days_left": None, "pays_left": None, "per_pay": None, "per_week": None, "per_day": None,
           "on_track": None, "expected": None, "projected": None, "overdue": False}
    if g.target_date and not done:
        days = (g.target_date - today).days
        out["days_left"] = days
        if days <= 0:
            out["overdue"] = True
            out["per_pay"] = out["per_week"] = out["per_day"] = remaining
        else:
            pays = pays_between(session, today, g.target_date)
            out.update(pays_left=pays, per_pay=remaining / pays, per_week=remaining / max(days / 7, 1),
                       per_day=remaining / days)
        created = g.created or today
        total = (g.target_date - created).days
        if total > 0:
            expected = target * min(max((today - created).days / total, 0), 1)
            out["expected"] = expected
            out["on_track"] = saved + 0.5 >= expected
    if not done and g.created and saved > 0:
        gone = (today - g.created).days
        if gone >= 7:  # too early to tell before a week
            rate = saved / gone
            out["projected"] = today + timedelta(days=round(remaining / rate)) if rate > 0 else None
    return out


def all_goals(session: Session, today: date, balances: dict[str, float] | None = None) -> list[dict]:
    goals = session.exec(select(Goal).order_by(Goal.done, Goal.target_date, Goal.id)).all()
    return [goal_status(session, g, today, balances) for g in goals]


def budget_for_goals(statuses: list[dict]) -> float:
    """What this pay sets aside for goals marked "Budget for this"."""
    return sum(st["per_pay"] or 0 for st in statuses if st["goal"].budget_it and not st["done"] and st["per_pay"])
