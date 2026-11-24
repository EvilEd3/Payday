"""Loans (a mortgage, a car loan, a personal loan): history, forecast to payoff, what-ifs, milestones and charts.

Interest is worked out the way NZ banks do it (and the way your spreadsheet does): daily, at the yearly rate ÷ 365,
on the balance owed, added up between repayments. Where the rate changes mid-way, each day uses the rate in force.

History is your imported rows, then repayments seen on the bank's loan account. The forecast starts from the last
repayment, using the bank's current balance when the loan account is connected.
"""
import csv
import io
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from dateutil.relativedelta import relativedelta
from markupsafe import Markup, escape
from sqlmodel import Session, delete, select

from .bills import _clean_pattern as clean_pattern, bill_matches
from .db import Account, Bill, Loan, LoanEntry, LoanRate, PropertyValue, Transaction


# ---------------------------------------------------------------- the maths
def rate_on(rates: list[tuple[date, float]], d: date) -> float:
    """% p.a. in force on day d. rates: (effective date, rate) sorted oldest first."""
    current = rates[0][1] if rates else 0.0
    for eff, r in rates:
        if eff <= d:
            current = r
        else:
            break
    return current


def interest_between(balance: float, start: date, end: date, rates: list[tuple[date, float]]) -> float:
    """Daily interest on a fixed balance from start (inclusive) to end (exclusive), splitting at rate changes."""
    total, d = 0.0, start
    cuts = sorted({eff for eff, _ in rates if start < eff < end} | {end})
    for cut in cuts:
        total += balance * rate_on(rates, d) / 100 * (cut - d).days / 365
        d = cut
    return total


FREQUENCIES = {"weekly": 52, "fortnightly": 26, "monthly": 12}  # repayments a year
KINDS = {"home": "Home loan", "car": "Car loan", "personal": "Personal loan", "other": "Other loan"}
ASSETS = {"home": "Home", "car": "Car", "other": "What it bought"}  # what each kind of loan pays for
DEFAULT_DEPRECIATION = {"car": 15.0}  # % a year: a common rule of thumb for a used car


def table_payment(balance: float, rate: float, periods: int, per_year: int = 12) -> float:
    """The standard repayment that clears balance over `periods` repayments at rate % p.a."""
    if periods <= 0:
        return balance
    r = rate / 100 / per_year
    return balance / periods if r == 0 else balance * r / (1 - (1 + r) ** -periods)


def periods_between(a: date, b: date, frequency: str = "monthly") -> int:
    """Repayments left from a to b."""
    if frequency == "monthly":
        return months_between(a, b)
    return max(math.ceil((b - a).days / (7 if frequency == "weekly" else 14)), 0)


def months_between(a: date, b: date) -> int:
    d = relativedelta(b, a)
    return max(d.years * 12 + d.months + (1 if d.days > 0 else 0), 0)


def payment_dates(after: date, day: int):
    """Monthly payment dates on `day` (last day of shorter months), strictly after `after`."""
    d = date(after.year, after.month, 1)
    while True:
        last = (d + relativedelta(months=1) - timedelta(days=1)).day
        due = date(d.year, d.month, min(day, last))
        if due > after:
            yield due
        d += relativedelta(months=1)


def repayment_dates(after: date, frequency: str = "monthly", day: int = 20, anchor: date | None = None):
    """Repayment dates strictly after `after`: monthly on `day`, or every week or fortnight from `anchor`."""
    if frequency not in ("weekly", "fortnightly") or not anchor:
        yield from payment_dates(after, day)
        return
    step = 7 if frequency == "weekly" else 14
    due = anchor + timedelta(days=((after - anchor).days // step + 1) * step)
    while True:
        yield due
        due += timedelta(days=step)


@dataclass
class Row:
    date: date
    payment: float
    interest: float
    principal: float
    balance: float
    rate: float
    kind: str  # actual | forecast
    extra: float = 0.0


@dataclass
class Plan:
    rows: list[Row] = field(default_factory=list)
    payoff: date | None = None
    interest: float = 0.0  # interest still to pay
    payments: float = 0.0
    payment_after_refix: float | None = None


def project(balance: float, since: date, rates: list[tuple[date, float]], payment: float, day: int,
            maturity: date, fixed_until: date | None, rate_after: float | None, payment_after: float | None,
            extra: float = 0.0, lump: float = 0.0, lump_date: date | None = None,
            frequency: str = "monthly", anchor: date | None = None) -> Plan:
    """Repayments from the one after `since` until the loan is paid off."""
    rates = sorted(rates)
    if fixed_until and rate_after is not None:
        rates = [(d, r) for d, r in rates if d < fixed_until] + [(fixed_until, rate_after)]
    plan, prev, owed, refixed = Plan(), since, balance, False
    per_year = FREQUENCIES.get(frequency, 12)
    for due in repayment_dates(since, frequency, day, anchor):
        if owed <= 0.005 or due > maturity + relativedelta(years=10):
            break
        if lump and lump_date and prev < lump_date <= due:
            owed = max(owed - lump, 0)
        raw = interest_between(owed, prev, due, rates)  # carried unrounded, like the bank's ledger
        interest = round(raw, 2)
        if fixed_until and not refixed and due > fixed_until:
            refixed = True
            payment = payment_after or table_payment(owed, rate_on(rates, due), periods_between(due, maturity, frequency) + 1,
                                                     per_year)
            plan.payment_after_refix = round(payment, 2)
        pay = round(payment + extra, 2)
        if owed + raw <= pay + 0.005:
            pay = round(owed + raw, 2)
        principal = round(pay - interest, 2)
        owed -= pay - raw
        plan.rows.append(Row(due, pay, interest, principal, max(round(owed, 2), 0), rate_on(rates, due), "forecast", extra))
        plan.interest += interest
        plan.payments += pay
        prev = due
        if principal <= 0 and not extra:  # repayment doesn't cover interest: would never finish
            break
    if plan.rows and plan.rows[-1].balance <= 0.005:
        plan.payoff = plan.rows[-1].date
    return plan


# ---------------------------------------------------------------- loading
def loan_meta(acc: Account | None) -> dict:
    """Loan details Akahu passes on from the bank (rate, fixed-term end, next repayment), if any."""
    try:
        return (json.loads(acc.meta or "{}").get("loan_details") or {}) if acc else {}
    except ValueError:
        return {}


def _d(v) -> date | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date() if v else None
    except ValueError:
        return None


def history(s: Session, loan: Loan, rates: list[tuple[date, float]]) -> list[Row]:
    """Imported rows, then repayments on the bank's loan account after the last imported one."""
    rows = [Row(e.date, e.payment, e.interest, e.principal, e.balance, e.rate or rate_on(rates, e.date), "actual")
            for e in s.exec(select(LoanEntry).where(LoanEntry.loan_id == loan.id).order_by(LoanEntry.date))]
    if not loan.account_id:
        return rows
    last = rows[-1] if rows else None
    prev_date = last.date if last else (loan.start_date or date.min)
    owed = last.balance if last else loan.original_amount
    txns = s.exec(select(Transaction).where(Transaction.account_id == loan.account_id, Transaction.date > prev_date)
                  .order_by(Transaction.date, Transaction.seq, Transaction.id)).all()
    for t in [t for t in txns if t.amount > 0]:  # repayments
        # interest and fees the bank charged since the last repayment, including the same day in either order
        charged = [c for c in txns if c.amount < 0 and prev_date < c.date <= t.date]
        charges = -sum(c.amount for c in charged)
        interest = round(charges, 2) if charged else round(interest_between(owed, prev_date, t.date, rates), 2)
        principal = round(t.amount - interest, 2)
        owed = round(owed - principal, 2)
        rows.append(Row(t.date, t.amount, interest, principal, owed, rate_on(rates, t.date), "actual"))
        prev_date = t.date
    return rows


@dataclass
class WhatIf:
    extra: float = 0.0
    lump: float = 0.0
    lump_date: date | None = None
    refix_rate: float | None = None

    @property
    def active(self) -> bool:
        return bool(self.extra or self.lump or self.refix_rate is not None)


def maturity_of(loan: Loan, meta: dict, today: date) -> date:
    start = loan.start_date or today
    if _d(meta.get("matures_at")):
        return _d(meta.get("matures_at"))
    if loan.term_months:
        return start + relativedelta(months=loan.term_months)
    return start + relativedelta(years=loan.term_years)


def schedule_anchor(loan: Loan, repay_meta: dict, hist: list[Row]) -> date | None:
    """A known repayment date, for weekly or fortnightly repayments: yours, the bank's next one, or the last seen."""
    return loan.repay_date or _d(repay_meta.get("next_date")) or (hist[-1].date if hist else None) or loan.start_date


def schedule_text(loan: Loan) -> str:
    if loan.frequency == "weekly":
        return "Repaid every week"
    if loan.frequency == "fortnightly":
        return "Repaid every fortnight"
    d = loan.payment_day
    suffix = "st" if d in (1, 21, 31) else "nd" if d in (2, 22) else "rd" if d in (3, 23) else "th"
    return f"Repaid on the {d}{suffix} of each month"


def per_repayment(loan: Loan) -> str:
    return {"weekly": "week", "fortnightly": "fortnight"}.get(loan.frequency, "month")


# ---------------------------------------------------------------- what it bought
def depreciation_of(loan: Loan) -> float:
    return loan.depreciation if loan.depreciation is not None else 0.0


def asset_points(loan: Loan, values: list[PropertyValue]) -> list[tuple[date, float]]:
    """Valuations you've added, oldest first. A car with none starts at what you paid (loan plus deposit) on the
    day the loan started. A home with none isn't counted in net worth (the Loans page still shows the price paid)."""
    if not loan.count_asset or loan.kind == "personal":
        return []
    pts = sorted((v.date, v.value) for v in values)
    if not pts and loan.kind != "home" and loan.start_date and (loan.original_amount + loan.deposit) > 0:
        pts = [(loan.start_date, loan.original_amount + loan.deposit)]
    return pts


def value_on(loan: Loan, points: list[tuple[date, float]], d: date) -> float | None:
    """What it's worth on day d: the latest value before then, less the yearly loss in value since."""
    known = [p for p in points if p[0] <= d]
    if not known:
        return None
    when, value = known[-1]
    rate = depreciation_of(loan)
    if rate:
        value *= (1 - min(rate, 99) / 100) ** ((d - when).days / 365.25)
    return value


def asset_series(loan: Loan, points: list[tuple[date, float]], end: date) -> list[tuple[date, float]]:
    """Points to draw its value from the first valuation to `end`, monthly where it's losing value."""
    if not points:
        return []
    if not depreciation_of(loan):
        return points + [(max(end, points[-1][0]), points[-1][1])]
    out, d = [], points[0][0]
    while d < end:
        out.append((d, value_on(loan, points, d)))
        d += relativedelta(months=1)
    out.append((end, value_on(loan, points, end)))
    return sorted(set(out) | {(p, value_on(loan, points, p)) for p, _ in points})


def summary(s: Session, loan: Loan, today: date, what: WhatIf | None = None) -> dict:
    acc = s.get(Account, loan.account_id) if loan.account_id else None
    meta = loan_meta(acc)
    interest_meta = meta.get("interest") or {}
    repay_meta = meta.get("repayment") or {}

    rates = sorted((r.effective, r.rate) for r in s.exec(select(LoanRate).where(LoanRate.loan_id == loan.id)))
    bank_rate = interest_meta.get("rate")
    if isinstance(bank_rate, (int, float)) and (not rates or abs(rate_on(rates, today) - bank_rate) > 1e-6):
        rates.append((today, float(bank_rate)))  # the bank reports a rate we don't have yet: use it from today
    if not rates:
        rates = [(loan.start_date or today, 0.0)]
    fixed_until = loan.fixed_until or _d(interest_meta.get("expires_at"))
    maturity = maturity_of(loan, meta, today)
    hist = history(s, loan, rates)
    anchor = schedule_anchor(loan, repay_meta, hist)
    per_year = FREQUENCIES.get(loan.frequency, 12)

    # where the forecast starts: the last repayment, with the bank's balance if it's connected
    since = hist[-1].date if hist else (loan.start_date or today)
    owed = hist[-1].balance if hist else loan.original_amount
    bank_owed = -acc.balance_current if acc and acc.balance_current < 0 else None
    if bank_owed is not None:
        owed = bank_owed
    payment = loan.repayment \
        or (repay_meta.get("next_amount") if isinstance(repay_meta.get("next_amount"), (int, float)) else None) \
        or (hist[-1].payment if hist else None) \
        or table_payment(owed, rate_on(rates, today), periods_between(today, maturity, loan.frequency), per_year)
    payment = float(payment)

    def run(w: WhatIf) -> Plan:
        after = w.refix_rate if w.refix_rate is not None else loan.rate_after
        pay_after = None if w.refix_rate is not None else loan.payment_after
        return project(owed, since, rates, payment, loan.payment_day, maturity, fixed_until, after, pay_after,
                       w.extra, w.lump, w.lump_date, loan.frequency, anchor)

    base = run(WhatIf())
    alt = run(what) if what and what.active else None

    paid = {"payments": sum(r.payment for r in hist), "interest": sum(r.interest for r in hist),
            "principal": sum(r.principal for r in hist)}
    values = sorted(s.exec(select(PropertyValue).where(PropertyValue.loan_id == loan.id)).all(), key=lambda v: v.date)
    points = asset_points(loan, values)
    value = value_on(loan, points, today)
    if value is None and loan.kind == "home" and loan.count_asset:
        value = loan.original_amount + loan.deposit  # the price paid, until you add a valuation
    has_asset = bool(value)
    equity = (value - owed) if has_asset else 0.0
    rate_now = rate_on(rates, today)

    return {
        "loan": loan, "account": acc, "meta": meta, "rates": rates, "rate_now": rate_now, "fixed_until": fixed_until,
        "maturity": maturity, "history": hist, "base": base, "alt": alt, "what": what or WhatIf(),
        "owed": owed, "bank_owed": bank_owed, "since": since, "payment": payment, "paid": paid,
        "has_asset": has_asset, "asset_name": ASSETS.get(loan.kind, "What it bought"), "kind_name": KINDS.get(loan.kind, "Loan"),
        "value": value or 0.0, "values": values, "asset_points": points, "depreciation": depreciation_of(loan),
        "equity": equity, "equity_pct": equity / value if has_asset else 0,
        "lvr": owed / value if has_asset else 0, "per_day": owed * rate_now / 100 / 365,
        "next": base.rows[0] if base.rows else None, "schedule": schedule_text(loan), "per": per_repayment(loan),
        "refix_days": (fixed_until - today).days if fixed_until and fixed_until >= today else None,
        "repay_meta": repay_meta, "milestones": milestones(loan, hist, base, points, value or 0.0, today),
        "year_interest": sum(r.interest for r in hist + base.rows if r.date.year == today.year),
    }


def milestones(loan: Loan, hist: list[Row], plan: Plan, points: list[tuple[date, float]], value: float,
               today: date) -> list[dict]:
    rows = hist + plan.rows
    out = []

    def first(label: str, test, about: str = ""):
        hit = next((r for r in rows if test(r)), None)
        if hit:
            out.append({"label": label, "date": hit.date, "done": hit.date <= today, "about": about})

    start = loan.original_amount or (rows[0].balance + rows[0].principal if rows else 0)
    for mark in (1_000_000, 750_000, 500_000, 250_000):
        if start > mark:
            first(f"Less than ${mark:,.0f} owing", lambda r, m=mark: r.balance < m)
    if loan.kind == "home" and value:
        first("20% equity (loan-to-value 80%)", lambda r: r.balance <= value * 0.8,
              "Usually the point where low-equity fees stop.")
        first("50% equity", lambda r: r.balance <= value * 0.5)
    elif points:
        name = ASSETS.get(loan.kind, "it").lower()
        first(f"The {name} is worth more than you owe",
              lambda r: (value_on(loan, points, r.date) or 0) >= r.balance,
              "Until then, selling it wouldn’t clear the loan.")
    if start:
        first("Halfway: half the loan repaid", lambda r: r.balance <= start / 2)
    first("Each repayment is more principal than interest", lambda r: r.principal > r.interest)
    if plan.payoff:
        out.append({"label": "Mortgage-free" if loan.kind == "home" else "Paid off", "date": plan.payoff,
                    "done": False, "about": ""})
    return sorted(out, key=lambda m: m["date"])


# ---------------------------------------------------------------- spreadsheet import
def _money(v: str) -> float | None:
    v = (v or "").replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        return float(v) if v else None
    except ValueError:
        return None


def _date(v: str) -> date | None:
    v = (v or "").strip()
    for fmt in ("%a, %d %b %Y", "%d %b %Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def parse_tracker(text: str) -> dict:
    """Read a mortgage tracker spreadsheet (downloaded as CSV): loan details, rate history and actual repayments.
    Forecast rows are skipped; Payday makes its own."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    out = {"details": {}, "rates": [], "entries": [], "values": []}
    labels = {"original loan amount": "original_amount", "loan term (years)": "term_years",
              "loan start date": "start_date", "original deposit/down payment": "deposit",
              "original property value": "original_value", "current property value": "current_value"}
    section = ""
    for r in rows:
        if not r or not any(c.strip() for c in r):
            continue
        head = r[0].strip().lower()
        if head in ("interest rate history", "monthly payment tracking & running totals", "key calculations"):
            section = head
            continue
        if head in labels:
            key = labels[head]
            if key == "start_date":
                out["details"][key] = _date(r[1])
            elif key == "current_value":
                when = next((_date(c.replace("Dated:", "")) for c in r[2:] if "dated" in c.lower()), None)
                out["details"][key] = (_money(r[1]), when)
            else:
                out["details"][key] = _money(r[1])
        elif section == "interest rate history" and _date(r[0]):
            rate = _money(r[1] if len(r) > 1 else "")
            if rate is not None:
                out["rates"].append((_date(r[0]), rate, r[2].strip() if len(r) > 2 else ""))
        elif head == "actual" and len(r) >= 9:
            d, rate, pay, intr, prin, bal = _date(r[1]), _money(r[3]), _money(r[4]), _money(r[5]), _money(r[6]), _money(r[8])
            if d and pay is not None and bal is not None:
                out["entries"].append({"date": d, "rate": rate, "payment": pay, "interest": intr or 0.0,
                                       "principal": prin if prin is not None else pay - (intr or 0.0), "balance": bal})
    return out


def import_tracker(s: Session, loan: Loan, parsed: dict) -> int:
    """Replace the loan's imported history with the spreadsheet's. Returns how many repayments were loaded."""
    d = parsed["details"]
    for key in ("original_amount", "deposit"):
        if d.get(key) is not None:
            setattr(loan, key, d[key])
    if d.get("term_years"):
        loan.term_years = int(d["term_years"])
    if d.get("start_date"):
        loan.start_date = d["start_date"]
    if parsed["entries"]:
        days = [e["date"].day for e in parsed["entries"][-6:]]
        loan.payment_day = max(set(days), key=days.count)
    s.add(loan)
    s.exec(delete(LoanEntry).where(LoanEntry.loan_id == loan.id, LoanEntry.source == "import"))
    for e in parsed["entries"]:
        s.add(LoanEntry(loan_id=loan.id, source="import", **e))
    if parsed["rates"]:
        s.exec(delete(LoanRate).where(LoanRate.loan_id == loan.id))
        for eff, rate, note in parsed["rates"]:
            s.add(LoanRate(loan_id=loan.id, effective=eff, rate=rate, note=note))
    have = {(v.date, v.value) for v in s.exec(select(PropertyValue).where(PropertyValue.loan_id == loan.id))}
    if d.get("original_value") and loan.start_date and (loan.start_date, d["original_value"]) not in have:
        s.add(PropertyValue(loan_id=loan.id, date=loan.start_date, value=d["original_value"], note="When you bought"))
    cur = d.get("current_value")
    if cur and cur[0] and cur[1] and (cur[1], cur[0]) not in have:
        s.add(PropertyValue(loan_id=loan.id, date=cur[1], value=cur[0], note="From your spreadsheet"))
    s.commit()
    return len(parsed["entries"])


# ---------------------------------------------------------------- charts
def _nice_step(span: float, ticks: int = 4) -> float:
    raw = span / ticks if span > 0 else 1
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)


def _short(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.2f}m".replace(".00m", "m")
    if abs(v) >= 1000:
        return f"${v / 1000:,.0f}k"
    return f"${v:,.0f}"


def line_chart(series: list[dict], height: int = 240, marker: date | None = None, y_fmt=_short,
               label: str = "Chart") -> Markup:
    """series: {"name", "points": [(date, value)], "cls": colour class, "dashed": bool, "fill": bool}.
    Lines are SVG stretched to the width; the labels are HTML, so text stays crisp and blurs in privacy mode."""
    pts = [p for s in series for p in s["points"]]
    if not pts:
        return Markup("")
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    span = max((x1 - x0).days, 1)
    top_raw = max(p[1] for p in pts) or 1
    step = _nice_step(top_raw)
    top = math.ceil(top_raw / step) * step

    def x(d):  # 0..1000
        return (d - x0).days / span * 1000

    def y(v):  # 0..100, from the top
        return 100 - max(v, 0) / top * 100

    parts = []
    for s_ in series:
        p = s_["points"]
        if len(p) < 2:
            continue
        path = " ".join(f"{'M' if i == 0 else 'L'}{x(d):.1f},{y(v):.2f}" for i, (d, v) in enumerate(p))
        if s_.get("fill"):
            parts.append(f'<path class="mc-fill {s_["cls"]}" d="{path} L{x(p[-1][0]):.1f},100 L{x(p[0][0]):.1f},100 Z"/>')
        parts.append(f'<path class="mc-line {s_["cls"]}{" dashed" if s_.get("dashed") else ""}" d="{path}"/>')
    if marker and x0 <= marker <= x1:
        parts.append(f'<line class="mc-now" x1="{x(marker):.1f}" x2="{x(marker):.1f}" y1="0" y2="100"/>')
    grid = "".join(f'<span class="mc-y" style="bottom:{v / top * 100:.2f}%"><span class="m">{escape(y_fmt(v))}</span></span>'
                   for v in [step * i for i in range(0, int(top / step) + 1)])
    years = list(range(x0.year + (0 if x0.month == 1 and x0.day == 1 else 1), x1.year + 1))
    every = max(1, math.ceil(len(years) / 7))
    xt = "".join(f'<span class="mc-x" style="left:{x(date(yr, 1, 1)) / 10:.2f}%">{yr}</span>'
                 for i, yr in enumerate(years) if i % every == 0)
    now = (f'<span class="mc-nowlbl" style="left:{x(marker) / 10:.2f}%">Now</span>'
           if marker and x0 <= marker <= x1 else "")
    return Markup(f'<div class="mchart" style="--h:{height}px" role="img" aria-label="{escape(label)}">'
                  f'<div class="mc-plot">{grid}{now}<svg viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden="true">'
                  f'{"".join(parts)}</svg></div><div class="mc-axis">{xt}</div></div>')


def yearly_split(rows: list[Row], today: date, height: int = 200) -> Markup:
    """Interest and principal paid in each year of the loan, stacked. Years still to come are lighter."""
    years: dict[int, list[float]] = {}
    for r in rows:
        y = years.setdefault(r.date.year, [0.0, 0.0])
        y[0] += r.interest
        y[1] += r.principal
    if not years:
        return Markup("")
    top = max(i + p for i, p in years.values()) or 1
    cols, ticks = [], []
    n = len(years)
    every = max(1, math.ceil(n / 8))
    for k, (yr, (i, p)) in enumerate(sorted(years.items())):
        future = " future" if yr > today.year else (" now" if yr == today.year else "")
        tip = f"{yr}: interest ${i:,.0f}, principal ${p:,.0f}"
        cols.append(f'<span class="ys-col{future}" title="{escape(tip)}">'
                    f'<span class="ys-p" style="height:{p / top * 100:.2f}%"></span>'
                    f'<span class="ys-i" style="height:{i / top * 100:.2f}%"></span></span>')
        ticks.append(f'<span>{yr if k % every == 0 else ""}</span>')
    return Markup(f'<div class="ysplit" style="--n:{n};--h:{height}px" role="img" aria-label="Interest and principal by year">'
                  f'<div class="ys-plot">{"".join(cols)}</div><div class="ys-axis">{"".join(ticks)}</div></div>')


# ---------------------------------------------------------------- the repayment as a bill
def paid_from(s: Session, loan: Loan, limit: int = 12) -> list[Transaction]:
    """Money out of your other accounts that paid this loan, newest first: the other half of each repayment."""
    if not loan.account_id:
        return []
    ins = s.exec(select(Transaction).where(Transaction.account_id == loan.account_id, Transaction.amount > 0)
                 .order_by(Transaction.date.desc()).limit(limit)).all()
    out = []
    for t in ins:
        other = s.get(Transaction, t.transfer_id) if t.transfer_id else None
        if other is None:  # not paired as a transfer: the same amount leaving another account around then
            other = s.exec(select(Transaction).where(
                Transaction.account_id != loan.account_id, Transaction.amount == -t.amount,
                Transaction.date >= t.date - timedelta(days=4), Transaction.date <= t.date + timedelta(days=1))).first()
        if other is not None and other.amount < 0 and other.id not in {o.id for o in out}:
            out.append(other)
    return out


def repayment_bill(s: Session, loan: Loan, paid: list[Transaction] | None = None) -> tuple[Bill | None, bool]:
    """The bill that sets this loan's repayments aside, and whether you've linked it (False: it only looks like it)."""
    if loan.bill_id:
        bill = s.get(Bill, loan.bill_id)
        if bill:
            return bill, True
    paid = paid_from(s, loan) if paid is None else paid
    for t in paid:
        if t.bill_id and s.get(Bill, t.bill_id):
            return s.get(Bill, t.bill_id), False
    for bill in s.exec(select(Bill).where(Bill.active == True).order_by(Bill.id)):  # noqa: E712
        if any(bill_matches(bill, f"{t.description} {t.merchant}", -t.amount) for t in paid):
            return bill, False
    return None, False


def bill_from_repayments(loan: Loan, payment: float, first_due: date, txn: Transaction, category_id: int | None) -> Bill:
    """A bill for the repayment, matched on the text of the last payment out of your everyday account."""
    return Bill(name=loan.name[:80], amount=round(payment, 2), frequency=loan.frequency if loan.frequency in FREQUENCIES
                else "monthly", anchor_date=first_due, match_pattern=clean_pattern(txn), category_id=category_id,
                account_id=txn.account_id)
