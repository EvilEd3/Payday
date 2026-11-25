import ipaddress
import secrets
import hmac
import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx
import segno
from markupsafe import Markup, escape
from sqlalchemy import func, or_
from sqlmodel import Session, delete as sql_delete, select

from . import activity, akahu, data, history, mailer, mortgage, splits, themes, vault
from .bills import APPROX_DAYS, match_pending, nearest_unpaid_due, occurrences, suggest_bill, valid_pattern
from .categorise import (categorise, rule_matches, find_partner, get_or_create_category, learn, link_transfer,
                         pair_transfers, recategorise_all)
from .auth import _sign as auth_sign
from .auth import (COOKIE, PRE_COOKIE, PROFILE_COOKIE, SESSION_DAYS, any_users, backup_codes_left, bootstrap_admin,
                   check_totp, create_profile, create_user, current_profile, current_user, find_invite, hash_password,
                   make_cookie, make_invite, make_pre_cookie, memberships, new_backup_codes, new_totp_secret, otpauth_uri,
                   password_problem, profile_admins, refresh_cookie, use_backup_code, user_from_cookie,
                   user_from_pre_cookie, verify_password)
from .db import (Account, Activity, Bill, Category, Goal, Invite, Loan, LoanEntry, LoanRate, Membership, NotifySent, PendingTransaction, Profile, PushDevice,
                 PropertyValue, Rule, Setting, Split, Transaction, User, current_engine, current_profile_id, get_setting,
                 init_db, set_setting, set_site_setting, site_engine, site_setting, use_profile)
from .insights import (Ctx, bars, category_averages, category_limits, usual_spend, change, detect_subscriptions, donut,
                       needs_review_query, payee_detail, payee_groups, payee_key, period_summary, review_groups,
                       trends)
from .forecast import account_balance, bank_balance, build_dashboard, counts_as_day_to_day
from .periods import FREQUENCIES, get_cycle, period_of

TZ = ZoneInfo(os.getenv("TZ_NAME", "Pacific/Auckland"))
scheduler = BackgroundScheduler(timezone=TZ)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STATIC = Path(__file__).parent / "static"


def money(v: float, signed: bool = False) -> Markup:
    s = f"${abs(v):,.2f}"
    if v < 0 and round(v, 2) != 0:
        s = f"−{s}"
    elif signed and v > 0:
        s = f"+{s}"
    return Markup(f'<span class="m">{s}</span>')


def is_private() -> bool:
    user = current_user.get()
    return bool(user and user.privacy)


templates.env.filters["money"] = money
templates.env.filters["nice"] = lambda d: d.strftime("%a %-d %b")
STALE_HOURS = 30  # Akahu refreshes about once a day; older than this means something's stuck


def _refreshed(iso: str):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def bank_freshness(iso: str) -> str:
    """When the bank last sent this account's transactions to Akahu, in words."""
    when = _refreshed(iso)
    if not when:
        return "not refreshed yet"
    local, now = when.astimezone(TZ), datetime.now(TZ)
    clock = local.strftime("%-I:%M %p").lower()
    days = (now.date() - local.date()).days
    if days == 0:
        return f"today {clock}"
    if days == 1:
        return f"yesterday {clock}"
    return f"{days} days ago ({local.strftime('%a %-d %b')})"


def bank_stale(account) -> bool:
    if account.bank_status and account.bank_status != "ACTIVE":
        return True
    when = _refreshed(account.refreshed_txns)
    return bool(when) and (datetime.now(timezone.utc) - when).total_seconds() > STALE_HOURS * 3600


BEHIND_HOURS = 1  # an account whose data is this much older than the newest is named as behind


def _naive(dt):
    return dt.replace(tzinfo=None) if dt and dt.tzinfo else dt


def bank_data(accounts) -> dict | None:
    """The newest bank data Payday holds, and which accounts lag well behind it.

    The newest time moves forward whenever a sync brings in fresh data. (Taking the oldest
    account instead kept the label stuck on one slow or closed account after every sync.)
    Accounts the last sync didn't see, e.g. ones removed from Akahu, are left out of "behind".
    """
    dated = [(a, w) for a in accounts if (w := _refreshed(a.refreshed_txns))]
    if not dated:
        return None
    newest = max(w for _, w in dated)
    seen = [_naive(a.updated_at) for a, _ in dated if a.updated_at]
    last_sync = max(seen) if seen else None
    behind = [{"name": a.name, "when": w.isoformat()} for a, w in sorted(dated, key=lambda d: d[1])
              if (newest - w).total_seconds() > BEHIND_HOURS * 3600
              and not (last_sync and a.updated_at and (last_sync - _naive(a.updated_at)).total_seconds() > 600)]
    return {"when": newest.isoformat(), "behind": behind}


templates.env.filters["freshness"] = bank_freshness
templates.env.filters["bank_data"] = bank_data
templates.env.tests["stale"] = bank_stale
templates.env.filters["short"] = lambda d: d.strftime("%-d %b")
templates.env.filters["long"] = lambda d: d.strftime("%A %-d %B")
templates.env.filters["my"] = lambda d: d.strftime("%b %Y") if d else "–"
templates.env.filters["dmy"] = lambda d: d.strftime("%-d %b %Y") if d else "–"
templates.env.filters["dollars"] = lambda v: Markup(f'<span class="m">{"−" if v < -0.5 else ""}${abs(v):,.0f}</span>')
templates.env.filters["yrsmths"] = lambda n: ("–" if n is None else
    ", ".join(x for x in (f"{n // 12} year{'s' if n // 12 != 1 else ''}" if n >= 12 else "",
                          f"{n % 12} month{'s' if n % 12 != 1 else ''}" if n % 12 or n < 12 else "") if x))
templates.env.filters["daybefore"] = lambda d: d - timedelta(days=1)
ACCOUNT_TYPES = {"CHECKING": "everyday account", "SAVINGS": "savings", "CREDITCARD": "credit card", "LOAN": "loan",
                 "KIWISAVER": "KiwiSaver", "INVESTMENT": "investment", "TERMDEPOSIT": "term deposit",
                 "FOREIGN": "foreign currency", "TAX": "tax", "REWARDS": "rewards", "WALLET": "wallet"}
templates.env.filters["acctype"] = lambda t: ACCOUNT_TYPES.get(t, t.lower())
templates.env.filters["pct"] = lambda v: "–" if v is None else f"{v:+.0%}".replace("-", "−")
templates.env.globals["private"] = is_private
templates.env.globals["sync_running"] = lambda: current_profile.get() is not None and akahu.is_running()


def asset(name: str) -> str:
    """URL for a file in /static that changes whenever the file does, so browsers never show a stale copy."""
    try:
        return f"/static/{name}?v={int((STATIC / name).stat().st_mtime)}"
    except OSError:
        return f"/static/{name}"


templates.env.globals["asset"] = asset
from .version import __version__  # noqa: E402
templates.env.globals["version"] = __version__


def today() -> date:
    return datetime.now(TZ).date()


def session():
    """The current profile's database (chosen and checked in the middleware)."""
    with Session(current_engine()) as s:
        yield s


def site_session():
    """The site database: people, profiles, memberships, invites."""
    with Session(site_engine) as s:
        yield s


def all_profile_ids() -> list[int]:
    with Session(site_engine) as s:
        return [p.id for p in s.exec(select(Profile).order_by(Profile.id))]


def schedule_sync(pid: int, hour: int) -> None:
    # misfire_grace_time: if the server was busy or just starting at the set hour, still run up to 2 hours late.
    # Profiles are spread over the hour so they don't all call the bank at once.
    scheduler.add_job(akahu.sync_profile, args=(pid, "scheduled"),
                      trigger=CronTrigger(hour=hour, minute=(pid * 7) % 60, timezone=TZ),
                      id=f"daily_sync_{pid}", replace_existing=True, misfire_grace_time=7200, coalesce=True)


def schedule_notifications() -> None:
    from . import notify
    scheduler.add_job(notify.run_all, trigger=CronTrigger(minute=5, timezone=TZ), id="notifications",
                      replace_existing=True, misfire_grace_time=1800, coalesce=True)


def unschedule_sync(pid: int) -> None:
    try:
        scheduler.remove_job(f"daily_sync_{pid}")
    except Exception:  # noqa: BLE001 - not scheduled is fine
        pass


def catch_up_sync(pid: int) -> None:
    """If the app was off when the daily sync was due, sync as soon as it starts again."""
    with use_profile(pid), Session(current_engine()) as s:
        last = get_setting(s, "last_sync_at")
    try:
        overdue = bool(last) and datetime.now(timezone.utc) - datetime.fromisoformat(last) > timedelta(hours=26)
    except ValueError:
        overdue = False
    if overdue:
        akahu.start_in_background("catch-up", pid)


BILL_DATES_VERSION = "3"  # 2: weekend bills due the Monday after. 3: bills checked before transfers (re-match once)


def upgrade_bill_dates(s: Session) -> None:
    """When the due-date rules change, re-point links you made by hand and re-match the rest."""
    version = get_setting(s, "bill_dates_version")
    if version == BILL_DATES_VERSION:
        return
    if version == "2":  # due dates unchanged: just re-match, so transfers that pay a bill get linked to it
        recategorise_all(s)
    elif s.exec(select(Bill)).first():
        bills = {b.id: b for b in s.exec(select(Bill))}
        manual = s.exec(select(Transaction).where(Transaction.bill_id != None,  # noqa: E711
                                                  Transaction.category_source == "manual")
                        .order_by(Transaction.date)).all()
        for t in manual:
            t.bill_due_date = None
        s.flush()
        for t in manual:
            if t.bill_id in bills:
                t.bill_due_date = nearest_unpaid_due(s, bills[t.bill_id], t)
                s.add(t)
                s.flush()
        s.commit()
        recategorise_all(s)
    set_setting(s, "bill_dates_version", BILL_DATES_VERSION)


def start_profile(pid: int) -> None:
    with use_profile(pid), Session(current_engine()) as s:
        upgrade_bill_dates(s)
        schedule_sync(pid, int(get_setting(s, "sync_hour") or 6))


@asynccontextmanager
async def lifespan(_app):
    init_db()
    bootstrap_admin()
    if not PUBLIC_URL:
        print("[payday] PUBLIC_URL isn't set, so Payday answers to any address. Set it to your https address "
              "before opening Payday to the internet.", flush=True)
    data.clean_temp()
    for pid in all_profile_ids():
        start_profile(pid)
    schedule_notifications()
    if not scheduler.running:
        scheduler.start()
    for pid in all_profile_ids():
        catch_up_sync(pid)
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


# No API pages: Payday has no machine interface, so the automatic /docs and /openapi.json are switched off.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# Reachable without signing in. Each of these checks its own proof (password, a 2FA step cookie or a one-time link).
PUBLIC_PATHS = ("/login", "/setup", "/invite/", "/reset/", "/2fa/", "/manifest.webmanifest", "/sw.js", "/static/",
                "/theme.css", "/favicon.ico", "/welcome", "/signup", "/verify/", "/forgot", "/legal/", "/help",
                "/contact", "/updates")
# Paths that work without a profile chosen.
NO_PROFILE_PATHS = ("/site", "/me", "/logout", "/profiles/", "/no-profile")
# Changes a read-only member may still make (their own preferences, sign-in and which profile they're viewing).
VIEWER_POSTS = ("/logout", "/privacy", "/me", "/me/password", "/me/username", "/me/signout-everywhere",
                "/me/2fa/reset", "/me/2fa/codes", "/me/email", "/me/delete", "/me/badge",
                "/me/notify", "/me/push/subscribe", "/me/push/remove", "/me/push/test", "/profiles/switch", "/profiles/leave")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
EXTRA_HOSTS = {h.strip().lower() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()}


def _host_ok(host: str) -> bool:
    """With PUBLIC_URL set, only answer to that name, this machine and addresses on your own network,
    so a stray DNS name or a rebinding trick can't reach Payday."""
    if not PUBLIC_URL:
        return True
    name = host.rsplit(":", 1)[0].strip("[]").lower() if host.count(":") <= 1 or host.startswith("[") else host.lower()
    if name in EXTRA_HOSTS or name == (urlparse(PUBLIC_URL).hostname or "").lower() or name in ("localhost", "testserver"):
        return True
    try:
        ip = ipaddress.ip_address(name)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


def _networks(raw: str) -> list:
    nets = []
    for part in raw.split(","):
        try:
            nets.append(ipaddress.ip_network(part.strip(), strict=False))
        except ValueError:
            if part.strip():
                print(f"[payday] TRUSTED_PROXIES: ignoring {part.strip()!r}, not an address or network.", flush=True)
    return nets


# Proxies whose forwarding headers (CF-Connecting-IP, X-Forwarded-For, X-Forwarded-Proto) are believed. By default
# this machine and private networks, which covers cloudflared or a reverse proxy in Docker or on your own network.
# Anyone else could put any address in those headers, and get round the sign-in limits by doing so.
TRUSTED_PROXIES = _networks(os.getenv("TRUSTED_PROXIES", "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,"
                                                         "192.168.0.0/16,fc00::/7"))


def _from_trusted_proxy(request: Request) -> bool:
    try:
        peer = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError:
        return False
    return any(peer in net for net in TRUSTED_PROXIES)


def is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    return _from_trusted_proxy(request) and (request.headers.get("x-forwarded-proto", "").lower() == "https"
                                             or '"https"' in request.headers.get("cf-visitor", ""))


def client_ip(request: Request) -> str:
    """The visitor's address. Behind a Cloudflare tunnel or a reverse proxy every request arrives from the proxy,
    so use the address it passes on, but only when the request really came from a trusted proxy."""
    peer = (request.client.host if request.client else "")
    if _from_trusted_proxy(request):
        forwarded = (request.headers.get("cf-connecting-ip") or request.headers.get("x-real-ip")
                     or request.headers.get("x-forwarded-for", "").split(",")[-1]).strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer.strip()[:64]


def _deny(message: str, status: int) -> Response:
    return Response(message, status_code=status, media_type="text/plain")


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    request.state.user = None
    request.state.profile = None
    request.state.profiles = []
    if not _host_ok(request.headers.get("host", "")):
        return _deny("Unknown address.", 421)
    # Refuse changes sent from another website (a form or script on some other page posting here while you're
    # signed in). Browsers label those requests "cross-site", and name the page they came from in Origin.
    if request.method not in ("GET", "HEAD"):
        if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            return _deny("Changes can only be made from Payday's own pages.", 403)
        origin = request.headers.get("origin", "")
        if origin and origin != "null" and urlparse(origin).netloc.lower() != request.headers.get("host", "").lower():
            return _deny("Changes can only be made from Payday's own pages.", 403)
    if path.startswith(PUBLIC_PATHS):
        if not path.startswith("/static/") and request.cookies.get(COOKIE):
            with Session(site_engine) as s:  # public pages can still say "Open Payday" to someone signed in
                known = user_from_cookie(s, request.cookies.get(COOKIE))
                if known:
                    s.expunge(known)
                    request.state.user = known
        return _harden(await call_next(request), request)
    with Session(site_engine) as s:
        if not any_users(s):
            return RedirectResponse("/setup", status_code=303)
        cookie = request.cookies.get(COOKIE)
        user = user_from_cookie(s, cookie)
        mems = []
        if user:
            mems = [(m.role, p) for m, p in memberships(s, user.id)]
            s.expunge(user)
            for _, p in mems:
                s.expunge(p)
    if not user:
        if request.method == "GET" and path == "/" and not request.url.query:
            return RedirectResponse("/welcome", status_code=303)
        if request.method == "GET":
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
        return _deny("Please sign in again.", 401)
    if path.startswith("/site") and not user.is_owner:
        return _deny("Only the site owner can open that.", 403)
    # A notification names its household (?household=), so tapping one from another household shows it there.
    asked = request.query_params.get("household", "") if request.method == "GET" else ""
    wanted = asked if any(str(p.id) == asked for _, p in mems) else request.cookies.get(PROFILE_COOKIE, "")
    chosen = next((m for m in mems if str(m[1].id) == wanted), None) or \
        next((m for m in mems if m[1].id == user.last_profile_id), None) or (mems[0] if mems else None)
    request.state.profiles = [(p.id, p.name, role) for role, p in mems]
    if not chosen and not path.startswith(NO_PROFILE_PATHS):
        return RedirectResponse("/site" if user.is_owner else "/no-profile", status_code=303)
    user.role = chosen[0] if chosen else "viewer"  # per request only; never saved
    if request.method != "GET" and user.role != "admin" and path not in VIEWER_POSTS \
            and not (path.startswith("/site") and user.is_owner):
        return _deny("Your account is read only here.", 403)
    request.state.user = user
    request.state.profile = chosen[1] if chosen else None
    tokens = [current_user.set(user), current_profile.set(request.state.profile)]
    try:
        if chosen:
            with use_profile(chosen[1].id):
                resp = await call_next(request)
        else:
            resp = await call_next(request)
        fresh = refresh_cookie(cookie)
        if fresh and COOKIE not in resp.headers.get("set-cookie", ""):
            _set_session_cookie(resp, request, fresh)
        if chosen and asked == str(chosen[1].id) and asked != request.cookies.get(PROFILE_COOKIE, ""):
            resp.set_cookie(PROFILE_COOKIE, asked, max_age=365 * 86400, **_cookie_opts(request))
        return _harden(resp, request)
    finally:
        current_profile.reset(tokens[1])
        current_user.reset(tokens[0])


def _harden(resp: Response, request: Request) -> Response:
    """Security headers on every response."""
    path = request.url.path
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    # Only this app's own scripts may run. Bank descriptions are shown on most pages, so this is a
    # second line of defence (after template escaping) against text that tries to inject a script.
    resp.headers.setdefault("Content-Security-Policy",
                            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                            "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
                            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    if is_https(request):
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if not path.startswith("/static/") and path not in ("/manifest.webmanifest", "/sw.js", "/theme.css"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ---------- Sign in, two-step codes, people ----------
def _safe_next(n: str) -> str:
    """Only ever redirect to a path inside this app."""
    return n if n.startswith("/") and not n.startswith("//") and "\\" not in n and "\n" not in n and "\r" not in n else "/"


def _here(request: Request, drop: tuple = ("changed",)) -> str:
    """The current page's address, for forms to come back to."""
    q = [(k, v) for k, v in request.query_params.multi_items() if k not in drop]
    return request.url.path + (f"?{urlencode(q)}" if q else "")


def _add_param(url: str, key: str, value) -> str:
    base, _, frag = url.partition("#")
    return f"{base}{'&' if '?' in base else '?'}{key}={quote(str(value), safe='')}" + (f"#{frag}" if frag else "")


templates.env.globals["here"] = _here

# Guessing limits. Per device: 8 wrong tries in 10 minutes. Per account, from anywhere: 20 in an hour.
MAX_FAILS, FAIL_WINDOW = 8, 600
MAX_ACCOUNT_FAILS, ACCOUNT_WINDOW = 20, 3600
MAX_KEYS = 20000  # memory cap for the counters below
_failures: dict[str, list[float]] = {}


def _trim_failures(now: float) -> None:
    """Keep the counters' memory bounded without ever wiping them all at once (that would let someone reset an
    account's limit by flooding junk names). Expired entries go first, then the ones with the fewest recent failures,
    so a busy account's counter is the last thing to be dropped."""
    for key in [k for k, v in _failures.items() if not v or now - v[-1] >= ACCOUNT_WINDOW]:
        del _failures[key]
    if len(_failures) > MAX_KEYS:
        by_weight = sorted(_failures, key=lambda k: (len(_failures[k]), _failures[k][-1]))
        for key in by_weight[:len(_failures) - MAX_KEYS // 2]:
            del _failures[key]


def _recent_failures(key: str, window: int = FAIL_WINDOW) -> list[float]:
    now = time.time()
    if len(_failures) > MAX_KEYS:
        _trim_failures(now)
    recent = [t for t in _failures.get(key, []) if now - t < window]
    _failures[key] = recent
    return recent


def _blocked(request: Request, name: str) -> bool:
    return (len(_recent_failures(f"{client_ip(request)}|{name}")) >= MAX_FAILS
            or len(_recent_failures(f"*|{name}", ACCOUNT_WINDOW)) >= MAX_ACCOUNT_FAILS
            or len(_recent_failures(f"{client_ip(request)}|*", ACCOUNT_WINDOW)) >= MAX_ACCOUNT_FAILS * 3)


def _failed(request: Request, name: str) -> None:
    now = time.time()
    for key in (f"{client_ip(request)}|{name}", f"*|{name}", f"{client_ip(request)}|*"):
        _recent_failures(key, ACCOUNT_WINDOW).append(now)
    # No sleep here: these handlers share the server's thread pool, so sleeping would let a burst of wrong
    # passwords stall every page for everyone. The counters above are what limit guessing.


def _cookie_opts(request: Request) -> dict:
    return {"httponly": True, "samesite": "lax", "secure": is_https(request), "path": "/"}


def _set_session_cookie(resp: Response, request: Request, value: str) -> None:
    resp.set_cookie(COOKIE, value, max_age=SESSION_DAYS * 86400, **_cookie_opts(request))


def _signed_in(request: Request, user: User, next_url: str = "/") -> RedirectResponse:
    resp = back(_safe_next(next_url))
    _set_session_cookie(resp, request, make_cookie(user))
    resp.delete_cookie(PRE_COOKIE, path="/")
    return resp


def _login_page(request: Request, error: str = "", status: int = 200, **extra):
    ctx = {"next": request.query_params.get("next", "/"), "error": error, "setup": False, "step": "password"}
    ctx.update(extra)
    return templates.TemplateResponse(request, "login.html", ctx, status_code=status)


@app.get("/login")
def login_page(request: Request, next: str = "/", error: str = ""):
    return _login_page(request, next=_safe_next(next), msg=request.query_params.get("msg", ""))


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/"),
          s: Session = Depends(site_session)):
    name = username.strip().lower()[:120]
    if _blocked(request, name):
        return _login_page(request, "Too many wrong tries. Wait a while, then try again.", 429, next=_safe_next(next))
    user = s.exec(select(User).where((User.username == name) | ((User.email == name) & (User.email_verified == True)))).first()  # noqa: E712
    if not verify_password(password, user.password_hash if user else None):
        _failed(request, name)
        return _login_page(request, "That username and password don’t match.", 401, next=_safe_next(next))
    if user.status == "unverified":
        return _login_page(request, "Confirm your email address first: open the link we sent you.", 403, next=_safe_next(next),
                           resend=user.username)
    if user.status == "pending":
        return _login_page(request, "Your account is waiting to be approved. You’ll get an email when it’s ready.", 403,
                           next=_safe_next(next))
    # Password right: now the 2FA code (or, the first time, setting it up)
    resp = back(f"/2fa/{'code' if user.totp_enabled else 'setup'}?next={quote(_safe_next(next), safe='')}")
    resp.set_cookie(PRE_COOKIE, make_pre_cookie(user), max_age=300, **_cookie_opts(request))
    return resp


def _pre_user(request: Request, s: Session) -> User | None:
    return user_from_pre_cookie(s, request.cookies.get(PRE_COOKIE))


@app.get("/2fa/code")
def code_page(request: Request, next: str = "/", s: Session = Depends(site_session)):
    if not _pre_user(request, s):
        return back("/login?msg=expired")
    return _login_page(request, step="code", next=_safe_next(next))


@app.post("/2fa/code")
def code_check(request: Request, code: str = Form(...), next: str = Form("/"), s: Session = Depends(site_session)):
    user = _pre_user(request, s)
    if not user or not user.totp_enabled:
        return back("/login?msg=expired")
    if _blocked(request, user.username):
        return _login_page(request, "Too many wrong tries. Wait a while, then try again.", 429, step="code", next=_safe_next(next))
    step = check_totp(vault.unseal("totp", user.totp_secret), code, user.totp_last_step)
    if step:
        user.totp_last_step = step
    elif not use_backup_code(user, code):
        _failed(request, user.username)
        return _login_page(request, "That code didn’t work. Codes change every 30 seconds.", 401, step="code", next=_safe_next(next))
    s.add(user)
    s.commit()
    return _signed_in(request, user, next)


@app.get("/2fa/setup")
def totp_setup_page(request: Request, next: str = "/", s: Session = Depends(site_session)):
    user = _pre_user(request, s)
    if not user:
        return back("/login?msg=expired")
    if user.totp_enabled:
        return back(f"/2fa/code?next={quote(_safe_next(next), safe='')}")
    secret = vault.unseal("totp", user.totp_secret)
    if not secret:
        secret = new_totp_secret()
        user.totp_secret = vault.seal("totp", secret)
        s.add(user)
        s.commit()
    uri = otpauth_uri(user, secret, os.getenv("PROFILE_NAME", "") and f"Payday {os.getenv('PROFILE_NAME')}" or "Payday")
    # viewBox (omitsize) lets the code scale without being cropped; a 4-module white border is what scanners need
    qr = segno.make(uri, error="m").svg_inline(scale=1, dark="#000", light="#fff", border=4, omitsize=True)
    return _login_page(request, step="setup", qr=Markup(qr), secret=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
                       username=user.username, next=_safe_next(next))


@app.post("/2fa/setup")
def totp_setup(request: Request, code: str = Form(...), next: str = Form("/"), s: Session = Depends(site_session)):
    user = _pre_user(request, s)
    if not user or user.totp_enabled:
        return back("/login?msg=expired")
    if _blocked(request, user.username):
        return back("/login?msg=expired")
    step = check_totp(vault.unseal("totp", user.totp_secret), code, 0)
    if not step:
        _failed(request, user.username)
        return back(f"/2fa/setup?next={quote(_safe_next(next), safe='')}&bad=1")
    codes, stored = new_backup_codes()
    user.totp_enabled, user.totp_last_step, user.backup_codes = True, step, stored
    s.add(user)
    s.commit()
    resp = _login_page(request, step="codes", codes=codes, next=_safe_next(next))
    _set_session_cookie(resp, request, make_cookie(user))
    resp.delete_cookie(PRE_COOKIE, path="/")
    return resp


@app.post("/logout")
def logout():
    resp = back("/login")
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/setup")
def setup_page(request: Request, s: Session = Depends(site_session)):
    if any_users(s):
        return back("/login")
    return _login_page(request, setup=True)


@app.post("/setup")
def setup(request: Request, username: str = Form(...), password: str = Form(...), profile: str = Form("Home"),
          email: str = Form(""), s: Session = Depends(site_session)):
    """The very first person on a new install: becomes the site owner and admin of the first profile."""
    if any_users(s):
        return back("/login")
    name = username.strip().lower()
    problem = password_problem(password, name) or ("" if re.fullmatch(r"[a-z0-9._-]{3,40}", name)
                                                   else "Usernames are 3 to 40 letters, numbers, dots or dashes.")
    if problem:
        return _login_page(request, problem, 400, setup=True)
    owner = create_user(s, name, password, owner=True)
    if EMAIL_RE.match(email.strip().lower()):
        owner.email = email.strip().lower()
        s.add(owner)
        s.commit()
        _send_verify(request, s, owner)
    if not s.exec(select(Profile)).first():
        prof = create_profile(s, profile, owner)
        start_profile(prof.id)
    else:
        for prof in s.exec(select(Profile)):
            s.add(Membership(user_id=owner.id, profile_id=prof.id, role="admin"))
        s.commit()
    resp = back("/2fa/setup")
    resp.set_cookie(PRE_COOKIE, make_pre_cookie(owner), max_age=300, **_cookie_opts(request))
    return resp


# ---------- Joining a profile, and password resets, by one-time link ----------
def _local_host(host: str) -> bool:
    """localhost, or an address on this machine or your own network."""
    name = host.rsplit(":", 1)[0].strip("[]").lower() if host.count(":") <= 1 or host.startswith("[") else host.strip("[]").lower()
    if name in ("localhost", "testserver"):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def _link(request: Request, path: str) -> str:
    """A full link to a page here. Uses PUBLIC_URL when set. Without it the address comes from the request, which
    is only safe for links shown to the person who made the request; emails use _email_link instead."""
    base = PUBLIC_URL or f"{'https' if is_https(request) else 'http'}://{request.headers.get('host', '')}"
    return f"{base}{path}"


def _email_link(request: Request, path: str) -> str | None:
    """A link to put in an email, or None if there's no address we can trust.

    Without PUBLIC_URL the Host header is whatever the sender typed, so a stranger could ask for someone's password
    reset with Host: their-site.example and the email would carry a real reset link to their site. So without
    PUBLIC_URL, only addresses on this machine or your own network are used."""
    if PUBLIC_URL:
        return f"{PUBLIC_URL}{path}"
    host = request.headers.get("host", "")
    if host and _local_host(host):
        return _link(request, path)
    print(f"[payday] Not emailing a link: PUBLIC_URL isn't set and the request came to {host[:80]!r}. "
          "Set PUBLIC_URL to Payday's https address.", flush=True)
    return None


@app.get("/invite/{token}")
def invite_page(request: Request, token: str, s: Session = Depends(site_session)):
    inv = find_invite(s, token)
    if not inv or inv.kind != "join":
        return _login_page(request, step="badlink", status=404)
    prof = s.get(Profile, inv.profile_id)
    signed_in = user_from_cookie(s, request.cookies.get(COOKIE))
    return _login_page(request, step="invite", token=token, profile_name=prof.name if prof else "",
                       role=inv.role, signed_in=signed_in.username if signed_in else "")


@app.post("/invite/{token}")
def invite_accept(request: Request, token: str, username: str = Form(""), password: str = Form(""),
                  email: str = Form(""), terms: bool = Form(False), s: Session = Depends(site_session)):
    inv = find_invite(s, token)
    if not inv or inv.kind != "join" or not s.get(Profile, inv.profile_id):
        return _login_page(request, step="badlink", status=404)
    prof = s.get(Profile, inv.profile_id)
    user = user_from_cookie(s, request.cookies.get(COOKIE))  # already have a Payday login: just join
    if not user:
        name, email = username.strip().lower(), email.strip().lower()
        problem = ("Usernames are 3 to 40 letters, numbers, dots or dashes, and that one may be taken."
                   if not _username_ok(s, name) else
                   "Enter a valid email address you don’t already use here." if not EMAIL_RE.match(email)
                   or s.exec(select(User).where(User.email == email)).first() else
                   password_problem(password, name) or ("" if terms else "Please accept the terms and privacy policy."))
        if problem:
            return _login_page(request, problem, 400, step="invite", token=token, profile_name=prof.name,
                               role=inv.role, signed_in="")
        user = create_user(s, name, password)
        user.email, user.terms_accepted_at = email, datetime.now(timezone.utc)
        s.add(user)
        s.commit()
        _send_verify(request, s, user)
    if not s.exec(select(Membership).where(Membership.user_id == user.id, Membership.profile_id == prof.id)).first():
        s.add(Membership(user_id=user.id, profile_id=prof.id, role=inv.role))
    inv.used_at = datetime.now(timezone.utc)
    user.last_profile_id = prof.id
    s.add(inv)
    s.add(user)
    s.commit()
    if user.totp_enabled and request.cookies.get(COOKIE):
        resp = back("/")
    else:
        resp = back("/2fa/setup")
        resp.set_cookie(PRE_COOKIE, make_pre_cookie(user), max_age=300, **_cookie_opts(request))
    resp.set_cookie(PROFILE_COOKIE, str(prof.id), max_age=365 * 86400, **_cookie_opts(request))
    return resp


@app.get("/reset/{token}")
def reset_page(request: Request, token: str, s: Session = Depends(site_session)):
    inv = find_invite(s, token)
    user = s.get(User, inv.user_id) if inv and inv.kind == "reset" else None
    if not user:
        return _login_page(request, step="badlink", status=404)
    return _login_page(request, step="reset", token=token, username=user.username)


@app.post("/reset/{token}")
def reset_password(request: Request, token: str, password: str = Form(...), s: Session = Depends(site_session)):
    inv = find_invite(s, token)
    user = s.get(User, inv.user_id) if inv and inv.kind == "reset" else None
    if not user:
        return _login_page(request, step="badlink", status=404)
    problem = password_problem(password, user.username)
    if problem:
        return _login_page(request, problem, 400, step="reset", token=token, username=user.username)
    user.password_hash = hash_password(password)
    user.session_version += 1
    inv.used_at = datetime.now(timezone.utc)
    s.add(user)
    s.add(inv)
    s.commit()
    return back("/login?msg=reset")


# ---------- Public sign-up, email checks, forgotten passwords ----------
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")


def signup_mode() -> str:
    mode = site_setting("signup_mode", "invite")
    return mode if mode in ("open", "approval", "invite") else "approval"


def _send_verify(request: Request, s: Session, user: User) -> None:
    token = make_invite(s, "verify", user.id, user_id=user.id, note=user.email or "", days=2)
    link = _email_link(request, "/verify/" + token)
    if not link:
        return
    mailer.send(user.email, "Confirm your email address",
                f"Hi {user.username},\n\nOpen this link to confirm your email address for Payday:\n\n"
                f"{link}\n\nThe link works once and lasts 48 hours. "
                "If you didn’t ask for this, ignore this email.")


def _activate(s: Session, user: User) -> None:
    """A confirmed (and, if needed, approved) sign-up gets their own household."""
    user.status = "active"
    s.add(user)
    s.commit()
    if user.signup_household and not s.exec(select(Membership).where(Membership.user_id == user.id)).first():
        prof = create_profile(s, user.signup_household, user)
        user.last_profile_id = prof.id
        s.add(user)
        s.commit()
        start_profile(prof.id)


@app.get("/welcome")
def welcome(request: Request):
    return templates.TemplateResponse(request, "welcome.html", {"mode": signup_mode(), "page": "welcome", "info": site_info()})


@app.get("/signup")
def signup_page(request: Request):
    return _login_page(request, step="signup", mode=signup_mode())


@app.post("/signup")
def signup(request: Request, household: str = Form(...), username: str = Form(...), email: str = Form(...),
           password: str = Form(...), terms: bool = Form(False), s: Session = Depends(site_session)):
    mode = signup_mode()
    if mode == "invite":
        return _login_page(request, "Sign-up is by invitation only at the moment.", 403, step="signup", mode=mode)
    if _blocked(request, "signup"):
        return _login_page(request, "Too many sign-ups from here. Try again later.", 429, step="signup", mode=mode)
    name, email = username.strip().lower(), email.strip().lower()
    household = " ".join(household.split())[:60]
    taken = s.exec(select(User).where(User.email == email)).first()
    problem = ("Give your household a name." if not household else
               "Enter a valid email address." if not EMAIL_RE.match(email) else
               "Usernames are 3 to 40 letters, numbers, dots or dashes, and that one may be taken." if not _username_ok(s, name) else
               "That email address already has an account. Sign in, or reset your password." if taken else
               password_problem(password, name) or
               ("" if terms else "Please accept the terms and privacy policy."))
    if problem:
        return _login_page(request, problem, 400, step="signup", mode=mode,
                           form={"household": household, "username": name, "email": email})
    _failed_quiet(request, "signup")
    user = create_user(s, name, password)
    user.email, user.status, user.signup_household = email, "unverified", household
    user.terms_accepted_at = datetime.now(timezone.utc)
    s.add(user)
    s.commit()
    _send_verify(request, s, user)
    return _login_page(request, step="checkemail", email=email)


def _failed_quiet(request: Request, key: str) -> None:
    """Count a sign-up or reset request towards the per-device limit, without the slow-down of a wrong password."""
    now = time.time()
    for k in (f"{client_ip(request)}|{key}",):
        _recent_failures(k).append(now)


@app.post("/signup/resend")
def resend_verify(request: Request, username: str = Form(...), s: Session = Depends(site_session)):
    if not _blocked(request, "signup"):
        _failed_quiet(request, "signup")
        user = s.exec(select(User).where(User.username == username.strip().lower())).first()
        if user and user.status == "unverified" and user.email:
            _send_verify(request, s, user)
    return _login_page(request, step="checkemail", email="")


@app.get("/verify/{token}")
def verify_email(request: Request, token: str, s: Session = Depends(site_session)):
    inv = find_invite(s, token)
    user = s.get(User, inv.user_id) if inv and inv.kind == "verify" else None
    if not user or (inv.note and user.email != inv.note):
        return _login_page(request, step="badlink", status=404)
    inv.used_at = datetime.now(timezone.utc)
    user.email_verified = True
    s.add(inv)
    s.add(user)
    s.commit()
    if user.status == "unverified":
        if signup_mode() == "open":
            _activate(s, user)
        else:
            user.status = "pending"
            s.add(user)
            s.commit()
            for owner in s.exec(select(User).where(User.is_owner == True, User.email_verified == True)):  # noqa: E712
                mailer.send(owner.email, "New Payday sign-up to approve",
                            f"{user.username} ({user.email}) signed up for the household “{user.signup_household}”.\n\n"
                            f"Approve or decline: {_email_link(request, '/site') or 'Site, in Payday'}")
            return _login_page(request, step="pending")
    return back("/login?msg=verified")


@app.get("/forgot")
def forgot_page(request: Request):
    return _login_page(request, step="forgot")


@app.post("/forgot")
def forgot(request: Request, email: str = Form(...), s: Session = Depends(site_session)):
    if not _blocked(request, "forgot"):
        _failed_quiet(request, "forgot")
        user = s.exec(select(User).where(User.email == email.strip().lower(), User.email_verified == True)).first()  # noqa: E712
        link = _email_link(request, "/reset/") if user else None
        if user and link:
            token = make_invite(s, "reset", user.id, user_id=user.id, note="Forgotten password", days=1)
            mailer.send(user.email, "Reset your Payday password",
                        f"Hi {user.username},\n\nOpen this link to choose a new password (it works once, for 24 hours):\n\n"
                        f"{link}{token}\n\nYou’ll still need your authenticator code to sign in. "
                        "If you didn’t ask for this, ignore this email; your password hasn’t changed.")
    return _login_page(request, step="forgotsent")


@app.post("/site/users/{user_id}/approve")
def site_approve(request: Request, user_id: int, s: Session = Depends(site_session)):
    user = s.get(User, user_id)
    if user and user.status == "pending":
        _activate(s, user)
        if user.email:
            mailer.send(user.email, "Your Payday account is ready",
                        f"Hi {user.username},\n\nYour account has been approved. Sign in here: {_email_link(request, '/login') or 'your Payday address'}")
    return _site("approved", who=user.username if user else "")


# ---------- Your own settings ----------
@app.post("/me")
def save_prefs(request: Request, text_size: str = Form(""), theme: str = Form(""), mode: str = Form(""),
               s: Session = Depends(site_session)):
    user = s.get(User, request.state.user.id)
    if text_size in ("small", "normal", "large"):
        user.text_size = text_size
    if theme in themes.THEMES:
        user.theme = theme
    if mode in themes.MODES:
        user.mode = mode
    s.add(user)
    s.commit()
    return _settings("you", msg="saved")


BADGE_EMOJI = ["🙂", "😎", "🦊", "🐨", "🐧", "🦉", "🐝", "🐙", "🌿", "🌻", "🌊", "⛰️", "☕", "🎧", "🚲", "⭐"]
BADGE_COLOURS = {"bush": "#2F7D5B", "sea": "#1F6F99", "kowhai": "#B8860B", "rata": "#C0392B",
                 "plum": "#7D3C98", "slate": "#4B5563", "pohutu": "#D35C7E", "fern": "#5B8C2A"}


def badge(user) -> Markup:
    """The round badge for a person: their emoji, or their initial on their colour."""
    if not user:
        return Markup("")
    colour = BADGE_COLOURS.get(getattr(user, "badge_colour", ""))
    style = f' style="--badge:{colour}"' if colour else ""
    cls = "avatar" + (" coloured" if colour else "")
    emoji = getattr(user, "badge_emoji", "")
    if emoji in BADGE_EMOJI:
        return Markup(f'<span class="{cls} emoji"{style} aria-hidden="true">{emoji}</span>')
    return Markup(f'<span class="{cls}"{style} aria-hidden="true">{escape(user.username[:1].upper())}</span>')


templates.env.globals.update(badge=badge, BADGE_EMOJI=BADGE_EMOJI, BADGE_COLOURS=BADGE_COLOURS)


@app.post("/me/badge")
def save_badge(request: Request, emoji: str = Form(""), colour: str = Form(""), s: Session = Depends(site_session)):
    user = s.get(User, request.state.user.id)
    user.badge_emoji = emoji if emoji in BADGE_EMOJI else ""
    user.badge_colour = colour if colour in BADGE_COLOURS else ""
    s.add(user)
    s.commit()
    return _settings("you", msg="saved")


# ---------- Notifications (per person) ----------
def _membership(ss: Session, user_id: int, profile_id: int) -> Membership | None:
    return ss.exec(select(Membership).where(Membership.user_id == user_id, Membership.profile_id == profile_id)).first()


@app.post("/me/notify")
async def save_notify(request: Request, s: Session = Depends(site_session)):
    from . import notify
    prof = request.state.profile
    m = _membership(s, request.state.user.id, prof.id) if prof else None
    if m:
        m.notify = json.dumps(notify.clean_prefs(await request.form()))
        s.add(m)
        s.commit()
    return _settings("notify", msg="saved")


def _device_label(ua: str) -> str:
    ua = ua or ""
    device = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                  ("Macintosh", "Mac"), ("Windows", "Windows"), ("Linux", "Linux")) if k in ua), "Device")
    browser = next((n for k, n in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
                                   ("Safari/", "Safari")) if k in ua), "")
    return f"{browser} on {device}" if browser and device not in ("iPhone", "iPad") else device


@app.post("/me/push/subscribe")
def push_subscribe(request: Request, endpoint: str = Form(""), p256dh: str = Form(""), auth: str = Form(""),
                   s: Session = Depends(site_session)):
    from . import webpush
    try:
        ok = (webpush.endpoint_ok(endpoint) and len(endpoint) < 1000 and len(webpush.unb64u(p256dh)) == 65
              and len(webpush.unb64u(auth)) == 16)
    except ValueError:
        ok = False
    if not ok:
        return JSONResponse({"ok": False}, status_code=400)
    dev = s.exec(select(PushDevice).where(PushDevice.endpoint == endpoint)).first()
    if dev and dev.user_id != request.state.user.id:  # someone else's device: never move it to this login
        return JSONResponse({"ok": False}, status_code=409)
    dev = dev or PushDevice(endpoint=endpoint, p256dh="", auth="")
    dev.user_id, dev.p256dh, dev.auth = request.state.user.id, p256dh, auth
    dev.label = _device_label(request.headers.get("user-agent", ""))
    dev.created = dev.created or datetime.now(timezone.utc)
    s.add(dev)
    s.commit()
    return JSONResponse({"ok": True})


@app.post("/me/push/remove")
def push_remove(request: Request, device: int = Form(0), s: Session = Depends(site_session)):
    dev = s.get(PushDevice, device)
    if dev and dev.user_id == request.state.user.id:
        s.delete(dev)
        s.commit()
    return _settings("notify", msg="devremoved")


@app.post("/me/push/test")
def push_test(request: Request, s: Session = Depends(site_session)):
    from . import notify
    sent = notify.test_message(s, request.state.user.id)
    return _settings("notify", msg="pushtest" if sent else "pushfail")


@app.post("/me/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...),
                    s: Session = Depends(site_session)):
    user = s.get(User, request.state.user.id)
    if not verify_password(current, user.password_hash):
        return _settings("you", msg="badpw")
    if password_problem(new, user.username):
        return _settings("you", msg="weakpw")
    user.password_hash = hash_password(new)
    user.session_version += 1  # signs out other devices
    s.add(user)
    s.commit()
    return _signed_in(request, user, "/settings?tab=you&msg=pw")


@app.post("/me/2fa/reset")
def reset_own_2fa(request: Request, current: str = Form(...), s: Session = Depends(site_session)):
    """New phone: set the authenticator up again (needs your password)."""
    user = s.get(User, request.state.user.id)
    if not verify_password(current, user.password_hash):
        return _settings("you", msg="badpw")
    user.totp_enabled, user.totp_secret, user.backup_codes = False, "", ""
    user.session_version += 1
    s.add(user)
    s.commit()
    resp = back("/2fa/setup?next=%2Fsettings%3Ftab%3Dyou")
    resp.delete_cookie(COOKIE, path="/")
    resp.set_cookie(PRE_COOKIE, make_pre_cookie(user), max_age=300, **_cookie_opts(request))
    return resp


@app.post("/me/2fa/codes")
def new_codes(request: Request, current: str = Form(...), s: Session = Depends(site_session)):
    user = s.get(User, request.state.user.id)
    if not verify_password(current, user.password_hash):
        return _settings("you", msg="badpw")
    codes, user.backup_codes = new_backup_codes()
    s.add(user)
    s.commit()
    return _login_page(request, step="codes", codes=codes, next="/settings?tab=you")


def _username_ok(s: Session, name: str, user_id: int | None = None) -> bool:
    """3 to 40 letters, numbers, dots, dashes or underscores, and not taken by someone else."""
    if not re.fullmatch(r"[a-z0-9._-]{3,40}", name):
        return False
    other = s.exec(select(User).where(User.username == name)).first()
    return other is None or other.id == user_id


@app.post("/me/signout-everywhere")
def signout_everywhere(request: Request, s: Session = Depends(site_session)):
    user = s.get(User, request.state.user.id)
    user.session_version += 1  # every existing sign-in cookie stops working
    s.add(user)
    s.commit()
    resp = back("/login")
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.post("/me/username")
def change_username(request: Request, username: str = Form(...), current: str = Form(...),
                    s: Session = Depends(site_session)):
    user = s.get(User, request.state.user.id)
    name = username.strip().lower()
    if not verify_password(current, user.password_hash):
        return _settings("you", msg="badpw")
    if not _username_ok(s, name, user.id):
        return _settings("you", msg="badname")
    user.username = name
    s.add(user)
    s.commit()
    return _settings("you", msg="renamed")


@app.post("/me/email")
def change_email(request: Request, email: str = Form(...), current: str = Form(...), s: Session = Depends(site_session)):
    user = s.get(User, request.state.user.id)
    email = email.strip().lower()
    if not verify_password(current, user.password_hash):
        return _settings("you", msg="badpw")
    other = s.exec(select(User).where(User.email == email, User.id != user.id)).first()
    if not EMAIL_RE.match(email) or other:
        return _settings("you", msg="bademail")
    user.email, user.email_verified = email, False
    s.add(user)
    s.commit()
    _send_verify(request, s, user)
    return _settings("you", msg="emailsent")


@app.post("/me/delete")
def delete_account(request: Request, current: str = Form(...), code: str = Form(...), confirm: str = Form(""),
                   s: Session = Depends(site_session)):
    """Delete your login. Households where you're the only member are deleted with all their data."""
    user = s.get(User, request.state.user.id)
    if _blocked(request, user.username):
        return _settings("you", msg="badcode")
    step = check_totp(vault.unseal("totp", user.totp_secret), code, user.totp_last_step)
    if not verify_password(current, user.password_hash) or not step or confirm.strip().lower() != "delete":
        _failed(request, user.username)
        return _settings("you", msg="badcode")
    if user.is_owner and len(s.exec(select(User).where(User.is_owner == True)).all()) <= 1 \
            and len(s.exec(select(User)).all()) > 1:  # noqa: E712
        return _settings("you", msg="lastowner")
    mine = s.exec(select(Membership).where(Membership.user_id == user.id)).all()
    alone, blocked = [], []
    for m in mine:
        others = s.exec(select(Membership).where(Membership.profile_id == m.profile_id, Membership.user_id != user.id)).all()
        if not others:
            alone.append(m.profile_id)
        elif m.role == "admin" and not any(o.role == "admin" for o in others):
            blocked.append(m.profile_id)
    if blocked:
        return _settings("you", msg="lastadmin")
    for pid in alone:
        prof = s.get(Profile, pid)
        if prof and not akahu.is_running(pid):
            unschedule_sync(pid)
            with use_profile(pid), Session(current_engine()) as ps:
                akahu.disconnect(ps)  # tell Akahu to stop sharing this household's bank data
            for i in s.exec(select(Invite).where(Invite.profile_id == pid)):
                s.delete(i)
            s.delete(prof)
            s.commit()
            data.drop_profile_data(prof)
    for m in s.exec(select(Membership).where(Membership.user_id == user.id)):
        s.delete(m)
    for i in s.exec(select(Invite).where(Invite.user_id == user.id)):
        s.delete(i)
    for row in [*s.exec(select(PushDevice).where(PushDevice.user_id == user.id)),
                *s.exec(select(NotifySent).where(NotifySent.user_id == user.id))]:
        s.delete(row)
    s.delete(user)
    s.commit()
    resp = back("/welcome?msg=deleted")
    resp.delete_cookie(COOKIE, path="/")
    resp.delete_cookie(PROFILE_COOKIE, path="/")
    return resp


# ---------- Profiles: switching, members, invites ----------
@app.post("/profiles/switch")
def switch_profile(request: Request, profile_id: int = Form(...), s: Session = Depends(site_session)):
    me = request.state.user
    if not s.exec(select(Membership).where(Membership.user_id == me.id, Membership.profile_id == profile_id)).first():
        return back("/")
    user = s.get(User, me.id)
    user.last_profile_id = profile_id
    s.add(user)
    s.commit()
    resp = back("/")
    resp.set_cookie(PROFILE_COOKIE, str(profile_id), max_age=365 * 86400, **_cookie_opts(request))
    return resp


@app.get("/no-profile")
def no_profile(request: Request):
    return templates.TemplateResponse(request, "no_profile.html", {"page": ""})


def _me_profile(request: Request) -> Profile:
    return request.state.profile


@app.post("/members/invite")
def invite_member(request: Request, role: str = Form("viewer"), note: str = Form(""), s: Session = Depends(site_session)):
    prof = _me_profile(request)
    token = make_invite(s, "join", request.state.user.id, profile_id=prof.id, role=role, note=note)
    return _settings("members", msg="invited", link=_link(request, f"/invite/{token}"))


@app.post("/members/invites/{invite_id}/delete")
def cancel_invite(request: Request, invite_id: int, s: Session = Depends(site_session)):
    inv = s.get(Invite, invite_id)
    if inv and inv.profile_id == _me_profile(request).id and inv.kind == "join":
        s.delete(inv)
        s.commit()
    return _settings("members", msg="deleted")


@app.post("/members/{member_id}")
def edit_member(request: Request, member_id: int, role: str = Form(...), s: Session = Depends(site_session)):
    m = s.get(Membership, member_id)
    prof = _me_profile(request)
    if not m or m.profile_id != prof.id:
        return _settings("members")
    role = role if role in ("admin", "viewer") else "viewer"
    if m.role == "admin" and role != "admin" and profile_admins(s, prof.id) <= 1:
        return _settings("members", msg="lastadmin")
    m.role = role
    s.add(m)
    s.commit()
    return _settings("members", msg="saved")


@app.post("/members/{member_id}/delete")
def remove_member(request: Request, member_id: int, s: Session = Depends(site_session)):
    m = s.get(Membership, member_id)
    prof = _me_profile(request)
    if m and m.profile_id == prof.id:
        if m.role == "admin" and profile_admins(s, prof.id) <= 1:
            return _settings("members", msg="lastadmin")
        s.delete(m)
        s.commit()
    return _settings("members", msg="deleted")


@app.post("/profiles/leave")
def leave_profile(request: Request, s: Session = Depends(site_session)):
    prof, me = _me_profile(request), request.state.user
    m = s.exec(select(Membership).where(Membership.user_id == me.id, Membership.profile_id == prof.id)).first() if prof else None
    if m:
        if m.role == "admin" and profile_admins(s, prof.id) <= 1:
            return _settings("members", msg="lastadmin")
        s.delete(m)
        s.commit()
    resp = back("/")
    resp.delete_cookie(PROFILE_COOKIE, path="/")
    return resp


@app.post("/profile/rename")
def rename_profile(request: Request, name: str = Form(...), s: Session = Depends(site_session)):
    prof = s.get(Profile, _me_profile(request).id)
    if " ".join(name.split()):
        prof.name = " ".join(name.split())[:60]
        s.add(prof)
        s.commit()
    return _settings("pay", msg="saved")


@app.get("/theme.css")
def theme_css():
    return Response(themes.CSS, media_type="text/css", headers={"Cache-Control": "public, max-age=31536000, immutable"})


templates.env.globals["theme_version"] = themes.VERSION
templates.env.globals["THEMES"] = themes.THEMES
templates.env.globals["MODES"] = themes.MODES
templates.env.globals["browser_colour"] = themes.browser_colour


# ---------- Install as an app ----------
@app.get("/manifest.webmanifest")
def manifest():
    return Response(media_type="application/manifest+json", content="""{
  "name": "Payday",
  "short_name": "Payday",
  "description": "Money per day until payday",
  "start_url": "/",
  "scope": "/",
  "display": "standalone",
  "background_color": "#EAEFEC",
  "theme_color": "#2E6B57",
  "icons": [
    {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
    {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    {"src": "/static/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}
  ]
}""")


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


# ---------- Dashboard ----------
@app.get("/")
def dashboard(request: Request, offset: int = 0, s: Session = Depends(session)):
    d = build_dashboard(s, today(), offset)
    return templates.TemplateResponse(request, "dashboard.html",
                                      {"d": d, "page": "home", "warn": rate_warning(s),
                                       "setup": None if d["all_accounts"] else setup_steps(s)})


def setup_steps(s: Session) -> list[dict]:
    """A new household's first steps, shown on the Overview until the first accounts arrive."""
    return [
        {"label": "Set your payday", "href": "/settings?tab=pay",
         "done": bool(s.get(Setting, "pay_frequency") or s.get(Setting, "payday_day")),
         "why": "Payday budgets from one pay to the next."},
        {"label": "Connect your bank", "href": "/settings?tab=bank", "done": bool(vault.tokens(s).get("user")),
         "why": "Through Akahu, read only. See Help for the steps."},
        {"label": "Sync and choose the accounts that count", "href": "/settings?tab=accounts", "done": False,
         "why": "Press Sync now below, then tick the accounts your day-to-day money is in."},
        {"label": "Add your bills", "href": "/bills", "done": bool(s.exec(select(Bill)).first()),
         "why": "Payday sets their money aside and ticks them off when they're paid."},
        {"label": "Set a goal", "href": "/goals", "done": bool(s.exec(select(Goal)).first()),
         "why": "Optional: save towards something by a date."},
    ]


@app.post("/privacy")
def toggle_privacy(request: Request, s: Session = Depends(site_session)):
    user = s.get(User, request.state.user.id)
    user.privacy = not user.privacy
    s.add(user)
    s.commit()
    ref = urlparse(request.headers.get("referer", "/"))
    return back(_safe_next((ref.path or "/") + (f"?{ref.query}" if ref.query else "")))


@app.post("/sync")
def sync_now(request: Request):
    started = akahu.start_in_background("manual")
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"started": started, "running": True})
    return back("/?sync=1")


@app.get("/sync/status")
def sync_status(s: Session = Depends(session)):
    snap = akahu.progress_for().snapshot()
    snap["running"] = akahu.is_running()
    if not snap["steps"] and get_setting(s, "last_sync_log"):
        snap = json.loads(get_setting(s, "last_sync_log"))
        snap["running"] = akahu.is_running()
    if not snap["steps"]:
        snap["last"] = get_setting(s, "last_sync_status")
        snap["last_time"] = get_setting(s, "last_sync")
    return JSONResponse(snap)


VIEW_TITLES = {"daytoday": "Day-to-day spending", "spending": "All spending", "bills": "Bill payments",
               "income": "Income", "transfers": "Transfers"}


@app.get("/transactions")
def transactions(request: Request, offset: int = 0, category: int | None = None,
                 view: str = "", day: str = "", payee: str = "", search: str = "", tag: str = "",
                 s: Session = Depends(session)):
    start, end = period_of(s, today(), offset)
    search, tag = search.strip(), tag.strip().lower()
    all_time = bool(search or tag)
    q = select(Transaction) if all_time else select(Transaction).where(Transaction.date >= start,
                                                                      Transaction.date < end)
    if category is not None:  # this category, or split with a part in it
        q = q.where(or_(Transaction.category_id == category,
                        Transaction.id.in_(select(Split.txn_id).where(Split.category_id == category))))
    txns = s.exec(q.order_by(Transaction.date.desc())).all()
    if category is not None or view:
        # totals by category count a split transaction part by part, so these lists do too
        txns = [t for t in splits.expand(s, txns) if category is None or t.category_id == category]
    cats = s.exec(select(Category).order_by(Category.name)).all()
    cat_map = {c.id: c for c in cats}
    accounts = {a.id: a for a in s.exec(select(Account))}
    ctx = Ctx(s, today())
    view_filters = {
        "daytoday": lambda t: counts_as_day_to_day(t, ctx.kind(t), accounts),
        "spending": ctx.is_spending,
        "bills": lambda t: t.bill_id is not None,
        "income": lambda t: t.amount > 0 and ctx.kind(t) == "income",
        "transfers": lambda t: ctx.kind(t) == "transfer",
    }
    if view in view_filters:
        txns = [t for t in txns if view_filters[view](t)]
    if payee:
        txns = [t for t in txns if payee_key(t) == payee]
    if tag:
        txns = [t for t in txns if tag in split_tags(t.tags)]
    if search:
        txns = [t for t in txns if matches_search(t, search, accounts, cat_map)]
    day_date = None
    if day:
        try:
            day_date = date.fromisoformat(day)
            txns = [t for t in txns if t.date == day_date]
        except ValueError:
            day_date = None
    pending = []
    if not (all_time or category or payee) and view in ("", "daytoday", "spending"):
        pq = select(PendingTransaction).where(PendingTransaction.date >= start, PendingTransaction.date < end)
        if day_date:
            pq = pq.where(PendingTransaction.date == day_date)
        pending = s.exec(pq.order_by(PendingTransaction.date.desc())).all()
        if view:  # the day-to-day and spending views list only purchases, as the totals do
            pending = [p for p in pending if p.amount < 0]
    found, total = len(txns), sum(t.amount for t in txns)
    txns = txns[:MAX_ROWS]  # a broad all-time search could otherwise build a page of thousands of rows
    others = {t.transfer_id for t in txns if t.transfer_id}
    partners = {t.id: t for t in s.exec(select(Transaction).where(Transaction.id.in_(others)))} if others else {}
    return templates.TemplateResponse(request, "transactions.html", {
        "txns": txns, "cats": cats, "cat_map": cat_map,
        "split_map": splits.parts_for(s, {t.id for t in txns if not hasattr(t, "split_of")}),
        "bills": {b.id: b for b in s.exec(select(Bill))},
        "accounts": accounts, "partners": partners, "view": view, "view_title": VIEW_TITLES.get(view, ""),
        "day": day_date, "payee": payee, "search": search, "tag": tag, "all_time": all_time,
        "pending": pending, "changed": request.query_params.get("changed", ""),
        "category": cat_map.get(category), "total": total, "found": found,
        "start": start, "end": end, "offset": offset, "page": "transactions"})


@app.post("/transfers/recheck")
def recheck_transfers(s: Session = Depends(session)):
    found = pair_transfers(s)
    set_setting(s, "last_sync_status", f"Transfer check found {found} new pair{'s' if found != 1 else ''}.")
    return back("/transactions?view=transfers")


TXN_PAGE = "/transactions"
MAX_ROWS = 500


def _log_txn(s: Session, txn: Transaction, title: str, lines: list | None = None) -> None:
    """A change someone made to one transaction, with where its money is allocated now."""
    names = activity.Names(s)
    activity.record(s, "change", f"{title}: {activity.money(txn.amount)} {activity.payee(txn)}",
                    (lines or []) + [f"{txn.date.strftime('%a %-d %b %Y')}, {names.account(txn.account_id)}",
                                     "Now: " + activity.allocation(txn, names)],
                    amount=txn.amount, url=activity.txn_url(txn), ref=txn.id)


def split_tags(raw: str) -> list[str]:
    return [t.strip().lower() for t in raw.replace(";", ",").split(",") if t.strip()]


def matches_search(t: Transaction, q: str, accounts: dict, cats: dict) -> bool:
    """Text anywhere (description, payee, note, tags, account, category) or an amount like 45.50."""
    ql = q.lower().replace("$", "").replace(",", "")
    try:
        if abs(abs(t.amount) - abs(float(ql))) < 0.005:
            return True
    except ValueError:
        pass
    acc = accounts.get(t.account_id)
    hay = " ".join([t.description, t.merchant, t.note, t.tags, payee_key(t),
                    acc.name if acc else "", cats[t.category_id].name if t.category_id in cats else "",
                    f"{abs(t.amount):.2f}"]).lower()
    return all(word in hay for word in ql.split())


@app.post("/transactions/{txn_id}/note")
def save_note(txn_id: str, note: str = Form(""), tags: str = Form(""), next: str = Form(TXN_PAGE),
              s: Session = Depends(session)):
    txn = s.get(Transaction, txn_id)
    if txn:
        old = (txn.note, txn.tags)
        txn.note = note.strip()[:500]
        txn.tags = ", ".join(dict.fromkeys(split_tags(tags)))[:200]
        s.add(txn)
        if old != (txn.note, txn.tags):
            _log_txn(s, txn, "Note changed", [f"Note: {txn.note or '(none)'}", f"Tags: {txn.tags or '(none)'}"])
        s.commit()
    return back(_safe_next(next))


@app.post("/transactions/{txn_id}/category")
def set_category(txn_id: str, category_id: int = Form(...), next: str = Form(TXN_PAGE),
                 s: Session = Depends(session)):
    txn, new_cat = s.get(Transaction, txn_id), s.get(Category, category_id)
    if not txn or not new_cat:
        return back(_safe_next(next))
    was = activity.allocation(txn, activity.Names(s))
    if txn.transfer_id and new_cat.kind != "transfer":
        # you've said this isn't a transfer, so unlink the other half too
        other = s.get(Transaction, txn.transfer_id)
        txn.transfer_id = None
        if other:
            other.transfer_id = None
            categorise(s, other)
            s.add(other)
    txn.category_id, txn.category_source, txn.reviewed = new_cat.id, "manual", True
    splits.clear(s, txn.id)  # one category for the whole transaction again
    s.add(txn)
    _log_txn(s, txn, "Category changed", [f"Was: {was}"])
    s.commit()
    if new_cat.kind == "transfer" and not txn.transfer_id:
        # you've said it's a transfer, so find and mark the other half too
        other = find_partner(s, txn)
        if other:
            link_transfer(s, txn, other)
            s.commit()
    # "changed" lets the page offer to use this category for the payee from now on
    return back(_add_param(_safe_next(next), "changed", txn.id))


def _splittable(txn: Transaction | None) -> bool:
    """Bills and transfers are matched as a whole, so they aren't split."""
    return bool(txn) and abs(txn.amount) >= 0.02 and txn.bill_id is None and txn.transfer_id is None


@app.get("/transactions/{txn_id}/split")
def split_page(request: Request, txn_id: str, next: str = TXN_PAGE, bad: str = "", s: Session = Depends(session)):
    txn = s.get(Transaction, txn_id)
    if not _splittable(txn):
        return back(_safe_next(next))
    parts = splits.parts_for(s, [txn.id]).get(txn.id, [])
    rows = [{"category_id": p.category_id, "amount": f"{abs(p.amount):.2f}", "note": p.note} for p in parts]
    if not rows:
        rows = [{"category_id": txn.category_id, "amount": f"{abs(txn.amount):.2f}", "note": ""},
                {"category_id": None, "amount": "", "note": ""}]
    rows += [{"category_id": None, "amount": "", "note": ""}] * max(0, SPLIT_ROWS - len(rows))
    return templates.TemplateResponse(request, "split.html", {
        "t": txn, "rows": rows, "split": bool(parts), "next": _safe_next(next), "bad": bad,
        "cats": s.exec(select(Category).where(Category.kind == "expense").order_by(Category.name)).all()
        if txn.amount < 0 else s.exec(select(Category).order_by(Category.name)).all(),
        "account": s.get(Account, txn.account_id), "page": "transactions"})


SPLIT_ROWS = 5


@app.post("/transactions/{txn_id}/split")
async def save_split(request: Request, txn_id: str, s: Session = Depends(session)):
    form = await request.form()
    next_ = _safe_next(str(form.get("next") or TXN_PAGE))
    txn = s.get(Transaction, txn_id)
    if not _splittable(txn):
        return back(next_)
    sign = -1 if txn.amount < 0 else 1
    parts, seen = [], set()
    for cid, amount, note in zip(form.getlist("category_id"), form.getlist("amount"), form.getlist("note")):
        amount = str(amount).replace("$", "").replace(",", "").strip()
        if not amount and not str(cid):
            continue
        try:
            value = round(abs(float(amount)), 2)
        except ValueError:
            value = 0
        cat = s.get(Category, int(cid)) if str(cid).isdigit() else None
        if value < 0.005 and not cat:
            continue
        if not cat or value < 0.005 or cat.id in seen:
            return back(f"/transactions/{txn_id}/split?bad=parts&next={quote(next_, safe='')}")
        seen.add(cat.id)
        parts.append(Split(txn_id=txn.id, amount=sign * value, category_id=cat.id, note=str(note).strip()[:200]))
    if len(parts) < 2 or abs(sum(p.amount for p in parts) - txn.amount) >= 0.005:
        return back(f"/transactions/{txn_id}/split?bad=sum&next={quote(next_, safe='')}")
    splits.clear(s, txn.id)
    for p in parts:
        s.add(p)
    biggest = max(parts, key=lambda p: abs(p.amount))  # what the transaction shows where one category is needed
    txn.category_id, txn.category_source, txn.reviewed = biggest.category_id, "manual", True
    s.add(txn)
    names = activity.Names(s)
    _log_txn(s, txn, "Split", [f"{activity.money(p.amount)} to {names.category(p.category_id)}"
                               + (f" ({p.note})" if p.note else "") for p in parts])
    s.commit()
    return back(next_)


@app.post("/transactions/{txn_id}/unsplit")
def unsplit(txn_id: str, next: str = Form(TXN_PAGE), s: Session = Depends(session)):
    txn = s.get(Transaction, txn_id)
    if txn:
        splits.clear(s, txn_id)
        _log_txn(s, txn, "Split removed")
        s.commit()
    return back(_safe_next(next))


@app.post("/transactions/{txn_id}/remember")
def remember_category(txn_id: str, next: str = Form(TXN_PAGE), s: Session = Depends(session)):
    txn = s.get(Transaction, txn_id)
    if txn and txn.category_id:
        learn(s, txn, txn.category_id)
    return back(_safe_next(next))


@app.post("/transactions/{txn_id}/reset")
def reset_category(txn_id: str, next: str = Form(TXN_PAGE), s: Session = Depends(session)):
    txn = s.get(Transaction, txn_id)
    if txn:
        txn.category_source = ""
        splits.clear(s, txn.id)
        categorise(s, txn)
        s.add(txn)
        _log_txn(s, txn, "Category handed back to Payday")
        s.commit()
    return back(_safe_next(next))


@app.post("/transactions/{txn_id}/bill")
def link_bill(txn_id: str, bill_id: str = Form(""), next: str = Form(TXN_PAGE),
              s: Session = Depends(session)):
    txn = s.get(Transaction, txn_id)
    bill = s.get(Bill, int(bill_id)) if bill_id.isdigit() else None
    if txn and (bill or not bill_id):
        if bill:  # link to the chosen bill
            txn.bill_id, txn.bill_due_date = bill.id, nearest_unpaid_due(s, bill, txn)
            txn.category_id = bill.category_id or get_or_create_category(s, "Bills").id
        else:  # unlink, and keep it unlinked
            txn.bill_id = txn.bill_due_date = None
        txn.category_source = "manual"
        s.add(txn)
        _log_txn(s, txn, "Linked to a bill" if bill else "Unlinked from its bill")
        s.commit()
    return back(_safe_next(next))


# ---------- Review: guessed categories ----------
def review_count() -> int:
    """How many transactions are waiting for you to check their category (shown to admins only)."""
    user = current_user.get()
    if not user or user.role != "admin" or current_profile.get() is None:
        return 0
    with Session(current_engine()) as s:
        return len(s.exec(needs_review_query()).all())


templates.env.globals["review_count"] = review_count


def _review_group(s: Session, name: str) -> list[Transaction]:
    return [t for t in s.exec(needs_review_query()) if payee_key(t) == name]


@app.get("/review")
def review_page(request: Request, s: Session = Depends(session)):
    ctx = Ctx(s, today())
    groups = review_groups(ctx)
    return templates.TemplateResponse(request, "review.html", {
        "groups": groups, "cats": s.exec(select(Category).order_by(Category.name)).all(),
        "accounts": ctx.accounts, "count": sum(len(g["txns"]) for g in groups),
        "new_count": sum(1 for g in groups if g["new"]), "msg": request.query_params.get("msg", ""),
        "page": "transactions"})


@app.post("/review/file")
def review_file(payee: str = Form(...), category_id: int = Form(...), always: bool = Form(False),
                s: Session = Depends(session)):
    """File every waiting transaction from this payee, and optionally all future ones too."""
    cat = s.get(Category, category_id)
    txns = _review_group(s, payee)
    if not cat or not txns:
        return back("/review")
    for t in txns:
        t.reviewed = True
        if not always:
            t.category_id, t.category_source = cat.id, "manual"
        s.add(t)
    activity.record(s, "change", f"{len(txns)} from {payee} filed under {cat.name}",
                    [f"{t.date.strftime('%-d %b')} {activity.money(t.amount)} {activity.payee(t)}" for t in txns]
                    + (["And all future ones from this payee."] if always else []),
                    url="/transactions?search=" + quote(payee[:40]))
    s.commit()
    if always:
        learn(s, txns[0], cat.id)  # adds the rule and re-sorts, so these become "learned from you"
    return back(f"/review?msg={'always' if always else 'filed'}")


@app.post("/review/accept")
def review_accept(payee: str = Form(""), everything: bool = Form(False), s: Session = Depends(session)):
    """The guess is right: keep the category and take it off the list."""
    txns = s.exec(needs_review_query()).all() if everything else _review_group(s, payee)
    for t in txns:
        t.reviewed = True
        s.add(t)
    if txns:
        names = activity.Names(s)
        activity.record(s, "change", f"{len(txns)} guessed categor{'ies' if len(txns) != 1 else 'y'} accepted",
                        [f"{t.date.strftime('%-d %b')} {activity.money(t.amount)} {activity.payee(t)}: "
                         f"{names.category(t.category_id)}" for t in txns], url="/review")
    s.commit()
    return back("/review?msg=accepted")


# ---------- Activity log ----------
ACTIVITY_PAGE = 100


def _local(dt: datetime) -> datetime:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(TZ)


templates.env.filters["local_time"] = lambda dt: _local(dt).strftime("%-I:%M %p").lower()
templates.env.filters["local_when"] = lambda dt: _local(dt).strftime("%A %-d %B %Y, %-I:%M %p").replace("AM", "am").replace("PM", "pm")


@app.get("/activity")
def activity_page(request: Request, kind: str = "", q: str = "", before: int = 0, s: Session = Depends(session)):
    query = select(Activity)
    kind = kind if kind in activity.KIND_NAMES else ""
    if kind:
        query = query.where(Activity.kind == kind)
    q = q.strip()[:80]
    if q:
        like = f"%{q}%"
        query = query.where(or_(Activity.title.ilike(like), Activity.body.ilike(like), Activity.who.ilike(like)))
    if before:
        query = query.where(Activity.id < before)
    rows = s.exec(query.order_by(Activity.id.desc()).limit(ACTIVITY_PAGE + 1)).all()
    more = len(rows) > ACTIVITY_PAGE
    rows = rows[:ACTIVITY_PAGE]
    days: list[tuple[date, list]] = []
    for r in rows:
        d = _local(r.at).date()
        if not days or days[-1][0] != d:
            days.append((d, []))
        days[-1][1].append(r)
    return templates.TemplateResponse(request, "activity.html", {
        "days": days, "kind": kind, "q": q, "kinds": activity.KINDS, "more": rows[-1].id if more else 0,
        "today": today(), "off": activity.off_kinds(s), "page": "settings", "tab": "activity",
        "nav": settings_nav(request)})


@app.get("/activity/{entry_id}")
def activity_entry(request: Request, entry_id: int, s: Session = Depends(session)):
    row = s.get(Activity, entry_id)
    if not row:
        return back("/activity?msg=gone")
    try:
        extra = json.loads(row.data) if row.data else {}
    except ValueError:
        extra = {}
    for r in extra.get("recipients", []):
        try:
            r["when"] = datetime.fromisoformat(r["at"])
        except (KeyError, ValueError):
            r["when"] = None
    txn = s.get(Transaction, row.ref) if row.ref and not row.ref.startswith("notify:") else None
    related = s.exec(select(Activity).where(Activity.ref == row.ref, Activity.id != row.id)
                     .order_by(Activity.id.desc()).limit(20)).all() if row.ref else []
    return templates.TemplateResponse(request, "activity_entry.html", {
        "e": row, "extra": extra, "kind_name": activity.KIND_NAMES.get(row.kind, row.kind),
        "link_label": activity.label_for(row.url) if row.url else "", "txn": txn, "related": related,
        "now_alloc": activity.allocation(txn, activity.Names(s)) if txn else "",
        "cats": s.exec(select(Category).order_by(Category.name)).all() if txn else [],
        "bills": s.exec(select(Bill).where(Bill.active == True).order_by(Bill.name)).all() if txn else [],  # noqa: E712
        "page": "settings", "tab": "activity", "nav": settings_nav(request)})


@app.post("/activity/settings")
async def activity_settings(request: Request, s: Session = Depends(session)):
    form = await request.form()
    try:
        keep = int(str(form.get("keep", activity.DEFAULT_KEEP)))
    except ValueError:
        keep = activity.DEFAULT_KEEP
    before = (sorted(activity.off_kinds(s)), activity.keep_days(s))
    activity.save_settings(s, [str(k) for k in form.getlist("on")], keep)
    after_off, after_keep = sorted(activity.off_kinds(s)), activity.keep_days(s)
    if (after_off, after_keep) != before:
        activity.record(s, "change", "Activity log settings changed",
                        [f"Recording: {', '.join(n for k, n in activity.KINDS if k not in after_off) or 'nothing'}",
                         f"Keeping entries for: {dict(activity.KEEP_CHOICES).get(after_keep, after_keep)}"],
                        url="/settings?tab=log", commit=True, force=True)
    activity.prune(s)
    return _settings("log", msg="saved")


@app.post("/activity/clear")
def activity_clear(confirm: str = Form(""), s: Session = Depends(session)):
    if confirm.strip().lower() != "clear":
        return _settings("log", msg="noclear")
    n = len(s.exec(select(Activity.id)).all())
    s.exec(sql_delete(Activity))
    s.commit()
    activity.record(s, "change", "Activity log cleared", [f"{n} entr{'ies' if n != 1 else 'y'} removed."],
                    url="/settings?tab=log", commit=True, force=True)
    return _settings("log", msg="cleared")


# ---------- Bills ----------
@app.get("/bills")
def bills_page(request: Request, from_txn: str | None = None, s: Session = Depends(session)):
    txn = s.get(Transaction, from_txn) if from_txn else None
    prefill = suggest_bill(s, txn) if txn and txn.amount < 0 else None
    bills = s.exec(select(Bill).order_by(Bill.name)).all()
    start, end = period_of(s, today())
    links = {(t.bill_id, t.bill_due_date): t for t in
             s.exec(select(Transaction).where(Transaction.bill_id != None))}  # noqa: E711
    pend = match_pending(s, bills)
    status = {b.id: [{"due": d, "txn": links.get((b.id, d)), "pending": pend.get((b.id, d))}
                     for d in occurrences(b, start, end)] for b in bills}
    ctx = Ctx(s, today())
    dismissed = set(json.loads(get_setting(s, "dismissed_subscriptions") or "[]"))
    suggestions = detect_subscriptions(ctx, bills, dismissed)
    return templates.TemplateResponse(request, "bills.html", {
        "bills": bills, "status": status, "start": start, "end": end, "suggestions": suggestions,
        "cats": s.exec(select(Category).order_by(Category.name)).all(),
        "today": today(), "prefill": prefill, "bill_msg": request.query_params.get("bill", ""),
        "pay_accounts": _pay_accounts(s), "page": "bills"})


@app.get("/calendar")
def calendar_page(request: Request, view: str = "pay", offset: int = 0, s: Session = Depends(session)):
    from .billcal import build_calendar
    c = build_calendar(s, today(), view, offset)
    return templates.TemplateResponse(request, "calendar.html", {"c": c, "page": "calendar"})


@app.post("/bills/suggestions/dismiss")
def dismiss_suggestion(name: str = Form(...), s: Session = Depends(session)):
    dismissed = set(json.loads(get_setting(s, "dismissed_subscriptions") or "[]"))
    dismissed.add(name.lower())
    set_setting(s, "dismissed_subscriptions", json.dumps(sorted(dismissed)))
    return back("/bills#suggested")


@app.post("/bills/suggestions/reset")
def reset_suggestions(s: Session = Depends(session)):
    set_setting(s, "dismissed_subscriptions", "[]")
    return back("/bills#suggested")


def _bill_values(name: str, amount: float, frequency: str, match_pattern: str, tolerance_pct: float,
                 window_days: int, category_id: str, s: Session, account_id: str = "") -> dict | None:
    """Checked form values for a bill, or None if they can't make a working bill."""
    if not name.strip() or abs(amount) < 0.005 or frequency not in APPROX_DAYS or not valid_pattern(match_pattern):
        return None
    cid = int(category_id) if category_id.isdigit() and s.get(Category, int(category_id)) else None
    return dict(name=name.strip()[:80], amount=round(abs(amount), 2), frequency=frequency,
                match_pattern=match_pattern.strip(), tolerance_pct=min(max(tolerance_pct, 0), 100),
                window_days=min(max(window_days, 0), 31), category_id=cid,
                account_id=account_id if account_id and s.get(Account, account_id) else None)


def _pay_accounts(s: Session) -> list[Account]:
    """Accounts a bill can come out of: everything but loans."""
    return [a for a in s.exec(select(Account).order_by(Account.name)) if a.type != "LOAN"]


@app.post("/bills")
def add_bill(name: str = Form(...), amount: float = Form(...), frequency: str = Form(...),
             anchor_date: date = Form(...), match_pattern: str = Form(...),
             tolerance_pct: float = Form(10), window_days: int = Form(4),
             category_id: str = Form(""), account_id: str = Form(""), s: Session = Depends(session)):
    values = _bill_values(name, amount, frequency, match_pattern, tolerance_pct, window_days, category_id, s, account_id)
    if not values:
        return back("/bills?bill=bad#add")
    bill = Bill(anchor_date=anchor_date, **values)
    s.add(bill)
    s.flush()
    activity.record(s, "change", f"Bill added: {bill.name}", _bill_lines(s, bill), amount=-bill.amount,
                    url=f"/bills/{bill.id}")
    s.commit()
    recategorise_all(s, f"The bill {bill.name} was added.")  # match existing transactions to the new bill
    return back("/bills")


def _bill_lines(s: Session, bill: Bill) -> list[str]:
    cat = s.get(Category, bill.category_id) if bill.category_id else None
    return [f"{activity.money(bill.amount)} {bill.frequency}, from {bill.anchor_date.strftime('%-d %b %Y')}",
            f"Matches statement text: {bill.match_pattern}", f"Category: {cat.name if cat else 'Bills'}",
            "Active" if bill.active else "Paused"]


@app.get("/bills/{bill_id}")
def bill_detail(request: Request, bill_id: int, s: Session = Depends(session)):
    bill = s.get(Bill, bill_id)
    if not bill:
        return back("/bills")
    linked = s.exec(select(Transaction).where(Transaction.bill_id == bill_id)
                    .order_by(Transaction.date.desc())).all()
    pending = sorted(((d, p) for (bid, d), p in match_pending(s).items() if bid == bill_id), key=lambda x: x[0])
    paid_dates = {t.bill_due_date for t in linked} | {d for d, _ in pending}
    t0 = today()
    first = s.exec(select(Transaction).order_by(Transaction.date)).first()
    since = max(t0 - timedelta(days=365), first.date if first else t0)
    past_year = occurrences(bill, since, t0 + timedelta(days=1))
    missed = [d for d in past_year if d not in paid_dates]
    upcoming = occurrences(bill, t0 + timedelta(days=1), t0 + timedelta(days=400))[:4]
    amounts = [-t.amount for t in linked]
    return templates.TemplateResponse(request, "bill_detail.html", {
        "bill": bill, "linked": linked, "missed": missed[-6:], "upcoming": upcoming, "pending": pending,
        "avg": sum(amounts) / len(amounts) if amounts else 0,
        "accounts": {a.id: a for a in s.exec(select(Account))}, "pay_accounts": _pay_accounts(s),
        "cats": s.exec(select(Category).order_by(Category.name)).all(),
        "today": t0, "bill_msg": request.query_params.get("bill", ""), "page": "bills"})


@app.post("/bills/{bill_id}")
def edit_bill(bill_id: int, name: str = Form(...), amount: float = Form(...),
              frequency: str = Form(...), anchor_date: date = Form(...),
              match_pattern: str = Form(...), tolerance_pct: float = Form(10),
              window_days: int = Form(4), category_id: str = Form(""), active: bool = Form(False),
              account_id: str = Form(""), s: Session = Depends(session)):
    bill = s.get(Bill, bill_id)
    values = _bill_values(name, amount, frequency, match_pattern, tolerance_pct, window_days, category_id, s, account_id)
    if not bill:
        return back("/bills")
    if not values:
        return back(f"/bills/{bill_id}?bill=bad#edit")
    for field, value in values.items():
        setattr(bill, field, value)
    bill.anchor_date, bill.active = anchor_date, active
    s.add(bill)
    activity.record(s, "change", f"Bill changed: {bill.name}", _bill_lines(s, bill), amount=-bill.amount,
                    url=f"/bills/{bill.id}")
    # due dates may have moved: re-point the links you made by hand, oldest first
    manual = s.exec(select(Transaction).where(Transaction.bill_id == bill_id,
                                              Transaction.category_source == "manual")
                    .order_by(Transaction.date)).all()
    for t in manual:
        t.bill_due_date = None
    s.flush()
    for t in manual:
        t.bill_due_date = nearest_unpaid_due(s, bill, t)
        s.add(t)
        s.flush()
    s.commit()
    recategorise_all(s, f"The bill {bill.name} was changed.")
    return back(f"/bills/{bill_id}")


@app.post("/bills/{bill_id}/delete")
def delete_bill(bill_id: int, s: Session = Depends(session)):
    bill = s.get(Bill, bill_id)
    if bill:
        for t in s.exec(select(Transaction).where(Transaction.bill_id == bill_id)):
            t.bill_id = t.bill_due_date = None
            s.add(t)
        s.delete(bill)
        activity.record(s, "change", f"Bill deleted: {bill.name}", [f"{activity.money(bill.amount)} {bill.frequency}"],
                        url="/bills")
        s.commit()
        recategorise_all(s, f"The bill {bill.name} was deleted.")
    return back("/bills")


# ---------- Payees ----------
@app.get("/payees")
def payees(request: Request, offset: int = 0, range: str = "period", q: str = "",
           s: Session = Depends(session)):
    ctx = Ctx(s, today())
    start, end = ctx.period(offset)
    txns = ctx.txns() if range == "all" else ctx.txns(start, end)
    groups = payee_groups(ctx, txns)
    if q:
        groups = [g for g in groups if q.lower() in g["name"].lower()]
    return templates.TemplateResponse(request, "payees.html", {
        "groups": groups, "start": start, "end": end, "offset": offset, "range": range, "q": q,
        "total_out": sum(g["out"] for g in groups), "page": "payees"})


@app.get("/payees/detail")
def payee(request: Request, name: str, s: Session = Depends(session)):
    ctx = Ctx(s, today())
    d = payee_detail(ctx, name)
    chart = bars([h["value"] for h in d["history"]], [h["label"] for h in d["history"]],
                 hrefs=[f"/transactions?offset={h['offset']}&payee={quote(name)}" for h in d["history"]],
                 show_values=not is_private())
    return templates.TemplateResponse(request, "payee_detail.html", {
        "d": d, "chart": chart, "accounts": ctx.accounts, "cats": ctx.cats, "page": "payees"})


# ---------- Summary ----------
@app.get("/summary")
def summary(request: Request, offset: int = 0, s: Session = Depends(session)):
    offset = min(offset, 0)
    ctx = Ctx(s, today())
    cur, prev = period_summary(ctx, offset), period_summary(ctx, offset - 1)
    dash = build_dashboard(s, today(), offset)
    o = f"&offset={offset}" if offset else ""
    # Day rate: after each day's spending, what's left of this pay's budget spread over the days that remain
    # (that day included). Today's point is the Overview's "a day until payday" figure.
    trend, spent_so_far = [], 0.0
    if dash["income"] > 0:
        for i, d in enumerate(cur["daily"]):
            if d["future"]:
                trend.append(None)
                continue
            spent_so_far += d["value"]
            trend.append((dash["budget"] - spent_so_far) / (cur["days"] - i))
    day_rate = next((t for t in reversed(trend) if t is not None), None)
    warn = rate_warning(s)
    rate_chart = None
    if trend:
        rate_chart = bars([max(t or 0, 0) for t in trend], [d["date"].strftime("%-d %b") for d in cur["daily"]],
                          target=warn, target_cls="warn",
                          highlight=[t is not None for t in trend],
                          axis=[d["date"].strftime("%-d") for d in cur["daily"]], show_values=not is_private(),
                          low=[t is not None and warn is not None and t < warn for t in trend], height=140)
    # While the period is still running, bills still to come aren't kept money yet: take them off "Kept",
    # and show them as their own slice of where the income went.
    in_progress = offset == 0 and today() < cur["end"]
    to_come = dash["to_come"] if in_progress else 0.0
    kept = cur["saved"] - to_come
    where = [("Bills", cur["bills"], f"/transactions?view=bills{o}")]
    if to_come > 0:
        where.append(("Bills to come", to_come, "/#bills"))
    where.append(("Day to day", cur["day_to_day"], f"/transactions?view=daytoday{o}"))
    if kept > 0:
        where.append(("Kept so far" if in_progress else "Kept", kept, f"/?offset={offset}" if offset else "/"))
    prev_cats = {name: v for name, v, _ in prev["categories"]}
    cat_change = {name: change(v, prev_cats.get(name, 0)) for name, v, _ in cur["categories"]}
    cat_slices = [(name, v, f"/transactions?category={cid}{o}") for name, v, cid in cur["categories"]]
    return templates.TemplateResponse(request, "summary.html", {
        "c": cur, "p": prev, "dash": dash, "page": "summary",
        "cat_chart": donut(cat_slices), "where_chart": donut(where), "o": o,
        "daily_chart": bars([d["value"] for d in cur["daily"]], [d["date"].strftime("%-d %b") for d in cur["daily"]],
                            target=dash["planned_per_day"] if dash["planned_per_day"] > 0 else None,
                            highlight=[not d["future"] for d in cur["daily"]],
                            hrefs=[f"/transactions?view=daytoday&day={d['date'].isoformat()}{o}" for d in cur["daily"]],
                            axis=[d["date"].strftime("%-d") for d in cur["daily"]], show_values=not is_private(),
                            part=[d["pending"] for d in cur["daily"]],
                            stack=[d["bills"] for d in cur["daily"]],
                            stack_hrefs=[f"/transactions?view=bills&day={d['date'].isoformat()}{o}" for d in cur["daily"]]),
        "day_rate": day_rate, "rate_chart": rate_chart, "warn": warn,
        "chg": {"spent": change(cur["spent"], prev["spent"]), "income": change(cur["income"], prev["income"]),
                "per_day": change(cur["per_day"], prev["per_day"]), "day_to_day": change(cur["day_to_day"], prev["day_to_day"])},
        "cat_change": cat_change, "limits": dash["limits"],
        "kept": kept, "in_progress": in_progress, "to_come": to_come})


@app.get("/trends")
def trends_page(request: Request, pays: int = 6, s: Session = Depends(session)):
    """Several pay periods side by side: are we doing better than we were?"""
    pays = 12 if pays >= 12 else 6
    t = trends(Ctx(s, today()), pays)
    ps = t["periods"]
    labels = [p["start"].strftime("%-d %b") for p in ps]
    chart = bars([p["day_to_day"] for p in ps], [f"{lbl}: ${p['spent']:,.0f} spent" for lbl, p in zip(labels, ps)],
                 target=t["average"]["income"] or None, highlight=[not p["current"] for p in ps], axis=labels,
                 show_values=not is_private(), stack=[p["bills"] for p in ps],
                 hrefs=[f"/transactions?view=daytoday&offset={i - len(ps) + 1}" for i in range(len(ps))],
                 stack_hrefs=[f"/transactions?view=bills&offset={i - len(ps) + 1}" for i in range(len(ps))], height=180)
    return templates.TemplateResponse(request, "trends.html", {"t": t, "chart": chart, "pays": pays,
                                                               "page": "summary"})


@app.get("/networth")
def networth_page(request: Request, months: int = 12, s: Session = Depends(session)):
    """Everything you have less everything you owe, and how that's moved month by month."""
    from . import networth
    months = 24 if months >= 24 else 12
    n = networth.build(s, today(), months)
    return templates.TemplateResponse(request, "networth.html", {"n": n, "chart": networth.line_chart(n["history"]),
                                                                 "months": months, "page": "networth"})


def rate_warning(s: Session) -> float | None:
    try:
        v = float(get_setting(s, "day_rate_warning") or 0)
    except ValueError:
        return None
    return v if v > 0 else None


@app.post("/summary/warning")
def summary_warning(request: Request, amount: str = Form(""), offset: int = Form(0), s: Session = Depends(session)):
    v = _float(amount)
    set_setting(s, "day_rate_warning", f"{min(abs(v), 100000):.2f}" if v else "")
    s.commit()
    return back(f"/summary?offset={min(offset, 0)}#day-rate" if offset else "/summary#day-rate")


# ---------- Goals ----------
def _goal_values(s: Session, name: str, target: str, target_date: str, account_id: str, start_amount: str,
                 budget_it: str) -> dict | None:
    name, amount = name.strip()[:80], _float(target)
    if not name or not amount or amount <= 0 or amount > 100_000_000:
        return None
    acc = s.get(Account, account_id) if account_id else None
    return {"name": name, "target": round(amount, 2), "target_date": _day(target_date),
            "account_id": acc.id if acc else None,
            "start_amount": round(abs(_float(start_amount) or 0), 2) if acc else 0,
            "budget_it": budget_it == "true"}


@app.get("/goals")
def goals_page(request: Request, s: Session = Depends(session)):
    from .forecast import account_balance
    from .goals import all_goals
    accounts = s.exec(select(Account).order_by(Account.name)).all()
    balances = {a.id: account_balance(s, a) for a in accounts}
    rows = all_goals(s, today(), balances)
    return templates.TemplateResponse(request, "goals.html", {
        "goals": rows, "accounts": accounts, "balances": balances, "page": "goals",
        "today": today(), "calendar": get_cycle(s).calendar,
        "bad": request.query_params.get("bad") == "1"})


@app.post("/goals")
def add_goal(name: str = Form(""), target: str = Form(""), target_date: str = Form(""), account_id: str = Form(""),
             start_amount: str = Form(""), budget_it: str = Form(""), saved: str = Form(""),
             s: Session = Depends(session)):
    values = _goal_values(s, name, target, target_date, account_id, start_amount, budget_it)
    if not values:
        return back("/goals?bad=1#add")
    g = Goal(created=today(), **values)
    if not g.account_id:
        g.manual_saved = round(abs(_float(saved) or 0), 2)
    s.add(g)
    s.flush()
    activity.record(s, "change", f"Goal added: {g.name}", [f"Target {activity.money(g.target)}"
                    + (f" by {g.target_date.strftime('%-d %b %Y')}" if g.target_date else "")]
                    + ([f"Already saved {activity.money(g.manual_saved)}"] if g.manual_saved else []),
                    url=f"/goals#g-{g.id}")
    s.commit()
    return back(f"/goals#g-{g.id}")


@app.post("/goals/{goal_id}")
def edit_goal(goal_id: int, name: str = Form(""), target: str = Form(""), target_date: str = Form(""),
              account_id: str = Form(""), start_amount: str = Form(""), budget_it: str = Form(""),
              s: Session = Depends(session)):
    g = s.get(Goal, goal_id)
    values = _goal_values(s, name, target, target_date, account_id, start_amount, budget_it) if g else None
    if not values:
        return back(f"/goals?bad=1#g-{goal_id}")
    for k, v in values.items():
        setattr(g, k, v)
    s.add(g)
    activity.record(s, "change", f"Goal changed: {g.name}", [f"Target {activity.money(g.target)}"
                    + (f" by {g.target_date.strftime('%-d %b %Y')}" if g.target_date else "")], url=f"/goals#g-{g.id}")
    s.commit()
    return back(f"/goals#g-{g.id}")


@app.post("/goals/{goal_id}/add")
def goal_add_money(goal_id: int, amount: str = Form(""), action: str = Form("add"), s: Session = Depends(session)):
    g = s.get(Goal, goal_id)
    v = _float(amount)
    if g and not g.account_id and v:
        was = g.manual_saved or 0
        if action == "set":
            g.manual_saved = round(max(v, 0), 2)
        else:
            g.manual_saved = round(max((g.manual_saved or 0) + (-abs(v) if action == "take" else abs(v)), 0), 2)
        s.add(g)
        moved = round(g.manual_saved - was, 2)
        activity.record(s, "allocation", f"{activity.money(abs(moved))} {'put towards' if moved >= 0 else 'taken from'} {g.name}",
                        [f"Saved was {activity.money(was)}, now {activity.money(g.manual_saved)} of {activity.money(g.target)}"],
                        amount=moved, url=f"/goals#g-{g.id}")
        s.commit()
    return back(f"/goals#g-{goal_id}")


@app.post("/goals/{goal_id}/done")
def goal_done(goal_id: int, s: Session = Depends(session)):
    g = s.get(Goal, goal_id)
    if g:
        g.done = not g.done
        s.add(g)
        activity.record(s, "change", f"Goal {'marked done' if g.done else 'reopened'}: {g.name}", url=f"/goals#g-{g.id}")
        s.commit()
    return back(f"/goals#g-{goal_id}")


@app.post("/goals/{goal_id}/delete")
def goal_delete(goal_id: int, s: Session = Depends(session)):
    g = s.get(Goal, goal_id)
    if g:
        s.delete(g)
        activity.record(s, "change", f"Goal deleted: {g.name}", [f"Target was {activity.money(g.target)}"], url="/goals")
        s.commit()
    return back("/goals")


# ---------- Loans (a mortgage, a car loan, ...) ----------
def the_loan(s: Session) -> Loan | None:
    return s.exec(select(Loan).order_by(Loan.id)).first()


def has_loan() -> bool:
    if current_profile.get() is None:
        return False
    with Session(current_engine()) as s:
        return the_loan(s) is not None


templates.env.globals["has_loan"] = has_loan


def _float(v: str) -> float | None:
    try:
        return float(v.replace("$", "").replace(",", "").replace("%", "").strip()) if v and v.strip() else None
    except ValueError:
        return None


def _day(v: str) -> date | None:
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _months(d: date | None) -> int | None:
    return d.year * 12 + d.month if d else None


@app.get("/loans")
def loans_page(request: Request, s: Session = Depends(session)):
    loans = s.exec(select(Loan).order_by(Loan.id)).all()
    rows = []
    for loan in loans:
        m = mortgage.summary(s, loan, today())
        first = loan.original_amount or ((m["history"][0].balance + m["history"][0].principal) if m["history"] else m["owed"])
        rows.append({"m": m, "first": first,
                     "repaid_pct": max(0.0, min(1.0, (first - m["owed"]) / first)) if first else 0})
    linked = {loan.account_id for loan in loans if loan.account_id}
    spare = [a for a in s.exec(select(Account).where(Account.type == "LOAN").order_by(Account.name)) if a.id not in linked]
    return templates.TemplateResponse(request, "loans.html", {
        "rows": rows, "spare": spare, "kinds": mortgage.KINDS,
        "owed": sum(r["m"]["owed"] for r in rows), "worth": sum(r["m"]["value"] for r in rows if r["m"]["has_asset"]),
        "page": "loans"})


@app.get("/loans/new")
def loan_new(request: Request, kind: str = "car", account: str = "", s: Session = Depends(session)):
    return _setup_page(request, s, None, kind, account)


@app.get("/loans/account/{acc_id}")
def loan_for_account(acc_id: str, s: Session = Depends(session)):
    """From a loan account's page: its loan, or a new one set up with it."""
    loan = s.exec(select(Loan).where(Loan.account_id == acc_id)).first()
    return back(f"/loans/{loan.id}" if loan else f"/loans/new?account={quote(acc_id)}")


@app.get("/loans/{loan_id}")
def loan_page(request: Request, loan_id: int, extra: str = "", lump: str = "", lump_date: str = "", refix: str = "",
              s: Session = Depends(session)):
    loan = s.get(Loan, loan_id)
    if not loan:
        return back("/loans")
    what = mortgage.WhatIf(extra=abs(_float(extra) or 0), lump=abs(_float(lump) or 0), lump_date=_day(lump_date),
                           refix_rate=_float(refix))
    if what.lump and not what.lump_date:
        what.lump_date = today()
    m = mortgage.summary(s, loan, today(), what)
    base, alt = m["base"], m["alt"]
    start = m["history"][0].date if m["history"] else m["since"]
    first_balance = (m["history"][0].balance + m["history"][0].principal) if m["history"] else m["owed"]

    def points(rows, lead=None):
        pts = ([lead] if lead else []) + [(r.date, r.balance) for r in rows]
        return pts[:: max(1, len(pts) // 240)] + ([pts[-1]] if pts else [])
    anchor = (m["since"], m["owed"])
    series = [{"name": "Owed so far", "points": [(start, first_balance)] + [(r.date, r.balance) for r in m["history"]],
               "cls": "s-actual", "fill": True},
              {"name": "Forecast", "points": points(base.rows, anchor), "cls": "s-forecast", "dashed": True}]
    if alt:
        series.append({"name": "With your what-if", "points": points(alt.rows, anchor), "cls": "s-what"})
    balance_chart = mortgage.line_chart(series, marker=today(), label=f"{loan.name} balance over time")

    equity_chart = None
    if m["has_asset"]:
        end = base.payoff or m["maturity"]
        vals = mortgage.asset_series(loan, m["asset_points"], end) or [(start, m["value"]), (end, m["value"])]
        equity_series = [{"name": m["asset_name"], "points": vals, "cls": "s-value"},
                         {"name": "Owed", "points": [(start, first_balance)] + [(r.date, r.balance) for r in m["history"] + base.rows],
                          "cls": "s-actual", "fill": True}]
        equity_chart = mortgage.line_chart(equity_series, height=200, marker=today(),
                                           label=f"{m['asset_name']} value and amount owed")
    plan = alt or base
    paid = m["paid"]
    first = loan.original_amount or first_balance
    cost = donut([("Principal", first), ("Interest paid", paid["interest"]), ("Interest to come", base.interest)])
    compare = None
    if alt:
        sooner = (_months(base.payoff) - _months(alt.payoff)) if base.payoff and alt.payoff else None
        days = (base.payoff - alt.payoff).days if base.payoff and alt.payoff else None
        saved = base.interest - alt.interest
        fee = loan.early_fee if (what.extra or what.lump) and loan.early_fee else 0.0
        compare = {"sooner": sooner, "weeks": round(days / 7) if days else 0, "saved": saved, "fee": fee,
                   "net": saved - fee, "extra_paid": sum(r.extra for r in alt.rows) + what.lump}

    paid_from = mortgage.paid_from(s, loan)
    bill, bill_linked = mortgage.repayment_bill(s, loan, paid_from)
    bills = s.exec(select(Bill).where(Bill.active == True).order_by(Bill.name)).all()  # noqa: E712
    bills.sort(key=lambda b: abs(b.amount - m["payment"]))  # closest to the repayment first
    return templates.TemplateResponse(request, "mortgage.html", {
        "cost": cost, "compare": compare, "first": first,
        "left_months": (_months(base.payoff) - _months(today())) if base.payoff else None,
        "repaid_pct": max(0.0, min(1.0, (first - m["owed"]) / first)) if first else 0,
        "m": m, "balance_chart": balance_chart, "equity_chart": equity_chart,
        "split_chart": mortgage.yearly_split(m["history"] + base.rows, today()),
        "upcoming": base.rows[:12], "plan": plan, "today": today(),
        "pay_account": s.get(Account, loan.pay_account_id) if loan.pay_account_id else None,
        "bill": bill, "bill_linked": bill_linked, "bills": bills, "paid_from": paid_from[0] if paid_from else None,
        "bill_msg": request.query_params.get("bill", ""),
        "form": {"extra": extra, "lump": lump, "lump_date": lump_date, "refix": refix},
        "page": "loans"})


def _setup_page(request: Request, s: Session, loan: Loan | None, kind: str = "home", account: str = ""):
    accounts = s.exec(select(Account).order_by(Account.name)).all()
    lid = loan.id if loan else None
    rates = s.exec(select(LoanRate).where(LoanRate.loan_id == lid).order_by(LoanRate.effective)).all() if loan else []
    m = mortgage.summary(s, loan, today()) if loan else None
    return templates.TemplateResponse(request, "mortgage_setup.html", {
        "loan": loan, "kind": loan.kind if loan else (kind if kind in mortgage.KINDS else "home"),
        "kinds": mortgage.KINDS, "accounts": accounts, "new_account": account,
        "entries": s.exec(select(LoanEntry).where(LoanEntry.loan_id == lid).order_by(LoanEntry.date)).all() if loan else [],
        "rates": rates, "rate_now": m["rate_now"] if m and rates else None,
        "values": s.exec(select(PropertyValue).where(PropertyValue.loan_id == lid).order_by(PropertyValue.date)).all() if loan else [],
        "meta": mortgage.loan_meta(s.get(Account, loan.account_id)) if loan and loan.account_id else {},
        "default_dep": mortgage.DEFAULT_DEPRECIATION,
        "msg": request.query_params.get("msg", ""), "count": request.query_params.get("n", ""), "page": "loans"})


@app.get("/loans/{loan_id}/setup")
def loan_setup(request: Request, loan_id: int, s: Session = Depends(session)):
    loan = s.get(Loan, loan_id)
    if not loan:
        return back("/loans")
    return _setup_page(request, s, loan)


def _save_loan(s: Session, loan: Loan, f: dict) -> None:
    kind = f["kind"] if f["kind"] in mortgage.KINDS else "home"
    loan.kind = kind
    loan.name = " ".join(f["name"].split())[:60] or mortgage.KINDS[kind]
    loan.account_id = f["account_id"] if f["account_id"] and s.get(Account, f["account_id"]) else None
    loan.pay_account_id = f["pay_account_id"] if f["pay_account_id"] and s.get(Account, f["pay_account_id"]) else None
    loan.original_amount = abs(_float(f["original_amount"]) or loan.original_amount or 0)
    loan.start_date = _day(f["start_date"]) or loan.start_date
    term = _float(f["term"])
    if term and f["term_unit"] == "months":
        loan.term_months = min(max(int(term), 1), 600)
        loan.term_years = max(1, round(loan.term_months / 12))
    elif term:
        loan.term_years, loan.term_months = min(max(int(term), 1), 50), None
    loan.deposit = abs(_float(f["deposit"]) or 0)
    loan.frequency = f["frequency"] if f["frequency"] in mortgage.FREQUENCIES else "monthly"
    loan.payment_day = min(max(int(_float(f["payment_day"]) or loan.payment_day or 20), 1), 31)
    loan.repay_date = _day(f["repay_date"])
    pay = _float(f["repayment"])
    loan.repayment = abs(pay) if pay else None
    loan.early_fee = abs(_float(f["early_fee"]) or 0)
    loan.fixed_until = _day(f["fixed_until"])
    loan.rate_after = _float(f["rate_after"])
    loan.payment_after = abs(_float(f["payment_after"])) if _float(f["payment_after"]) else None
    loan.count_asset = kind != "personal" and f["count_asset"]
    dep = _float(f["depreciation"])
    loan.depreciation = min(max(dep, 0), 90) if dep is not None else None
    s.add(loan)
    s.commit()
    s.refresh(loan)
    rate = _float(f["rate"])
    if rate is not None and 0 <= rate < 40:  # the rate now: start the history with it, or add it if it's changed
        have = sorted((r.effective, r.rate) for r in s.exec(select(LoanRate).where(LoanRate.loan_id == loan.id)))
        if not have:
            s.add(LoanRate(loan_id=loan.id, effective=loan.start_date or today(), rate=rate, note="When set up"))
        elif abs(mortgage.rate_on(have, today()) - rate) > 1e-6:
            s.add(LoanRate(loan_id=loan.id, effective=today(), rate=rate, note="Changed in setup"))
    changed = False
    for acc_id in {loan.account_id if f["leave_out"] else None, loan.pay_account_id if f["leave_out_pay"] else None} - {None}:
        acc = s.get(Account, acc_id)
        if acc.in_budget or acc.count_spending:  # the loan looks after itself: keep it out of the budget
            acc.in_budget = acc.count_spending = False
            s.add(acc)
            changed = True
    s.commit()
    if changed:
        recategorise_all(s)


async def _loan_form(request: Request) -> dict:
    form = await request.form()
    keys = ("kind", "name", "account_id", "pay_account_id", "original_amount", "start_date", "term", "term_unit",
            "deposit", "frequency", "payment_day", "repay_date", "repayment", "early_fee", "rate", "fixed_until",
            "rate_after", "payment_after", "depreciation")
    f = {k: str(form.get(k) or "") for k in keys}
    for k in ("leave_out", "leave_out_pay", "count_asset"):
        f[k] = form.get(k) == "true"
    return f


@app.post("/loans/new")
async def loan_create(request: Request, s: Session = Depends(session)):
    loan = Loan()
    _save_loan(s, loan, await _loan_form(request))
    return back(f"/loans/{loan.id}")


@app.post("/loans/{loan_id}/setup")
async def loan_save(request: Request, loan_id: int, s: Session = Depends(session)):
    loan = s.get(Loan, loan_id)
    if not loan:
        return back("/loans")
    _save_loan(s, loan, await _loan_form(request))
    return back(f"/loans/{loan.id}/setup?msg=saved")


@app.post("/loans/{loan_id}/delete")
def loan_delete(loan_id: int, s: Session = Depends(session)):
    loan = s.get(Loan, loan_id)
    if loan:
        for model in (LoanEntry, LoanRate, PropertyValue):
            for row in s.exec(select(model).where(model.loan_id == loan_id)):
                s.delete(row)
        s.delete(loan)
        s.commit()
    return back("/loans")


@app.post("/loans/{loan_id}/import")
async def loan_import(loan_id: int, file: UploadFile, s: Session = Depends(session)):
    loan = s.get(Loan, loan_id)
    if not loan:
        return back("/loans")
    raw = await file.read(2_000_000)
    try:
        parsed = mortgage.parse_tracker(raw.decode("utf-8-sig", errors="replace"))
    except Exception:  # noqa: BLE001 - any unreadable file gets the same message
        parsed = {"entries": []}
    if not parsed.get("entries"):
        return back(f"/loans/{loan_id}/setup?msg=badimport")
    n = mortgage.import_tracker(s, loan, parsed)
    return back(f"/loans/{loan_id}/setup?msg=imported&n={n}")


@app.post("/loans/{loan_id}/rates")
def loan_add_rate(loan_id: int, effective: str = Form(...), rate: str = Form(...), note: str = Form(""),
                  s: Session = Depends(session)):
    loan, d, r = s.get(Loan, loan_id), _day(effective), _float(rate)
    if not loan:
        return back("/loans")
    if not d or r is None or not 0 <= r < 40:
        return back(f"/loans/{loan_id}/setup?msg=badrate#rates")
    s.add(LoanRate(loan_id=loan.id, effective=d, rate=r, note=note.strip()[:80]))
    s.commit()
    return back(f"/loans/{loan_id}/setup?msg=saved#rates")


@app.post("/loans/{loan_id}/rates/{rate_id}/delete")
def loan_delete_rate(loan_id: int, rate_id: int, s: Session = Depends(session)):
    r = s.get(LoanRate, rate_id)
    if r and r.loan_id == loan_id:
        s.delete(r)
        s.commit()
    return back(f"/loans/{loan_id}/setup?msg=deleted#rates")


@app.post("/loans/{loan_id}/values")
def loan_add_value(loan_id: int, when: str = Form(...), value: str = Form(...), note: str = Form(""),
                   next: str = Form(""), s: Session = Depends(session)):
    loan, d, v = s.get(Loan, loan_id), _day(when), _float(value)
    if not loan:
        return back("/loans")
    if not d or not v or v <= 0:
        return back(f"/loans/{loan_id}/setup?msg=badvalue#values")
    s.add(PropertyValue(loan_id=loan.id, date=d, value=v, note=note.strip()[:80]))
    s.commit()
    return back(_safe_next(next) if next.startswith(f"/loans/{loan_id}") else f"/loans/{loan_id}/setup?msg=saved#values")


@app.post("/loans/{loan_id}/values/{value_id}/delete")
def loan_delete_value(loan_id: int, value_id: int, s: Session = Depends(session)):
    v = s.get(PropertyValue, value_id)
    if v and v.loan_id == loan_id:
        s.delete(v)
        s.commit()
    return back(f"/loans/{loan_id}/setup?msg=deleted#values")


@app.post("/loans/{loan_id}/history/clear")
def loan_clear_history(loan_id: int, s: Session = Depends(session)):
    for e in s.exec(select(LoanEntry).where(LoanEntry.loan_id == loan_id, LoanEntry.source == "import")):
        s.delete(e)
    s.commit()
    return back(f"/loans/{loan_id}/setup?msg=cleared")


@app.post("/loans/{loan_id}/bill")
def loan_bill(loan_id: int, bill_id: str = Form(""), s: Session = Depends(session)):
    """Link the loan to a bill you already have (bill_id), stop linking it ("none"), or add one from its repayments."""
    loan = s.get(Loan, loan_id)
    if not loan:
        return back("/loans")
    if bill_id == "none":
        loan.bill_id = None
        s.add(loan)
        s.commit()
        return back(f"/loans/{loan_id}?bill=unlinked#setaside")
    if bill_id.isdigit() and s.get(Bill, int(bill_id)):
        loan.bill_id = int(bill_id)
        s.add(loan)
        s.commit()
        return back(f"/loans/{loan_id}?bill=linked#setaside")
    existing, _ = mortgage.repayment_bill(s, loan)
    paid = mortgage.paid_from(s, loan)
    if existing or not paid:  # never make a second bill; with nothing to match on, set one up by hand
        return back(f"/loans/{loan_id}?bill=none#setaside")
    m = mortgage.summary(s, loan, today())
    cat = get_or_create_category(s, "Loan repayments")
    first_due = m["next"].date if m["next"] else today()
    bill = mortgage.bill_from_repayments(loan, m["payment"], first_due, paid[0], cat.id)
    if not valid_pattern(bill.match_pattern):
        return back(f"/loans/{loan_id}?bill=none#setaside")
    s.add(bill)
    s.commit()
    s.refresh(bill)
    loan.bill_id = bill.id
    s.add(loan)
    s.commit()
    recategorise_all(s)  # match past repayments to the new bill
    return back(f"/loans/{loan_id}?bill=added#setaside")


# the Mortgage page's old addresses, for bookmarks
@app.get("/mortgage")
def mortgage_page(s: Session = Depends(session)):
    loan = s.exec(select(Loan).where(Loan.kind == "home").order_by(Loan.id)).first() or the_loan(s)
    return back(f"/loans/{loan.id}" if loan else "/loans")


@app.get("/mortgage/setup")
def mortgage_setup(s: Session = Depends(session)):
    loan = s.exec(select(Loan).where(Loan.kind == "home").order_by(Loan.id)).first()
    return back(f"/loans/{loan.id}/setup" if loan else "/loans/new?kind=home")


# ---------- Settings ----------
# The settings side nav: groups of (key, label, admin only). "activity" is the log itself at /activity;
# every other key is a tab of /settings.
SETTINGS_GROUPS = [
    ("Household", [("pay", "Household and pay", True), ("bank", "Bank connection", True),
                   ("accounts", "Accounts", True), ("members", "People", True)]),
    ("Spending", [("categories", "Categories", True), ("limits", "Category limits", True), ("rules", "Rules", True)]),
    ("Records", [("activity", "Activity", False), ("log", "Activity log settings", True),
                 ("data", "Backup and export", True)]),
    ("You", [("notify", "Notifications", False), ("you", "Your account", False)]),
]
SETTINGS_TABS = [(k, label) for _, items in SETTINGS_GROUPS for k, label, _ in items if k != "activity"]


def settings_nav(request: Request) -> list:
    """The settings groups this person can see, as (group, [(key, label, href)])."""
    admin = request.state.user.role == "admin"
    return [(g, [(k, label, "/activity" if k == "activity" else f"/settings?tab={k}")
                 for k, label, admin_only in items if admin or not admin_only])
            for g, items in SETTINGS_GROUPS if admin or any(not a for *_, a in items)]
PAYDAY_ADJUST = ("previous_business_day", "next_business_day", "none")


def _settings(tab: str, **params) -> RedirectResponse:
    return back(f"/settings?{urlencode({'tab': tab, **params})}")


def describe_rule(rule: Rule, accounts: dict) -> Markup:
    """A rule's conditions as a phrase: contains “mojo”, money out, $150.00 or more, in Everyday."""
    def amt(v: float) -> str:
        return f'<span class="m">${v:,.2f}</span>'
    parts = []
    if rule.pattern.startswith("re:"):
        parts.append(f"matches <code>{escape(rule.pattern[3:])}</code>")
    elif rule.pattern:
        parts.append(f"contains “{escape(rule.pattern)}”")
    if rule.direction != "any":
        parts.append("money out" if rule.direction == "out" else "money in")
    lo, hi = rule.min_amount, rule.max_amount
    if lo is not None and hi is not None:
        parts.append(f"between {amt(lo)} and {amt(hi)}")
    elif lo is not None:
        parts.append(f"{amt(lo)} or more")
    elif hi is not None:
        parts.append(f"up to {amt(hi)}")
    if rule.account_id:
        acc = accounts.get(rule.account_id)
        parts.append(f"in {escape(acc.name) if acc else 'an account that’s gone'}")
    text = ", ".join(parts)
    return Markup(text[:1].upper() + text[1:]) if text else Markup("No conditions")


templates.env.globals["describe_rule"] = describe_rule


@app.get("/settings")
def settings_page(request: Request, tab: str = "", s: Session = Depends(session)):
    me = request.state.user
    tabs = SETTINGS_TABS if me.role == "admin" else [("notify", "Notifications"), ("you", "Your account")]
    index = tab not in dict(tabs)  # plain /settings: phones show the list of sections, wider screens the first one
    if index:
        tab = tabs[0][0]
    ctx: dict = {"tab": tab, "tabs": tabs, "nav": settings_nav(request), "index": index,
                 "page": "settings", "msg": request.query_params.get("msg", ""),
                 "test": ""}
    if ctx["msg"] in ("tokens", "tested", "badtokens", "testfail"):
        ctx["test"] = get_setting(s, "akahu_test")
    cats = s.exec(select(Category).order_by(Category.name)).all()
    if tab == "pay":
        ctx.update(payday_day=get_setting(s, "payday_day"), payday_adjust=get_setting(s, "payday_adjust"),
                   buffer=get_setting(s, "buffer"), sync_hour=get_setting(s, "sync_hour"),
                   period=period_of(s, today()), pay_frequency=get_cycle(s).frequency,
                   pay_anchor=get_setting(s, "pay_anchor"))
    elif tab == "accounts":
        accounts = s.exec(select(Account).order_by(Account.in_budget.desc(), Account.name)).all()
        ctx.update(accounts=accounts, balances={a.id: account_balance(s, a) for a in accounts},
                   bank={a.id: bank_balance(a) for a in accounts})
    elif tab == "limits":
        c = Ctx(s, today())
        ctx.update(cats=[x for x in cats if x.kind == "expense"], limits=category_limits(c, 0),
                   averages=usual_spend(c, 0))
    elif tab == "rules":
        rules = s.exec(select(Rule)).all()
        cat_names = {c.id: c.name for c in cats}
        by_name = lambda r: (cat_names.get(r.category_id, ""), r.pattern.lower())  # noqa: E731
        ctx.update(cats=cats, cat_names=cat_names, accounts=s.exec(select(Account).order_by(Account.name)).all(),
                   account_map={a.id: a for a in s.exec(select(Account))},
                   your_rules=sorted((r for r in rules if r.source == "user"), key=lambda r: (r.priority, r.id)),
                   learned_rules=sorted((r for r in rules if r.source != "user"), key=by_name))
    elif tab == "bank":
        t = vault.tokens(s)
        ctx.update(token_source=t["source"], token_mask={k: vault.masked(t[k]) for k in ("app", "user")},
                   tokens_via=t.get("via", ""), akahu_test=get_setting(s, "akahu_test"))
    elif tab == "members":
        prof = request.state.profile
        with Session(site_engine) as ss:
            rows = ss.exec(select(Membership, User).where(Membership.profile_id == prof.id,
                                                          Membership.user_id == User.id).order_by(User.username)).all()
            invites = [i for i in ss.exec(select(Invite).where(Invite.profile_id == prof.id, Invite.kind == "join",
                                                                Invite.used_at == None)  # noqa: E711
                                          .order_by(Invite.created_at.desc())) if find_invite_row_live(i)]
            ctx.update(members=[(m, u.username, u.totp_enabled, badge(u)) for m, u in rows], invites=invites,
                       link=request.query_params.get("link", "") if request.query_params.get("link", "").startswith(
                           _link(request, "/invite/")) else "")
    elif tab == "you":
        with Session(site_engine) as ss:
            u = ss.get(User, me.id)
            ctx.update(codes_left=backup_codes_left(u))
    elif tab == "notify":
        from . import notify, webpush
        with Session(site_engine) as ss:
            m = _membership(ss, me.id, request.state.profile.id)
            ctx.update(prefs=notify.prefs_of(m) if m else dict(notify.DEFAULTS),
                       devices=ss.exec(select(PushDevice).where(PushDevice.user_id == me.id)
                                       .order_by(PushDevice.created)).all(),
                       vapid=webpush.public_key(), warn=rate_warning(s))
    elif tab == "categories":
        used = {cid: n for cid, n in s.exec(select(Transaction.category_id, func.count()).group_by(Transaction.category_id))}
        ctx.update(cats=cats, used=used)
    elif tab == "log":
        ctx.update(log_kinds=activity.KINDS, log_off=activity.off_kinds(s), log_keep=activity.keep_days(s),
                   keep_choices=activity.KEEP_CHOICES, log_count=len(s.exec(select(Activity.id)).all()))
    elif tab == "data":
        ctx.update(restore=request.query_params.get("restore", ""), held=None, problem=request.query_params.get("problem", ""))
    return templates.TemplateResponse(request, "settings.html", ctx)


def find_invite_row_live(inv: Invite) -> bool:
    exp = inv.expires_at
    if exp and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return bool(exp and exp > datetime.now(timezone.utc))


@app.post("/settings")
def save_settings(payday_day: int = Form(15), payday_adjust: str = Form(...), pay_frequency: str = Form("monthly"),
                  pay_anchor: str = Form(""), buffer: str = Form("0"), sync_hour: int = Form(6), s: Session = Depends(session)):
    freq = pay_frequency if pay_frequency in FREQUENCIES else "monthly"
    if freq in ("weekly", "fortnightly"):
        try:
            anchor = date.fromisoformat(pay_anchor)
        except ValueError:
            return _settings("pay", msg="anchor")
        set_setting(s, "pay_anchor", anchor.isoformat())
    set_setting(s, "pay_frequency", freq)
    set_setting(s, "payday_day", str(min(max(payday_day, 1), 31)))
    set_setting(s, "payday_adjust", payday_adjust if payday_adjust in PAYDAY_ADJUST else PAYDAY_ADJUST[0])
    set_setting(s, "buffer", str(_num(buffer) or 0))
    hour = min(max(sync_hour, 0), 23)
    set_setting(s, "sync_hour", str(hour))
    schedule_sync(current_profile_id(), hour)
    return _settings("pay", msg="saved")


@app.get("/accounts/{acc_id}")
def account_page(request: Request, acc_id: str, offset: int = 0, all: int = 0, chart: str = "1y",
                 s: Session = Depends(session)):
    """One account's transactions with a running balance, laid out like a banking app, for reconciling.

    Pending transactions sit on their own dates (after that day's settled ones) and are included in the
    running balance. The balance starts from your opening balance in Settings when you've set one;
    otherwise it's worked back from the bank's settled balance at the last sync."""
    acc = s.get(Account, acc_id)
    if not acc:
        return back("/")
    offset = min(offset, 0)
    start, end = period_of(s, today(), offset)
    settled = s.exec(select(Transaction).where(Transaction.account_id == acc_id)).all()
    pending = s.exec(select(PendingTransaction).where(PendingTransaction.account_id == acc_id)).all()

    # oldest first; on the same day, settled rows in the bank's order, then pending ones
    billed = {p.id: s.get(Bill, bid) for (bid, _), p in match_pending(s).items()}
    entries = sorted([{"t": t, "pending": False, "date": t.date, "amount": t.amount, "key": (0, t.seq or 0, t.id)}
                      for t in settled] +
                     [{"t": p, "pending": True, "date": p.date, "amount": p.amount, "key": (1, 0, p.id),
                       "bill": billed.get(p.id)}
                      for p in pending],
                     key=lambda e: (e["date"], e["key"]))

    own = acc.opening_balance is not None and acc.opening_date is not None
    if own:
        # your opening balance is the balance at the start of its date
        base = acc.opening_balance - sum(e["amount"] for e in entries if e["date"] < acc.opening_date)
    else:
        # the bank's settled balance is the balance after every settled transaction
        base = acc.balance_current - sum(t.amount for t in settled)
    run = base
    for e in entries:
        run += e["amount"]
        e["after"] = run
    with_pending = run
    settled_total = with_pending - sum(p.amount for p in pending)  # what the bank's Balance should say

    rows = entries if all else [e for e in entries if start <= e["date"] < end]
    rows = list(reversed(rows))[:MAX_ROWS * 2]
    cats = {c.id: c for c in s.exec(select(Category))}
    chart = chart if chart in history.RANGES else "1y"
    pts = history.points(s, acc, TZ)
    return templates.TemplateResponse(request, "account.html", {
        "acc": acc, "rows": rows, "cats": cats, "own": own,
        "graph": history.chart(pts, datetime.now(TZ), chart), "readings": list(reversed(pts))[:12],
        "reading_count": len(pts), "chart_range": chart, "ranges": history.RANGES,
        "pending_count": len(pending), "pending_total": sum(p.amount for p in pending),
        "with_pending": with_pending, "settled_total": settled_total,
        "gap": acc.balance_current - settled_total if own else 0.0,
        "earliest": entries[0]["date"] if entries else None,
        "shown_in": sum(e["amount"] for e in rows if e["amount"] > 0),
        "shown_out": -sum(e["amount"] for e in rows if e["amount"] < 0),
        "start": start, "end": end, "offset": offset, "all": bool(all),
        "last_sync": get_setting(s, "last_sync"), "page": "home"})


@app.post("/bank/connect")
def bank_connect(request: Request):
    """Start "Connect your bank": off to Akahu to sign in and choose accounts, then back to /bank/callback."""
    if not akahu.connect_enabled():
        return _settings("bank", msg="noconnect")
    nonce = secrets.token_urlsafe(18)
    body = f"{request.state.profile.id}.{request.state.user.id}.{nonce}.{int(time.time())}"
    state = f"{body}.{auth_sign(body, 'oauth')}"
    resp = back(akahu.authorise_url(_link(request, "/bank/callback"), state, request.state.user.email or ""))
    resp.set_cookie("payday_oauth", nonce, max_age=900, **_cookie_opts(request))
    return resp


@app.get("/bank/callback")
def bank_callback(request: Request, code: str = "", state: str = "", error: str = "", s: Session = Depends(session)):
    """Akahu sends the person back here after they approve (or cancel)."""
    if error:
        return _settings("bank", msg="cancelled")
    parts = state.split(".")
    ok = (len(parts) == 5 and hmac.compare_digest(parts[4], auth_sign(".".join(parts[:4]), "oauth"))
          and parts[0] == str(request.state.profile.id) and parts[1] == str(request.state.user.id)
          and hmac.compare_digest(parts[2], request.cookies.get("payday_oauth", "-"))
          and parts[3].isdigit() and time.time() - int(parts[3]) < 900)
    if not ok or not code or request.state.user.role != "admin":
        return _settings("bank", msg="badstate")
    try:
        token = akahu.exchange_code(code, _link(request, "/bank/callback"))
    except (akahu.SyncProblem, httpx.HTTPError) as e:
        set_setting(s, "akahu_test", str(e)[:300])
        return _settings("bank", msg="testfail")
    vault.save_connect_token(s, token)
    set_setting(s, "akahu_test", "Connected with Akahu.")
    akahu.start_in_background("first sync")
    resp = _settings("bank", msg="connected")
    resp.delete_cookie("payday_oauth", path="/")
    return resp


@app.post("/bank/disconnect")
def bank_disconnect(s: Session = Depends(session)):
    akahu.disconnect(s)
    return _settings("bank", msg="forgot")


@app.post("/settings/akahu")
def save_akahu(request: Request, app_token: str = Form(""), user_token: str = Form(""), s: Session = Depends(session)):
    if not request.state.user.is_owner:  # pasted personal-app tokens are only for the site owner's own household
        return _settings("bank", msg="owneronly")
    if not app_token.strip() and not user_token.strip():
        return _settings("bank", msg="notokens")
    current = vault.tokens(s)
    ok, message = akahu.test_connection(app_token.strip() or current["app"], user_token.strip() or current["user"])
    if not ok:  # don't save tokens that don't work
        set_setting(s, "akahu_test", message)
        return _settings("bank", msg="badtokens")
    vault.save_tokens(s, app_token, user_token)
    set_setting(s, "akahu_test", message)
    return _settings("bank", msg="tokens")


@app.post("/settings/akahu/test")
def test_akahu(s: Session = Depends(session)):
    t = vault.tokens(s)
    ok, message = akahu.test_connection(t["app"], t["user"])
    set_setting(s, "akahu_test", message)
    return _settings("bank", msg="tested" if ok else "testfail")


@app.post("/settings/akahu/forget")
def forget_akahu(s: Session = Depends(session)):
    akahu.disconnect(s)
    return _settings("bank", msg="forgot")


@app.post("/accounts/{acc_id}")
def save_account(acc_id: str, in_budget: bool = Form(False), count_spending: bool = Form(False),
                 opening_balance: str = Form(""), opening_date: str = Form(""), nickname: str = Form(""),
                 s: Session = Depends(session)):
    acc = s.get(Account, acc_id)
    if not acc:
        return _settings("accounts")
    was = acc.count_spending
    acc.in_budget, acc.count_spending = in_budget, count_spending
    acc.nickname = " ".join(nickname.split())[:60]
    acc.name = acc.nickname or acc.bank_name or acc.name
    if opening_balance.strip() or opening_date:
        try:  # a balance can be negative (a credit card), so don't strip the sign
            acc.opening_balance = float(opening_balance.replace(",", "").replace("$", "").replace("−", "-"))
            acc.opening_date = date.fromisoformat(opening_date)
        except ValueError:
            return _settings("accounts", msg="opening")
    else:  # both blank = go back to the bank's balance
        acc.opening_balance = acc.opening_date = None
    s.add(acc)
    s.commit()
    if was != count_spending:
        recategorise_all(s)  # bills paid to or from this account may change
    return _settings("accounts", msg="saved")


def _num(v: str) -> float | None:
    """A positive dollar amount from a text box, or None if it's blank or not a number."""
    try:
        return abs(float(v.replace("$", "").replace(",", ""))) if v.strip() else None
    except ValueError:
        return None


def _rule_from_form(pattern: str, direction: str, min_amount: str, max_amount: str, account_id: str,
                    category_id: int) -> Rule:
    lo, hi = _num(min_amount), _num(max_amount)
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return Rule(pattern=pattern.strip(), direction=direction if direction in ("any", "in", "out") else "any",
                min_amount=lo, max_amount=hi, account_id=account_id or None,
                category_id=category_id, source="user")


def _usable(r: Rule) -> bool:
    """Has at least one condition, and any regular expression in it compiles."""
    has = bool(r.pattern or r.direction != "any" or r.min_amount is not None
               or r.max_amount is not None or r.account_id)
    return has and (not r.pattern or valid_pattern(r.pattern))


@app.get("/rules/preview")
def preview_rule(request: Request, pattern: str = "", direction: str = "any", min_amount: str = "",
                 max_amount: str = "", account_id: str = "", category_id: str = "", new_category: str = "",
                 kind: str = "expense", rule_id: str = "", s: Session = Depends(session)):
    cats = {c.id: c for c in s.exec(select(Category))}
    cid = int(category_id) if category_id.isdigit() and not new_category.strip() else None
    rule = _rule_from_form(pattern, direction, min_amount, max_amount, account_id, cid or 0)
    usable = _usable(rule)
    matches = [t for t in s.exec(select(Transaction).order_by(Transaction.date.desc()))
               if rule_matches(rule, t)] if usable else []
    locked = ("manual", "bill", "transfer")
    return templates.TemplateResponse(request, "rule_preview.html", {
        "rule": rule, "usable": usable, "matches": matches[:300], "total": len(matches), "cats": cats,
        "target": new_category.strip() or (cats[cid].name if cid in cats else ""),
        "changing": sum(1 for t in matches if t.category_id != cid and t.category_source not in locked),
        "locked": sum(1 for t in matches if t.category_source in locked),
        "accounts": {a.id: a for a in s.exec(select(Account))}, "form": dict(request.query_params),
        "rule_id": rule_id if rule_id.isdigit() else "", "page": "settings"})


def _rule_words(s: Session, rule: Rule) -> str:
    cat = s.get(Category, rule.category_id)
    what = rule.pattern.removeprefix("re:") or "anything"
    return f"{what} goes in {cat.name if cat else 'a category'}"


@app.post("/rules")
def add_rule(pattern: str = Form(""), direction: str = Form("any"), min_amount: str = Form(""),
             max_amount: str = Form(""), account_id: str = Form(""), category_id: str = Form(""),
             new_category: str = Form(""), kind: str = Form("expense"), s: Session = Depends(session)):
    rule = _rule_from_form(pattern, direction, min_amount, max_amount, account_id, 0)
    known = category_id.isdigit() and s.get(Category, int(category_id))
    if not _usable(rule) or not (new_category.strip() or known):
        return _settings("rules", msg="badrule")
    kind = kind if kind in ("expense", "income", "transfer") else "expense"
    rule.category_id = (get_or_create_category(s, new_category.strip()[:60], kind).id if new_category.strip()
                        else int(category_id))
    s.add(rule)
    s.commit()
    recategorise_all(s, f"Rule added: {_rule_words(s, rule)}.")
    return _settings("rules", msg="saved")


@app.post("/rules/{rule_id}")
def edit_rule(rule_id: int, pattern: str = Form(""), direction: str = Form("any"), min_amount: str = Form(""),
              max_amount: str = Form(""), account_id: str = Form(""), category_id: int = Form(...),
              s: Session = Depends(session)):
    rule = s.get(Rule, rule_id)
    new = _rule_from_form(pattern, direction, min_amount, max_amount, account_id, category_id)
    if not rule or not _usable(new) or not s.get(Category, category_id):
        return _settings("rules", msg="badrule")
    for f in ("pattern", "direction", "min_amount", "max_amount", "account_id", "category_id"):
        setattr(rule, f, getattr(new, f))
    s.add(rule)
    s.commit()
    recategorise_all(s, f"Rule changed: {_rule_words(s, rule)}.")
    return _settings("rules", msg="saved")


@app.post("/rules/{rule_id}/delete")
def delete_rule(rule_id: int, s: Session = Depends(session)):
    rule = s.get(Rule, rule_id)
    if rule:
        words = _rule_words(s, rule)
        s.delete(rule)
        s.commit()
        recategorise_all(s, f"Rule deleted: {words}.")
    return _settings("rules", msg="deleted")


@app.post("/categories/{cat_id}/limit")
def set_limit(cat_id: int, limit_mode: str = Form("auto"), limit_amount: str = Form(""),
              s: Session = Depends(session)):
    cat = s.get(Category, cat_id)
    if cat:
        cat.limit_mode = limit_mode if limit_mode in ("auto", "fixed", "off") else "auto"
        cat.limit_amount = _num(limit_amount)
        if cat.limit_mode == "fixed" and not cat.limit_amount:
            cat.limit_mode = "auto"
        s.add(cat)
        s.commit()
    return _settings("limits", msg="saved")


# ---------- Categories ----------
@app.post("/categories/{cat_id}/rename")
def rename_category(cat_id: int, name: str = Form(...), merge: bool = Form(False), s: Session = Depends(session)):
    """Rename a category. Transactions, rules, bills and limits point at it by number, so they follow.
    If another category already has the new name, offer (then do) a merge into that one."""
    cat = s.get(Category, cat_id)
    name = " ".join(name.split())[:60]
    if not cat or not name:
        return _settings("categories", msg="badname")
    other = s.exec(select(Category).where(func.lower(Category.name) == name.lower(), Category.id != cat.id)).first()
    if other:
        if not merge:
            return _settings("categories", msg="exists", cat=cat.id, into=other.id)
        if cat.name in ("Income", "Transfer", "Uncategorised"):
            return _settings("categories", msg="builtin")
        for t in s.exec(select(Transaction).where(Transaction.category_id == cat.id)):
            t.category_id = other.id
            s.add(t)
        for r in s.exec(select(Rule).where(Rule.category_id == cat.id)):
            r.category_id = other.id
            s.add(r)
        for b in s.exec(select(Bill).where(Bill.category_id == cat.id)):
            b.category_id = other.id
            s.add(b)
        s.delete(cat)
        s.commit()
        return _settings("categories", msg="merged")
    if cat.name in ("Income", "Transfer", "Uncategorised", "Bills"):
        return _settings("categories", msg="builtin")
    cat.name = name
    s.add(cat)
    s.commit()
    return _settings("categories", msg="renamed")


@app.post("/categories/{cat_id}/delete")
def delete_category(cat_id: int, s: Session = Depends(session)):
    """Only an unused category can go (merge it into another to move its transactions)."""
    cat = s.get(Category, cat_id)
    if cat and cat.name not in ("Income", "Transfer", "Uncategorised", "Bills") \
            and not s.exec(select(Transaction).where(Transaction.category_id == cat.id)).first() \
            and not s.exec(select(Rule).where(Rule.category_id == cat.id)).first() \
            and not s.exec(select(Bill).where(Bill.category_id == cat.id)).first():
        s.delete(cat)
        s.commit()
        return _settings("categories", msg="deleted")
    return _settings("categories", msg="inuse")


@app.post("/categories")
def add_category(name: str = Form(...), kind: str = Form("expense"), s: Session = Depends(session)):
    name = " ".join(name.split())[:60]
    if not name or s.exec(select(Category).where(func.lower(Category.name) == name.lower())).first():
        return _settings("categories", msg="exists" if name else "badname")
    s.add(Category(name=name, kind=kind if kind in ("expense", "income", "transfer") else "expense"))
    s.commit()
    return _settings("categories", msg="saved")


# ---------- Backup, restore and export (this profile) ----------
def _download(body: bytes, name: str) -> Response:
    return Response(body, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/data/backup")
def download_backup(request: Request, passphrase: str = Form(""), s: Session = Depends(session)):
    prof = request.state.profile
    if passphrase and len(passphrase) < 8:
        return _settings("data", problem="Use a passphrase of at least 8 characters, or leave it empty.")
    return _download(data.backup_zip(s, prof.name, passphrase), data.filename("backup", prof.name, bool(passphrase)))


@app.post("/data/export")
def download_export(request: Request, s: Session = Depends(session)):
    prof = request.state.profile
    return _download(data.export_zip(s), data.filename("csv", prof.name))


@app.post("/data/restore/check")
async def restore_check(request: Request, file: UploadFile, passphrase: str = Form("")):
    raw = await file.read(data.MAX_UPLOAD + 1)
    try:
        body = data.read_backup(raw, passphrase)
    except data.BadBackup as e:
        return _settings("data", problem=str(e))
    token = data.hold(body, request.state.profile.id, request.state.user.id)
    return templates.TemplateResponse(request, "restore.html", {
        "token": token, "counts": data.counts(body), "from_profile": str(body.get("profile", ""))[:60],
        "created": str(body.get("created", ""))[:19].replace("T", " "), "page": "settings"})


@app.post("/data/restore")
def restore_apply(request: Request, token: str = Form(...), confirm: str = Form("")):
    prof = request.state.profile
    if confirm.strip().lower() != prof.name.strip().lower():
        return _settings("data", problem="Type the household’s name to confirm. Nothing was changed; upload the file again.")
    if akahu.is_running(prof.id):
        return _settings("data", problem="A bank sync is running. Wait for it to finish, then upload the file again.")
    body = data.take(token, prof.id, request.state.user.id)
    if not body:
        return _settings("data", problem="That upload has expired. Choose the file again.")
    data.restore(prof.id, body)
    start_profile(prof.id)
    return _settings("data", msg="restored")


# ---------- Site owner: profiles and sign-ins ----------
@app.get("/site")
def site_page(request: Request, s: Session = Depends(site_session)):
    profiles = s.exec(select(Profile).order_by(Profile.name)).all()
    members = s.exec(select(Membership, User).where(Membership.user_id == User.id)).all()
    by_profile: dict[int, list] = {}
    for m, u in members:
        by_profile.setdefault(m.profile_id, []).append((u.username, m.role))
    users = s.exec(select(User).order_by(User.username)).all()
    user_profiles: dict[int, list] = {}
    names = {p.id: p.name for p in profiles}
    for m, u in members:
        user_profiles.setdefault(u.id, []).append(names.get(m.profile_id, "?"))
    return templates.TemplateResponse(request, "site.html", {
        "profiles": profiles, "by_profile": by_profile, "users": users, "user_profiles": user_profiles,
        "mine": {pid for pid, _, _ in request.state.profiles},
        "msg": request.query_params.get("msg", ""),
        "link": request.query_params.get("link", "") if request.query_params.get("link", "").startswith(_link(request, "/")) else "",
        "page": "site"})


def _site(msg: str, **params) -> RedirectResponse:
    return back(f"/site?{urlencode({'msg': msg, **params})}")


@app.post("/site/profiles")
def site_add_profile(request: Request, name: str = Form(...), join: bool = Form(False), role: str = Form("admin"),
                     s: Session = Depends(site_session)):
    if not " ".join(name.split()):
        return _site("badname")
    me = s.get(User, request.state.user.id)
    prof = create_profile(s, name, me if join else None)
    start_profile(prof.id)
    if join:
        return _site("created")
    token = make_invite(s, "join", me.id, profile_id=prof.id, role="admin", note=f"First admin of {prof.name}")
    return _site("created", link=_link(request, f"/invite/{token}"))


@app.post("/site/profiles/{profile_id}/invite")
def site_invite(request: Request, profile_id: int, s: Session = Depends(site_session)):
    if not s.get(Profile, profile_id):
        return _site("")
    token = make_invite(s, "join", request.state.user.id, profile_id=profile_id, role="admin", note="Admin")
    return _site("invited", link=_link(request, f"/invite/{token}"))


@app.post("/site/profiles/{profile_id}/delete")
def site_delete_profile(request: Request, profile_id: int, confirm: str = Form(""), s: Session = Depends(site_session)):
    prof = s.get(Profile, profile_id)
    if not prof or confirm.strip().lower() != prof.name.strip().lower():
        return _site("badconfirm")
    if akahu.is_running(profile_id):
        return _site("syncing")
    unschedule_sync(profile_id)
    for m in s.exec(select(Membership).where(Membership.profile_id == profile_id)):
        s.delete(m)
    for i in s.exec(select(Invite).where(Invite.profile_id == profile_id)):
        s.delete(i)
    s.delete(prof)
    s.commit()
    data.drop_profile_data(prof)
    return _site("deleted")


@app.post("/site/users/{user_id}/reset")
def site_reset_password(request: Request, user_id: int, s: Session = Depends(site_session)):
    user = s.get(User, user_id)
    if not user:
        return _site("")
    token = make_invite(s, "reset", request.state.user.id, user_id=user.id, note=f"Password for {user.username}", days=1)
    return _site("resetlink", link=_link(request, f"/reset/{token}"), who=user.username)


@app.post("/site/users/{user_id}/2fa")
def site_reset_2fa(request: Request, user_id: int, s: Session = Depends(site_session)):
    user = s.get(User, user_id)
    if user and user.id != request.state.user.id:
        user.totp_enabled, user.totp_secret, user.backup_codes = False, "", ""
        user.session_version += 1
        s.add(user)
        s.commit()
        return _site("2fa", who=user.username)
    return _site("")


@app.post("/site/users/{user_id}/delete")
def site_delete_user(request: Request, user_id: int, s: Session = Depends(site_session)):
    user = s.get(User, user_id)
    if not user or user.id == request.state.user.id:
        return _site("")
    for m in s.exec(select(Membership).where(Membership.user_id == user.id)):
        if m.role == "admin" and profile_admins(s, m.profile_id) <= 1:
            return _site("lastadmin", who=user.username)
    for m in s.exec(select(Membership).where(Membership.user_id == user.id)):
        s.delete(m)
    for i in s.exec(select(Invite).where(Invite.user_id == user.id)):
        s.delete(i)
    for row in [*s.exec(select(PushDevice).where(PushDevice.user_id == user.id)),
                *s.exec(select(NotifySent).where(NotifySent.user_id == user.id))]:
        s.delete(row)
    s.delete(user)
    s.commit()
    return _site("userdeleted")


@app.post("/site/users/{user_id}/owner")
def site_owner(request: Request, user_id: int, s: Session = Depends(site_session)):
    user = s.get(User, user_id)
    if user and user.id != request.state.user.id:
        user.is_owner = not user.is_owner
        s.add(user)
        s.commit()
    return _site("saved")


@app.post("/site/backup")
def site_backup(request: Request, password: str = Form(...), code: str = Form(...), s: Session = Depends(site_session)):
    """The whole site, key file included, so it needs your password and a fresh 2FA code."""
    user = s.get(User, request.state.user.id)
    if _blocked(request, user.username):
        return _site("badcode")
    step = check_totp(vault.unseal("totp", user.totp_secret), code, user.totp_last_step)
    if not verify_password(password, user.password_hash) or not step:
        _failed(request, user.username)
        return _site("badcode")
    user.totp_last_step = step
    s.add(user)
    s.commit()
    body = data.site_backup_zip(s.exec(select(Profile)).all(), vault.KEY_FILE)
    return _download(body, data.filename("site", "whole"))


# ---------- Site details, policies, contact, help ----------
SITE_FIELDS = [
    ("business_name", "Business or trading name", "Payday"),
    ("legal_name", "Legal entity name (if different)", ""),
    ("nzbn", "NZBN", ""),
    ("address", "Postal address", ""),
    ("contact_email", "Contact email", ""),
    ("privacy_email", "Privacy officer email", ""),
    ("privacy_officer", "Privacy officer name", ""),
    ("security_email", "Email for reporting security problems", ""),
    ("phone", "Phone (optional)", ""),
    ("data_location", "Where the data is stored", "New Zealand"),
    ("hosting_provider", "Hosting provider", ""),
    ("email_provider", "Email provider", ""),
    ("policy_date", "Policies last updated (YYYY-MM-DD)", ""),
    ("backup_days", "Days until deleted data is gone from your server backups", "30"),
]


def site_info() -> dict:
    info = {k: site_setting(k, default) for k, _, default in SITE_FIELDS}
    info["legal_name"] = info["legal_name"] or info["business_name"]
    info["privacy_email"] = info["privacy_email"] or info["contact_email"]
    info["security_email"] = info["security_email"] or info["contact_email"]
    info["signup_mode"] = signup_mode()
    info["connect"] = akahu.connect_enabled()
    info["email_on"] = mailer.configured()
    return info


templates.env.globals["site_info"] = site_info
templates.env.globals["site_setting_raw"] = lambda key: site_setting(key, "")


@app.get("/site/details")
def site_details_page(request: Request):
    return templates.TemplateResponse(request, "site_details.html", {
        "fields": SITE_FIELDS, "info": site_info(), "msg": request.query_params.get("msg", ""), "page": "site"})


@app.post("/site/details")
async def site_details_save(request: Request, s: Session = Depends(site_session)):
    form = await request.form()
    for key, _, _ in SITE_FIELDS:
        value = " ".join(str(form.get(key, "")).split())[:200]
        if key.endswith("email") and value and not EMAIL_RE.match(value):
            return back("/site/details?msg=bademail")
        if key == "policy_date" and value:
            try:
                date.fromisoformat(value)
            except ValueError:
                return back("/site/details?msg=baddate")
        set_site_setting(s, key, value)
    mode = str(form.get("signup_mode", ""))
    if mode in ("open", "approval", "invite"):
        set_site_setting(s, "signup_mode", mode)
    return back("/site/details?msg=saved")


@app.get("/legal/{page}")
def legal(request: Request, page: str):
    if page not in ("privacy", "terms", "security"):
        return Response("Not found", status_code=404, media_type="text/plain")
    return templates.TemplateResponse(request, f"legal_{page}.html", {"page": "legal", "info": site_info()})


@app.get("/contact")
def contact(request: Request):
    return templates.TemplateResponse(request, "contact.html", {"page": "legal", "info": site_info()})


@app.get("/updates")
def updates_page(request: Request):
    """What's changed in each version, from CHANGELOG.md. Opened from the version number in the footer."""
    from .changelog import releases
    return templates.TemplateResponse(request, "updates.html", {"page": "updates", "info": site_info(),
                                                                "releases": releases()})


@app.get("/help")
@app.get("/help/{topic}")
def help_page(request: Request, topic: str = "start"):
    if topic not in ("bank", "start"):
        return Response("Not found", status_code=404, media_type="text/plain")
    return templates.TemplateResponse(request, f"help_{topic}.html", {"page": "help", "info": site_info(),
                                                                      "callback": _link(request, "/bank/callback")})
