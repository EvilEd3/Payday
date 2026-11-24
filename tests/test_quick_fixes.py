"""Payee names keep their initials; a new household gets its first steps on the Overview."""
import pytest

from app.text import nice_case
from test_security import _signed_in, _user


@pytest.mark.parametrize("raw, shown", [
    ("ANZ HOME LOAN", "ANZ Home Loan"), ("AA INSURANCE", "AA Insurance"), ("IRD", "IRD"),
    ("PAK N SAVE", "Pak N Save"), ("THE WAREHOUSE", "The Warehouse"), ("ONE NZ BROADBAND", "One NZ Broadband"),
    ("BP CONNECT", "BP Connect"), ("McDonald's", "McDonald's"),
])
def test_nice_case_keeps_initials(raw, shown):
    assert nice_case(raw) == shown


def test_new_household_sees_setup_steps(client):
    _signed_in(client, _user(client, "newcomer"))
    r = client.get("/")
    assert r.status_code == 200
    assert "Let’s set Payday up" in r.text
    assert 'href="/settings?tab=bank"' in r.text
    assert "a day until payday" not in r.text  # no $0.00 headline before there's anything to count
