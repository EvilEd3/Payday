"""Transactions split across categories.

The transaction itself stays as the bank sent it; its parts are kept beside it. Totals by category (the Overview,
Summary, Trends and limits) count each part in its own category, using pieces: stand-ins for the transaction
that carry one part's amount and category and everything else from the transaction."""
from sqlmodel import Session, delete, select

from .db import Split, Transaction


class Piece:
    """One part of a split transaction, standing in for it in totals."""

    def __init__(self, txn: Transaction, part: Split):
        self._txn, self.amount, self.category_id, self.part = txn, part.amount, part.category_id, part
        self.split_of = txn

    def __getattr__(self, name):
        return getattr(self._txn, name)


def parts_for(session: Session, txn_ids) -> dict[str, list[Split]]:
    ids = list(txn_ids)
    out: dict[str, list[Split]] = {}
    for i in range(0, len(ids), 500):
        for p in session.exec(select(Split).where(Split.txn_id.in_(ids[i:i + 500])).order_by(Split.id)):
            out.setdefault(p.txn_id, []).append(p)
    return out


def adds_up(txn: Transaction, parts: list[Split]) -> bool:
    return len(parts) >= 2 and abs(sum(p.amount for p in parts) - txn.amount) < 0.005


def expand(session: Session, txns: list) -> list:
    """The transactions, with each split one replaced by its parts. Parts that no longer add up to their
    transaction (the bank changed the amount) are ignored, and the transaction counts whole."""
    parts = parts_for(session, [t.id for t in txns])
    if not parts:
        return list(txns)
    out = []
    for t in txns:
        ps = parts.get(t.id)
        if ps and adds_up(t, ps):
            out.extend(Piece(t, p) for p in ps)
        else:
            out.append(t)
    return out


def clear(session: Session, txn_id: str) -> None:
    session.exec(delete(Split).where(Split.txn_id == txn_id))


def move(session: Session, old_id: str, new_id: str) -> None:
    for p in session.exec(select(Split).where(Split.txn_id == old_id)):
        p.txn_id = new_id
        session.add(p)
