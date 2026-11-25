"""Tapping or hovering "Bank data from ..." on the Overview shows the same sync details as the footer."""
import re
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app import auth
from app.db import Account, engine_for, set_setting, site_engine
from test_security import _signed_in, _user


def test_header_pill_shows_full_sync_details(client):
    user = _user(client, "synced")
    with Session(site_engine) as s:
        pid = s.exec(select(auth.Membership).where(auth.Membership.user_id == user.id)).first().profile_id
    now = datetime.now(timezone.utc).replace(microsecond=0)
    iso = lambda h: (now - timedelta(hours=h)).isoformat().replace("+00:00", "Z")  # noqa: E731
    with Session(engine_for(pid)) as s:
        s.add(Account(id="sd-everyday", name="Everyday", type="CHECKING", balance_current=500,
                      refreshed_txns=iso(1), updated_at=now))
        s.add(Account(id="sd-savings", name="Savings", type="SAVINGS", balance_current=900,
                      refreshed_txns=iso(30), updated_at=now))
        set_setting(s, "last_sync", "Wed 25 Nov 07:07")
        set_setting(s, "last_sync_status", "Finished: 0 new transactions.")
        s.commit()
    _signed_in(client, user)
    try:
        html = client.get("/").text
    finally:  # the profile is shared with other tests that expect a new household
        with Session(engine_for(pid)) as s:
            for acc_id in ("sd-everyday", "sd-savings"):
                s.delete(s.get(Account, acc_id))
            s.commit()

    tip = re.search(r'<span id="fresh-tip" class="tip" role="tooltip">(.*?)</span>', html, re.S)
    assert tip, "the pill has no sync details"
    text = tip.group(1)
    assert text.startswith("Synced Wed 25 Nov 07:07, bank data from ")
    assert "(Savings from " in text and text.endswith("Finished: 0 new transactions.")
    assert 'data-freshtip' in html and 'aria-controls="fresh-tip"' in html
    footer = re.search(r'<div class="statusline">\s*<span>(.*?)\s*<button', html, re.S).group(1)
    assert footer == text  # header and footer say the same thing
