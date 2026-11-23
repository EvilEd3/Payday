"""Payee grouping, period summaries and small server-rendered SVG charts."""
import hashlib
import math
import re
from collections import defaultdict
from datetime import date, timedelta

from markupsafe import Markup, escape
from sqlmodel import Session, select

from .db import Account, Category, Transaction
from .periods import get_cycle, pay_period

PALETTE = ["--c1", "--c2", "--c3", "--c4", "--c5", "--c6", "--c7", "--c8"]


def payee_key(t: Transaction) -> str:
    """Group name for a transaction: Akahu's merchant, else the description minus reference numbers."""
    if t.merchant:
        return t.merchant.strip()
    words = [w for w in re.split(r"\s+", t.description) if w and not any(ch.isdigit() for ch in w)]
    return (" ".join(words[:3]) or t.description).title()


class Ctx:
    """Lookups shared by the pages below."""

    def __init__(self, session: Session, today: date):
        self.s = session
        self.today = today
        self.cycle = get_cycle(session)
        self.cats = {c.id: c for c in session.exec(select(Category))}
        self.accounts = {a.id: a for a in session.exec(select(Account))}

    def period(self, offset: int) -> tuple[date, date]:
        return pay_period(self.today, self.cycle, offset=offset)

    def kind(self, t: Transaction) -> str:
        return self.cats[t.category_id].kind if t.category_id in self.cats else "expense"

    def cat_name(self, t: Transaction) -> str:
        return self.cats[t.category_id].name if t.category_id in self.cats else "Uncategorised"

    def is_spending(self, t: Transaction) -> bool:
        return self.kind(t) == "expense" and self.counts(t)

    def counts(self, t: Transaction) -> bool:
        """Is this transaction's account part of the budget ("Spending counts" ticked)?"""
        acc = self.accounts.get(t.account_id)
        return acc is None or acc.count_spending

    def txns(self, start: date | None = None, end: date | None = None) -> list[Transaction]:
        q = select(Transaction)
        if start:
            q = q.where(Transaction.date >= start)
        if end:
            q = q.where(Transaction.date < end)
        return self.s.exec(q.order_by(Transaction.date.desc())).all()


# ---------- payees ----------
def payee_groups(ctx: Ctx, txns: list[Transaction]) -> list[dict]:
    groups: dict[str, dict] = {}
    for t in txns:
        if not ctx.counts(t):  # accounts that aren't part of the budget
            continue
        if ctx.kind(t) == "transfer":
            continue
        g = groups.setdefault(payee_key(t), {"name": payee_key(t), "count": 0, "out": 0.0, "in": 0.0,
                                             "last": t.date, "cats": defaultdict(int)})
        g["count"] += 1
        g["out" if t.amount < 0 else "in"] += abs(t.amount)
        g["last"] = max(g["last"], t.date)
        g["cats"][ctx.cat_name(t)] += 1
    out = []
    for g in groups.values():
        g["category"] = max(g["cats"].items(), key=lambda kv: kv[1])[0]
        g["net"] = g["in"] - g["out"]
        g["avg"] = (g["out"] or g["in"]) / g["count"]
        del g["cats"]
        out.append(g)
    return sorted(out, key=lambda g: (-g["out"], -g["in"]))


def payee_detail(ctx: Ctx, name: str) -> dict:
    txns = [t for t in ctx.txns() if payee_key(t) == name]
    spent = [t for t in txns if t.amount < 0]
    history = []
    for off in range(-11, 1):
        start, end = ctx.period(off)
        history.append({"label": start.strftime("%b" if ctx.cycle.frequency in ("monthly", "calendar") else "%-d %b"), "start": start, "offset": off,
                        "value": -sum(t.amount for t in spent if start <= t.date < end)})
    first = min((t.date for t in txns), default=None)
    months = max(((ctx.today - first).days / 30.4) if first else 1, 1)
    cur_start, cur_end = ctx.period(0)
    return {
        "name": name, "txns": txns, "count": len(txns),
        "total_out": -sum(t.amount for t in spent), "total_in": sum(t.amount for t in txns if t.amount > 0),
        "avg": (-sum(t.amount for t in spent) / len(spent)) if spent else 0,
        "biggest": min(spent, key=lambda t: t.amount) if spent else None,
        "first": first, "last": max((t.date for t in txns), default=None),
        "per_month": len(spent) / months, "spend_per_month": -sum(t.amount for t in spent) / months,
        "this_period": -sum(t.amount for t in spent if cur_start <= t.date < cur_end),
        "history": history,
    }


# ---------- summary ----------
def period_summary(ctx: Ctx, offset: int) -> dict:
    start, end = ctx.period(offset)
    txns = ctx.txns(start, end)
    income = sum(t.amount for t in txns if t.amount > 0 and ctx.kind(t) == "income" and ctx.counts(t))
    spending = [t for t in txns if ctx.is_spending(t)]
    spent = -sum(t.amount for t in spending)
    bills = -sum(t.amount for t in spending if t.bill_id is not None)
    day_to_day = spent - bills

    by_cat = defaultdict(float)
    for t in spending:
        by_cat[(t.category_id, ctx.cat_name(t))] -= t.amount
    cats = sorted(((name, v, cid) for (cid, name), v in by_cat.items() if v > 0), key=lambda x: -x[1])

    # Purchases the bank hasn't settled yet count as spending on the day they were made
    from .forecast import pending_spending  # local import: forecast imports this module
    pending = pending_spending(ctx.s, ctx.accounts, start, end)
    pending_total = -sum(p.amount for p in pending)
    day_to_day += pending_total
    spent += pending_total

    days = (end - start).days
    elapsed = max(min((ctx.today - start).days + 1, days), 1)
    daily = []
    for i in range(days):
        d = start + timedelta(days=i)
        settled = -sum(t.amount for t in spending if t.date == d and t.bill_id is None)
        unsettled = -sum(p.amount for p in pending if p.date == d)
        billed = -sum(t.amount for t in spending if t.date == d and t.bill_id is not None)
        daily.append({"date": d, "future": d > ctx.today, "value": settled + unsettled, "pending": unsettled,
                      "bills": billed})
    purchases = [t for t in spending if t.amount < 0 and t.bill_id is None]
    payees = [g for g in payee_groups(ctx, spending) if g["out"] > 0]
    tags: dict[str, list] = {}
    for t in spending:
        for tag in {x.strip() for x in (t.tags or "").split(",") if x.strip()}:
            row = tags.setdefault(tag, [0.0, 0])
            row[0] -= t.amount
            row[1] += 1
    return {
        "start": start, "end": end, "last": end - timedelta(days=1), "days": days, "elapsed": elapsed, "offset": offset,
        "income": income, "spent": spent, "bills": bills, "day_to_day": day_to_day,
        "saved": income - spent, "rate": (income - spent) / income if income else None,
        "per_day": day_to_day / elapsed, "categories": cats, "daily": daily,
        "biggest": (big := min(purchases, key=lambda t: t.amount) if purchases else None),
        "biggest_payee": payee_key(big) if big else "",
        "count": len(purchases), "payees": payees[:6],
        "tags": sorted(((k, v, n) for k, (v, n) in tags.items() if v > 0.004), key=lambda r: -r[1]),
        "most_visited": max(payees, key=lambda g: g["count"]) if payees else None,
        "no_spend_days": sum(1 for d in daily if not d["future"] and d["value"] <= 0.004),
        "pending_total": pending_total, "pending_count": len(pending),
    }


def change(now: float, before: float) -> float | None:
    return (now - before) / before if before else None


# ---------- charts ----------
def donut(slices: list[tuple], size: int = 220, max_slices: int = 7) -> dict:
    """slices: (label, value[, href]). Returns {'svg': Markup, 'legend': [(label, value, pct, colour, href)]}."""
    slices = [(x[0], x[1], x[2] if len(x) > 2 else None) for x in slices if x[1] > 0]
    if len(slices) > max_slices:
        rest = sum(x[1] for x in slices[max_slices - 1:])
        slices = slices[:max_slices - 1] + [("Other", rest, None)]
    total = sum(x[1] for x in slices) or 1
    r, inner, c = size / 2, size / 2 * 0.62, size / 2
    parts, legend, angle = [], [], -math.pi / 2
    def wrap(shape: str, href: str | None, label: str) -> str:
        if not href:
            return shape
        return f'<a href="{escape(href)}" aria-label="{escape(label)}" class="seg">{shape}</a>'

    for i, (label, value, href) in enumerate(slices):
        colour = f"var({PALETTE[i % len(PALETTE)]})"
        frac = value / total
        legend.append((label, value, frac, colour, href))
        if frac >= 0.9999:
            parts.append(wrap(f'<circle cx="{c}" cy="{c}" r="{(r + inner) / 2}" fill="none" stroke="{colour}" '
                              f'stroke-width="{r - inner}"><title>{escape(label)}: 100%</title></circle>',
                              href, label))
            break
        a2 = angle + frac * 2 * math.pi
        large = 1 if frac > 0.5 else 0
        p = lambda rad, a: (c + rad * math.cos(a), c + rad * math.sin(a))  # noqa: E731
        (x1, y1), (x2, y2) = p(r, angle), p(r, a2)
        (x3, y3), (x4, y4) = p(inner, a2), p(inner, angle)
        parts.append(wrap(f'<path d="M{x1:.2f},{y1:.2f} A{r},{r} 0 {large} 1 {x2:.2f},{y2:.2f} '
                          f'L{x3:.2f},{y3:.2f} A{inner},{inner} 0 {large} 0 {x4:.2f},{y4:.2f} Z" '
                          f'fill="{colour}" stroke="var(--surface)" stroke-width="2">'
                          f'<title>{escape(label)}: {frac:.0%}</title></path>', href, label))
        angle = a2
    svg = (f'<svg viewBox="0 0 {size} {size}" role="img" aria-label="Breakdown chart" '
           f'style="width:100%;max-width:{size}px;height:auto">{"".join(parts)}</svg>')
    return {"svg": Markup(svg), "legend": legend}


def _short_money(v: float) -> str:
    return f"${v:,.0f}" if v >= 10 else f"${v:,.2f}"


def bars(values: list[float], labels: list[str], target: float | None = None,
         highlight: list[bool] | None = None, height: int = 160,
         hrefs: list[str | None] | None = None, axis: list[str] | None = None,
         show_values: bool = True, part: list[float] | None = None,
         trend: list[float | None] | None = None, stack: list[float] | None = None,
         stack_hrefs: list[str | None] | None = None, low: list[bool] | None = None,
         target_cls: str = "") -> Markup:
    """Column chart in HTML, so value labels can wrap, rotate on phones and blur in privacy mode.
    With `stack`, a second amount (bills) sits on top of each column in its own colour, with its own link."""
    n = max(len(values), 1)
    totals = [v + (stack[i] if stack else 0) for i, v in enumerate(values)]
    top = max(max(totals, default=0), target or 0, max((t for t in trend or [] if t is not None), default=0)) or 1
    if stack:
        # One big bill (rent) would squash every other day flat: scale to day-to-day spending instead,
        # and draw taller columns cut off at the top, still labelled with their real total.
        everyday = max(max(values, default=0), target or 0, max((t for t in trend or [] if t is not None), default=0))
        if everyday and top > everyday * 2.2:
            top = everyday * 1.6
    cols = []
    for i, v in enumerate(values):
        if stack:
            b = max(stack[i], 0)
            tot = max(v, 0) + b
            faded = highlight is not None and not highlight[i]
            val = (f'<span class="cc-val"><span class="m">{_short_money(tot)}</span></span>'
                   if show_values and tot > 0.004 else "")
            share = (part[i] / v * 100) if part and v > 0.004 else 0
            stripe = f'<span class="cc-part" style="height:{min(share, 100):.2f}%"></span>' if share > 0 else ""

            bar_amount = min(tot, top)  # what the drawn column stands for

            def seg(cls, amount, href, inner=""):
                if amount <= 0.004:
                    return ""
                if cls == "bills" and tot > top:  # the cut-off column: bills take what day to day leaves
                    amount = max(bar_amount - max(v, 0), bar_amount * .25)
                h = min(amount / bar_amount * 100, 100)
                tag = "a" if href else "span"
                attr = f' href="{escape(href)}"' if href else ""
                return f'<{tag} class="cc-seg {cls}"{attr} style="height:{h:.2f}%">{inner}</{tag}>'
            body = (seg("bills", b, stack_hrefs[i] if stack_hrefs else None)
                    + seg("d2d", max(v, 0), hrefs[i] if hrefs else None, stripe))
            cut = tot > top
            cols.append(f'<span class="cc-col" title="{escape(labels[i])}"><span class="cc-bar stack{" faded" if faded else ""}'
                        f'{" cut" if cut else ""}" style="height:{min(max(tot / top * 100, 0), 100):.2f}%">{val}{body}</span></span>')
            continue
        pct = max(v / top * 100, 0)
        faded = highlight is not None and not highlight[i]
        val = (f'<span class="cc-val"><span class="m">{_short_money(v)}</span></span>'
               if show_values and v > 0.004 else "")
        share = (part[i] / v * 100) if part and v > 0.004 else 0  # e.g. the pending part, drawn lighter on top
        stripe = f'<span class="cc-part" style="height:{min(share, 100):.2f}%"></span>' if share > 0 else ""
        flag = (" faded" if faded else "") + (" low" if low and low[i] else "")
        inner = (f'<span class="cc-bar{flag}" style="height:{pct:.2f}%">{stripe}{val}</span>')
        href = hrefs[i] if hrefs else None
        tag = "a" if href and v > 0 else "span"
        attr = f' href="{escape(href)}"' if tag == "a" else ""
        cols.append(f'<{tag} class="cc-col"{attr} title="{escape(labels[i])}">{inner}</{tag}>')
    line = ""
    if target:
        line = f'<span class="cc-target {target_cls}" style="--t:{min(target / top, 1):.4f}"></span>'
    if trend and any(t is not None for t in trend):
        # x runs 0..n so each point sits over the middle of its day's column; y is 0..100 from the bottom
        pts = [(i + .5, 100 - max(t, 0) / top * 100) for i, t in enumerate(trend) if t is not None]
        path = " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
        lx, ly = pts[-1]
        line += (f'<svg class="cc-trend" viewBox="0 0 {n} 100" preserveAspectRatio="none" aria-hidden="true">'
                 f'<polyline points="{path}"/></svg>'
                 f'<span class="cc-dot" style="left:calc({lx / n * 100:.3f}%);--y:{(100 - ly) / 100:.4f}"></span>')
    ax = axis or [lbl.split(":")[0] for lbl in labels]
    step = max(1, math.ceil(n / 10))
    ticks = "".join(f'<span>{escape(ax[i]) if i % step == 0 else ""}</span>' for i in range(n))
    dense = " dense" if n > 14 else ""
    return Markup(f'<div class="colchart{dense}" style="--n:{n};--h:{height}px">'
                  f'<div class="cc-plot">{line}{"".join(cols)}</div>'
                  f'<div class="cc-axis">{ticks}</div></div>')





# ---------- subscriptions ----------
FREQUENCIES = [("weekly", 5, 9), ("fortnightly", 12, 17), ("monthly", 26, 35), ("quarterly", 80, 100),
               ("yearly", 345, 385)]


def detect_subscriptions(ctx: "Ctx", bills: list, dismissed: set[str]) -> list[dict]:
    """Payees paid at a regular interval for a similar amount, that aren't bills yet."""
    from .bills import matches_pattern  # local import avoids a cycle
    since = ctx.today - timedelta(days=400)
    groups: dict[str, list[Transaction]] = defaultdict(list)
    for t in ctx.txns(since, None):
        if t.amount < 0 and t.bill_id is None and ctx.kind(t) != "transfer" and ctx.counts(t):
            groups[payee_key(t)].append(t)
    found = []
    for name, txns in groups.items():
        if len(txns) < 3 or name.lower() in dismissed:
            continue
        if any(matches_pattern(b.match_pattern, f"{txns[0].description} {txns[0].merchant}") for b in bills):
            continue
        txns.sort(key=lambda t: t.date)
        gaps = sorted((b.date - a.date).days for a, b in zip(txns, txns[1:]))
        gap = gaps[len(gaps) // 2]
        freq = next((f for f, lo, hi in FREQUENCIES if lo <= gap <= hi), None)
        if not freq:
            continue
        lo, hi = next((lo, hi) for f, lo, hi in FREQUENCIES if f == freq)
        if sum(1 for g in gaps if lo - 3 <= g <= hi + 3) < len(gaps) * 0.75:
            continue  # irregular timing
        amounts = sorted(-t.amount for t in txns)
        typical = amounts[len(amounts) // 2]
        if sum(1 for a in amounts if abs(a - typical) <= typical * 0.15) < len(amounts) * 0.8:
            continue  # amounts vary too much (e.g. groceries)
        last = txns[-1]
        if (ctx.today - last.date).days > hi * 2:
            continue  # looks stopped
        found.append({"name": name, "amount": typical, "frequency": freq, "count": len(txns),
                      "last": last.date, "next": last.date + timedelta(days=gap), "txn_id": last.id,
                      "category": ctx.cat_name(last)})
    return sorted(found, key=lambda f: -f["amount"])


# ---------- category limits ----------
LIMIT_PERIODS = 3


def category_spend(ctx: "Ctx", start: date, end: date, bills: bool = True) -> dict[int, float]:
    totals: dict[int, float] = defaultdict(float)
    for t in ctx.txns(start, end):
        if ctx.is_spending(t) and (bills or t.bill_id is None):
            totals[t.category_id] -= t.amount
    return totals


def category_averages(ctx: "Ctx", offset: int = 0, bills: bool = True) -> dict[int, float]:
    """Average spend per category over the LIMIT_PERIODS pay periods before the one at offset.
    bills=False leaves out payments matched to a bill, giving the day-to-day average."""
    history = [category_spend(ctx, *ctx.period(offset - k), bills=bills) for k in range(1, LIMIT_PERIODS + 1)]
    ids = {cid for h in history for cid in h}
    return {cid: sum(h.get(cid, 0) for h in history) / LIMIT_PERIODS for cid in ids}


def bills_due_by_category(ctx: "Ctx", offset: int = 0) -> dict[int, float]:
    """What your bills expect to take from each category in the pay period at offset, from their schedules.
    A bill without a category of its own is filed under Bills, as the categoriser does."""
    from .bills import occurrences  # local import keeps this module's imports small
    from .db import Bill
    start, end = ctx.period(offset)
    bills_cat = next((cid for cid, c in ctx.cats.items() if c.name == "Bills"), None)
    out: dict[int, float] = defaultdict(float)
    for b in ctx.s.exec(select(Bill).where(Bill.active == True)):  # noqa: E712
        cid = b.category_id or bills_cat
        if cid is not None:
            out[cid] += b.amount * len(occurrences(b, start, end))
    return out


def usual_spend(ctx: "Ctx", offset: int = 0) -> dict[int, float]:
    """The automatic limit for each category: its usual day-to-day spending, plus the bills due in it this
    period. Bills are taken from their schedules rather than averaged, so a quarterly or yearly bill landing
    this pay doesn't look like overspending, and isn't spread into pays where it isn't due."""
    usual = defaultdict(float, category_averages(ctx, offset, bills=False))
    for cid, amount in bills_due_by_category(ctx, offset).items():
        usual[cid] += amount
    return dict(usual)


def category_limits(ctx: "Ctx", offset: int = 0) -> dict[int, dict]:
    """{category_id: {"limit": amount, "mode": auto|fixed, "average": usual}} for the period at offset."""
    averages = usual_spend(ctx, offset)
    out = {}
    for cid, cat in ctx.cats.items():
        if cat.kind != "expense" or cat.limit_mode == "off":
            continue
        avg = averages.get(cid, 0)
        if cat.limit_mode == "fixed" and cat.limit_amount:
            out[cid] = {"limit": cat.limit_amount, "mode": "fixed", "average": avg}
        elif cat.limit_mode == "auto" and avg > 0.5:
            out[cid] = {"limit": round(avg, 2), "mode": "auto", "average": avg}
    return out


# ---------- review: categories that were only a guess ----------
GUESSED = ("akahu", "refund", "default")


def needs_review_query():
    """Transactions filed by a guess (Akahu's category, or Income / Uncategorised) that you haven't checked."""
    from sqlmodel import select as _select
    left_out = _select(Account.id).where(Account.count_spending == False)  # noqa: E712 - not part of the budget
    return _select(Transaction).where(Transaction.category_source.in_(GUESSED), Transaction.reviewed == False,  # noqa: E712
                                      Transaction.transfer_id == None, Transaction.bill_id == None,  # noqa: E711
                                      Transaction.account_id.not_in(left_out))


def review_groups(ctx: "Ctx") -> list[dict]:
    """The review list, one entry per payee, new payees first."""
    todo = ctx.s.exec(needs_review_query().order_by(Transaction.date.desc())).all()
    if not todo:
        return []
    todo_ids = {t.id for t in todo}
    # a payee is "new" if nothing else from them has been checked, set by you or matched by a rule
    known = {payee_key(t) for t in ctx.s.exec(_select_all()) if t.id not in todo_ids}
    groups: dict[str, dict] = {}
    for t in todo:
        g = groups.setdefault(payee_key(t), {"name": payee_key(t), "txns": [], "total": 0.0, "cats": defaultdict(int)})
        g["txns"].append(t)
        g["total"] += t.amount
        g["cats"][t.category_id] += 1
    out = []
    for g in groups.values():
        g["category_id"] = max(g["cats"].items(), key=lambda kv: kv[1])[0]
        g["guess"] = ctx.cats[g["category_id"]].name if g["category_id"] in ctx.cats else "Uncategorised"
        g["new"] = g["name"] not in known
        g["first"] = g["txns"][-1].date
        g["last"] = g["txns"][0].date
        g["key"] = hashlib.sha1(g["name"].encode()).hexdigest()[:10]
        del g["cats"]
        out.append(g)
    return sorted(out, key=lambda g: (not g["new"], -abs(g["total"])))


def _select_all():
    from sqlmodel import select as _select
    return _select(Transaction)
