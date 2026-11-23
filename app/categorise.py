import re
from collections import Counter, defaultdict
from datetime import timedelta

from sqlmodel import Session, select

from .bills import bill_matches, closest_free_due, matches_pattern
from .db import Bill, Category, Rule, Transaction


def get_or_create_category(session: Session, name: str, kind: str = "expense") -> Category:
    cat = session.exec(select(Category).where(Category.name == name)).first()
    if not cat:
        cat = Category(name=name, kind=kind, limit_mode="auto" if kind == "expense" else "off")
        session.add(cat)
        session.commit()
        session.refresh(cat)
    return cat


def rule_matches(rule: Rule, txn) -> bool:
    """Every condition that's set must match."""
    if not (rule.pattern or rule.direction != "any" or rule.min_amount is not None
            or rule.max_amount is not None or rule.account_id):
        return False  # an empty rule matches nothing
    if rule.pattern and not matches_pattern(rule.pattern, f"{txn.description} {txn.merchant}"):
        return False
    if rule.direction == "out" and txn.amount >= 0 or rule.direction == "in" and txn.amount <= 0:
        return False
    size = abs(txn.amount)
    if rule.min_amount is not None and size < rule.min_amount - 0.005:
        return False
    if rule.max_amount is not None and size > rule.max_amount + 0.005:
        return False
    return not (rule.account_id and txn.account_id != rule.account_id)


class Categoriser:
    """Categorises transactions against one snapshot of your categories, rules and bills.

    Order: transfer between your accounts > recurring bill > your rules > learned merchants >
    Akahu's own category > a refund from a payee you spend with > Income / Uncategorised. Anything you set by hand is left alone.

    Loading the lookups once, and tracking which bill due dates are already paid in memory, lets a
    full re-run over every transaction finish in a single database commit."""

    def __init__(self, session: Session):
        self.s = session
        self.cats = {c.name: c.id for c in session.exec(select(Category))}
        self.rules = sorted(session.exec(select(Rule)).all(),
                            key=lambda r: (0 if r.source == "user" else 1, r.priority, r.id or 0))
        self.bills = session.exec(select(Bill).where(Bill.active == True).order_by(Bill.id)).all()  # noqa: E712
        self.paid: dict[int, dict] = {}  # bill id -> {due date: id of the transaction that paid it}
        self._spent_with: dict[str, int] | None = None  # payee -> the expense category you usually spend there
        for t in session.exec(select(Transaction).where(Transaction.bill_id != None)):  # noqa: E711
            self.paid.setdefault(t.bill_id, {})[t.bill_due_date] = t.id

    def category(self, name: str, kind: str = "expense") -> int:
        if name not in self.cats:
            cat = Category(name=name, kind=kind, limit_mode="auto" if kind == "expense" else "off")
            self.s.add(cat)
            self.s.flush()
            self.cats[name] = cat.id
        return self.cats[name]

    def refund_category(self, txn: Transaction) -> int | None:
        """For money in: the expense category of a payee you've spent money with, so a refund or return
        takes the spending back off that category instead of counting as income. None for other payees."""
        if self._spent_with is None:
            counts: dict[str, Counter] = defaultdict(Counter)
            expense = {c.id for c in self.s.exec(select(Category).where(Category.kind == "expense"))}
            for t in self.s.exec(select(Transaction).where(Transaction.amount < 0, Transaction.bill_id == None,  # noqa: E711
                                                           Transaction.transfer_id == None)):  # noqa: E711
                if t.category_id in expense and t.category_source != "refund":
                    counts[payee_pattern(t).lower()][t.category_id] += 1
            self._spent_with = {k: c.most_common(1)[0][0] for k, c in counts.items() if c}
        key = payee_pattern(txn).lower()
        return self._spent_with.get(key) if key else None

    def _find_bill(self, txn: Transaction):
        if txn.amount >= 0:
            return None
        text = f"{txn.description} {txn.merchant}"
        for bill in self.bills:
            if bill_matches(bill, text, -txn.amount):
                taken = {d for d, tid in self.paid.get(bill.id, {}).items() if tid != txn.id}
                due = closest_free_due(bill, txn.date, taken)
                if due:
                    return bill, due
        return None

    def apply(self, txn: Transaction) -> None:
        if txn.category_source == "manual":
            return
        if txn.bill_id is not None:  # give its due date back before matching again
            dates = self.paid.get(txn.bill_id, {})
            if dates.get(txn.bill_due_date) == txn.id:
                del dates[txn.bill_due_date]
        txn.bill_id = txn.bill_due_date = None

        # Bills first: a payment that matches one of your bills counts as paying it, even when the money goes to
        # another of your own accounts (e.g. a monthly top-up of the account the mortgage comes out of).
        match = self._find_bill(txn)
        if match:
            bill, due = match
            self.paid.setdefault(bill.id, {})[due] = txn.id
            txn.bill_id, txn.bill_due_date = bill.id, due
            txn.category_id, txn.category_source = bill.category_id or self.category("Bills"), "bill"
            return

        if txn.transfer_id or txn.akahu_type == "TRANSFER":
            txn.category_id, txn.category_source = self.category("Transfer", "transfer"), "transfer"
            return

        for rule in self.rules:
            if rule_matches(rule, txn):
                txn.category_id, txn.category_source = rule.category_id, rule.source
                return

        if txn.amount > 0:
            refund_to = self.refund_category(txn)
            if refund_to:
                txn.category_id, txn.category_source = refund_to, "refund"
            else:
                txn.category_id, txn.category_source = self.category("Income", "income"), "default"
        elif txn.akahu_group:
            txn.category_id, txn.category_source = self.category(txn.akahu_group), "akahu"
        else:
            txn.category_id, txn.category_source = self.category("Uncategorised"), "default"


def categorise(session: Session, txn: Transaction) -> None:
    """Categorise one transaction. For many at once, build a Categoriser and call apply()."""
    Categoriser(session).apply(txn)


def payee_pattern(txn: Transaction) -> str:
    """Text that will match this payee's future transactions: the merchant name, or the description's words
    without the reference numbers that change every time (e.g. "000042732821 23248320")."""
    if txn.merchant.strip():
        return txn.merchant.strip()
    words = [w for w in re.split(r"\s+", txn.description) if w and not any(ch.isdigit() for ch in w)][:3]
    if not words:
        return txn.description.strip()
    plain = " ".join(words)
    if plain.lower() in txn.description.lower():
        return plain
    return "re:" + r".*".join(re.escape(w) for w in words)  # the words, with numbers allowed in between


def learn(session: Session, txn: Transaction, category_id: int) -> None:
    """Remember payee -> category and apply it to other transactions you haven't set by hand."""
    key = payee_pattern(txn)
    if not key:
        return
    rule = session.exec(select(Rule).where(Rule.source == "learned", Rule.pattern == key)).first()
    if rule:
        rule.category_id = category_id
    else:
        rule = Rule(pattern=key, category_id=category_id, source="learned")
    session.add(rule)
    session.commit()
    recategorise_all(session)


TRANSFER_DAYS = 5  # transfers between banks can take a few days, longer over a weekend


def _transfer_cat_ids(session: Session) -> set[int]:
    return {c.id for c in session.exec(select(Category).where(Category.kind == "transfer"))}


def _pairable(t: Transaction, transfer_cats: set[int]) -> bool:
    """Unlinked, and not something you've deliberately filed as a non-transfer."""
    return t.transfer_id is None and (t.category_source != "manual" or t.category_id in transfer_cats)


def find_partner(session: Session, txn: Transaction, max_days: int = TRANSFER_DAYS,
                 transfer_cats: set[int] | None = None) -> Transaction | None:
    """The opposite-signed, same-amount transaction in another of your accounts, closest in date."""
    transfer_cats = transfer_cats if transfer_cats is not None else _transfer_cat_ids(session)
    lo, hi = txn.date - timedelta(days=max_days), txn.date + timedelta(days=max_days)
    cands = session.exec(select(Transaction).where(
        Transaction.account_id != txn.account_id, Transaction.date >= lo, Transaction.date <= hi,
        Transaction.transfer_id == None)).all()  # noqa: E711
    cands = [c for c in cands if round(c.amount, 2) == round(-txn.amount, 2) and c.id != txn.id
             and _pairable(c, transfer_cats)]
    return min(cands, key=lambda c: abs((c.date - txn.date).days), default=None)


def link_transfer(session: Session, a: Transaction, b: Transaction, engine: "Categoriser | None" = None) -> None:
    engine = engine or Categoriser(session)
    a.transfer_id, b.transfer_id = b.id, a.id
    for t in (a, b):
        engine.apply(t)  # ones you filed by hand keep their (transfer) category
        session.add(t)


def pair_transfers(session: Session, max_days: int = TRANSFER_DAYS) -> int:
    """Mark money leaving one of your accounts and the same amount arriving in another as a transfer.
    Pairs the closest dates first, so two transfers of the same amount don't get crossed."""
    transfer_cats = _transfer_cat_ids(session)
    txns = [t for t in session.exec(select(Transaction).where(Transaction.transfer_id == None))  # noqa: E711
            if _pairable(t, transfer_cats)]
    incoming: dict[float, list[Transaction]] = {}
    for t in txns:
        if t.amount > 0:
            incoming.setdefault(round(t.amount, 2), []).append(t)
    options = []
    for out in txns:
        if out.amount >= 0:
            continue
        for cand in incoming.get(round(-out.amount, 2), []):
            gap = abs((cand.date - out.date).days)
            if cand.account_id != out.account_id and gap <= max_days:
                options.append((gap, out.date, out.id, out, cand))
    paired, engine = 0, None
    for *_, out, cand in sorted(options, key=lambda o: o[:3]):
        if out.transfer_id is None and cand.transfer_id is None:
            engine = engine or Categoriser(session)
            link_transfer(session, out, cand, engine)
            paired += 1
    session.commit()
    return paired


def recategorise_all(session: Session) -> None:
    """Re-run everything after a rule or bill changes. One pass, one commit."""
    pair_transfers(session)
    txns = session.exec(select(Transaction).order_by(Transaction.date, Transaction.id)).all()
    for t in txns:  # clear automatic bill links first so due dates can be re-assigned in date order
        if t.category_source != "manual":
            t.bill_id = t.bill_due_date = None
    session.flush()
    engine = Categoriser(session)  # sees only the links you made by hand
    for t in txns:
        engine.apply(t)
        session.add(t)
    session.commit()
