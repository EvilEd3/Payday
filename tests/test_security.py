import time

import pytest
from sqlmodel import Session, select
from starlette.requests import Request

from app import auth, mailer, main, webpush
from app.bills import matches_pattern, valid_pattern
from app.db import PushDevice, User, site_engine


def _request(peer: str, headers: dict | None = None, scheme: str = "http") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw, "query_string": b"",
                    "client": (peer, 1234), "scheme": scheme, "server": ("testserver", 80)})


def _user(client, name: str, email: str | None = None, owner: bool = False) -> User:
    with Session(site_engine) as s:
        found = s.exec(select(User).where(User.username == name)).first()
        if found:
            s.expunge(found)
            return found
        user = auth.create_user(s, name, "correct horse battery", owner=owner)
        user.email, user.email_verified, user.totp_enabled = email, bool(email), True
        user.totp_secret = ""
        s.add(user)
        s.commit()
        s.refresh(user)
        prof = s.exec(select(auth.Profile)).first()
        if not prof:
            auth.create_profile(s, "Home", user)
        else:
            s.add(auth.Membership(user_id=user.id, profile_id=prof.id, role="admin"))
            s.commit()
        s.refresh(user)
        s.expunge(user)
        return user


def _signed_in(client, user: User):
    client.cookies.set(auth.COOKIE, auth.make_cookie(user))


# ---------- forwarded headers are only believed from a trusted proxy ----------
def test_client_ip_ignores_forwarded_headers_from_the_internet():
    req = _request("203.0.113.9", {"CF-Connecting-IP": "1.2.3.4", "X-Forwarded-For": "5.6.7.8"})
    assert main.client_ip(req) == "203.0.113.9"


def test_client_ip_uses_forwarded_headers_from_a_trusted_proxy():
    assert main.client_ip(_request("172.18.0.2", {"CF-Connecting-IP": "1.2.3.4"})) == "1.2.3.4"
    assert main.client_ip(_request("127.0.0.1", {"X-Forwarded-For": "9.9.9.9, 1.2.3.4"})) == "1.2.3.4"


def test_client_ip_ignores_junk_in_forwarded_headers():
    assert main.client_ip(_request("127.0.0.1", {"CF-Connecting-IP": "not-an-ip"})) == "127.0.0.1"


def test_is_https_only_trusts_proxy_headers_from_a_proxy():
    assert not main.is_https(_request("203.0.113.9", {"X-Forwarded-Proto": "https"}))
    assert main.is_https(_request("10.0.0.5", {"X-Forwarded-Proto": "https"}))
    assert main.is_https(_request("203.0.113.9", scheme="https"))


# ---------- sign-in limits ----------
def test_flooding_junk_names_does_not_reset_an_accounts_limit():
    req = _request("203.0.113.9")
    for _ in range(main.MAX_ACCOUNT_FAILS):
        main._failed(req, "victim")
    assert main._blocked(_request("198.51.100.1"), "victim")
    now = time.time()
    for i in range(main.MAX_KEYS + 10):  # what an attacker sending junk names from many addresses builds up
        main._failures[f"198.51.{i // 250}.{i % 250}|junk{i}"] = [now]
    main._recent_failures("trigger")  # the next lookup trims the table
    assert len(main._failures) <= main.MAX_KEYS
    assert main._blocked(_request("198.51.100.2"), "victim")


def test_failed_sign_in_does_not_sleep():
    started = time.monotonic()
    main._failed(_request("203.0.113.9"), "someone")
    assert time.monotonic() - started < 0.5


# ---------- emailed links never come from an untrusted Host header ----------
def test_forgot_password_ignores_a_strangers_host_header(client):
    _user(client, "resetme", "resetme@example.com")
    before = len(mailer.sent)
    r = client.post("/forgot", data={"email": "resetme@example.com"}, headers={"Host": "evil.example"})
    assert r.status_code == 200
    assert len(mailer.sent) == before  # no email with a link to evil.example


def test_forgot_password_link_uses_a_local_address(client):
    _user(client, "resetme", "resetme@example.com")
    before = len(mailer.sent)
    client.post("/forgot", data={"email": "resetme@example.com"}, headers={"Host": "192.168.1.20:8000"})
    assert len(mailer.sent) == before + 1
    body = mailer.sent[-1].get_content()
    assert "http://192.168.1.20:8000/reset/" in body


def test_email_link_prefers_public_url(monkeypatch):
    monkeypatch.setattr(main, "PUBLIC_URL", "https://payday.example.nz")
    assert main._email_link(_request("203.0.113.9", {"Host": "evil.example"}), "/reset/x") == \
        "https://payday.example.nz/reset/x"


# ---------- cross-site changes and read-only members ----------
def test_cross_site_posts_are_refused(client):
    r = client.post("/forgot", data={"email": "x@example.com"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = client.post("/forgot", data={"email": "x@example.com"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


# ---------- push subscriptions ----------
@pytest.mark.parametrize("endpoint,ok", [
    ("https://fcm.googleapis.com/fcm/send/abc", True),
    ("https://updates.push.services.mozilla.com/wpush/v2/abc", True),
    ("https://web.push.apple.com/abc", True),
    ("https://db5p.notify.windows.com/w/?token=abc", True),
    ("https://10.0.0.1/admin", False),
    ("https://localhost/x", False),
    ("http://fcm.googleapis.com/fcm/send/abc", False),
    ("https://fcm.googleapis.com:8443/x", False),
    ("https://fcm.googleapis.com.evil.example/x", False),
    ("https://user@fcm.googleapis.com/x", False),
    ("https://evilpush.apple.com.example/x", False),
])
def test_push_endpoint_must_be_a_known_push_service(endpoint, ok):
    assert webpush.endpoint_ok(endpoint) is ok


def test_push_send_refuses_unknown_hosts():
    with pytest.raises(webpush.Gone):
        webpush.send("https://10.0.0.1/x", "", "", {})


def _push_form(endpoint):
    p256dh = webpush.b64u(b"\x04" + b"\x01" * 64)
    return {"endpoint": endpoint, "p256dh": p256dh, "auth": webpush.b64u(b"\x02" * 16)}


def test_push_subscribe_refuses_internal_addresses(client):
    _signed_in(client, _user(client, "pushowner"))
    r = client.post("/me/push/subscribe", data=_push_form("https://10.0.0.1/hook"))
    assert r.status_code == 400
    client.cookies.clear()


def test_push_subscribe_cannot_take_someone_elses_device(client):
    endpoint = "https://fcm.googleapis.com/fcm/send/device-1"
    alice, bob = _user(client, "alicepush"), _user(client, "bobpush")
    _signed_in(client, alice)
    assert client.post("/me/push/subscribe", data=_push_form(endpoint)).status_code == 200
    _signed_in(client, bob)
    assert client.post("/me/push/subscribe", data=_push_form(endpoint)).status_code == 409
    client.cookies.clear()
    with Session(site_engine) as s:
        assert s.exec(select(PushDevice).where(PushDevice.endpoint == endpoint)).one().user_id == alice.id


# ---------- user regular expressions ----------
def test_catastrophic_regex_gives_up_quickly():
    started = time.monotonic()
    assert matches_pattern("re:(a+)+$", "a" * 40 + "!") is False
    assert matches_pattern("re:(a+)+$", "a" * 40 + "!") is False  # remembered as slow: no second wait
    assert time.monotonic() - started < 1


def test_ordinary_regex_and_text_patterns_still_match():
    assert matches_pattern("re:^count(down|dn)", "COUNTDOWN AUCKLAND")
    assert matches_pattern("mojo", "Mojo Coffee Wellington")
    assert not matches_pattern("re:^z", "Countdown")


def test_pattern_validation():
    assert valid_pattern("re:^abc")
    assert not valid_pattern("re:(")
    assert not valid_pattern("re:")
    assert not valid_pattern("x" * 300)


# ---------- passwords, redirects ----------
def test_very_long_passwords_are_refused():
    assert auth.password_problem("correct horse " * 30)
    assert auth.password_problem("correct horse battery staple") == ""


@pytest.mark.parametrize("target", ["//evil.example/", "https://evil.example", "/\\evil.example", "/a\r\nSet-Cookie: x"])
def test_safe_next_stays_on_this_site(target):
    assert main._safe_next(target) == "/"


def test_privacy_toggle_does_not_redirect_off_site(client):
    _signed_in(client, _user(client, "privacyuser"))
    r = client.post("/privacy", headers={"Referer": "http://testserver//evil.example/x"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    client.cookies.clear()
