"""People, passwords, two-step sign-in codes, profiles and session cookies (site database)."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, select

from . import vault
from .db import Invite, Membership, Profile, User, engine_for, init_profile_db, new_profile_file, set_setting, site_engine

COOKIE = "payday_session"
PRE_COOKIE = "payday_signin"  # between the password and the 2FA code
PROFILE_COOKIE = "payday_profile"
SESSION_DAYS = 30  # longest a sign-in lasts
IDLE_HOURS = 24 * 7  # signed out after a week without using Payday
current_user: ContextVar[Optional[User]] = ContextVar("current_user", default=None)
current_profile: ContextVar[Optional[Profile]] = ContextVar("current_profile_row", default=None)

MIN_PASSWORD = 10
MAX_PASSWORD = 256  # longer is no safer, and very long ones only make scrypt work harder
COMMON = {"password", "password1", "password123", "1234567890", "12345678910", "qwertyuiop", "letmein123",
          "iloveyou12", "changeme12", "change-me-123", "welcome123", "payday1234", "administrator", "qwerty1234",
          "aotearoa123", "newzealand", "1q2w3e4r5t", "abc1234567", "monkey1234", "dragon1234", "football12"}


def password_problem(password: str, username: str = "") -> str:
    """Why a new password isn't good enough, or "" if it is."""
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters. A few unrelated words is easiest to remember."
    if len(password) > MAX_PASSWORD:
        return f"Use at most {MAX_PASSWORD} characters."
    if password.lower() in COMMON or (username and username.lower() in password.lower()):
        return "That password is too easy to guess. Try a few unrelated words."
    if len(set(password)) < 4:
        return "That password is too easy to guess. Try a few unrelated words."
    return ""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


_DUMMY = hash_password(secrets.token_hex(8))


def verify_password(password: str, stored: str | None) -> bool:
    """Always does the same work, so a wrong username takes as long as a wrong password."""
    try:
        _, salt, digest = (stored or _DUMMY).split("$")
        test = hashlib.scrypt(password.encode()[:1024], salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return stored is not None and hmac.compare_digest(test.hex(), digest)
    except ValueError:
        return False


# ---------- cookies ----------
def _sign(body: str, purpose: str = "session") -> str:
    key = vault.session_secret() if purpose == "session" else vault._derive(f"cookie-{purpose}")
    return hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


def make_cookie(user: User) -> str:
    """user . version . issued . last seen . signature"""
    now = int(time.time())
    body = f"{user.id}.{user.session_version}.{now}.{now}"
    return f"{body}.{_sign(body)}"


def refresh_cookie(value: str) -> Optional[str]:
    """A new cookie with "last seen" moved to now, at most once an hour."""
    uid, ver, issued, seen, _ = value.split(".")
    now = int(time.time())
    if now - int(seen) < 3600:
        return None
    body = f"{uid}.{ver}.{issued}.{now}"
    return f"{body}.{_sign(body)}"


def user_from_cookie(session: Session, value: str | None) -> Optional[User]:
    if not value or value.count(".") != 4:
        return None
    uid, ver, issued, seen, sig = value.split(".")
    body = f"{uid}.{ver}.{issued}.{seen}"
    if not hmac.compare_digest(sig, _sign(body)) or not (uid.isdigit() and issued.isdigit() and seen.isdigit()):
        return None
    now = time.time()
    if now - int(issued) > SESSION_DAYS * 86400 or now - int(seen) > IDLE_HOURS * 3600:
        return None
    user = session.get(User, int(uid))
    return user if user and str(user.session_version) == ver and user.totp_enabled else None


def make_pre_cookie(user: User) -> str:
    """Proves the password was right, for five minutes, so the 2FA step can follow."""
    body = f"{user.id}.{user.session_version}.{int(time.time())}"
    return f"{body}.{_sign(body, 'pre')}"


def user_from_pre_cookie(session: Session, value: str | None) -> Optional[User]:
    if not value or value.count(".") != 3:
        return None
    uid, ver, when, sig = value.split(".")
    body = f"{uid}.{ver}.{when}"
    if not hmac.compare_digest(sig, _sign(body, "pre")) or not uid.isdigit() or not when.isdigit():
        return None
    if time.time() - int(when) > 300:
        return None
    user = session.get(User, int(uid))
    return user if user and str(user.session_version) == ver else None


# ---------- two-step codes (TOTP, RFC 6238) ----------
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _totp(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_now(secret: str, at: float | None = None) -> str:
    return _totp(secret, int((at or time.time()) // 30))


def check_totp(secret: str, code: str, last_step: int) -> int:
    """The time step the code belongs to (one either side allowed for clock drift), or 0. Used steps are refused."""
    code = "".join(c for c in code if c.isdigit())
    if len(code) != 6 or not secret:
        return 0
    now = int(time.time() // 30)
    for step in (now - 1, now, now + 1):
        if step > last_step and hmac.compare_digest(_totp(secret, step), code):
            return step
    return 0


def otpauth_uri(user: User, secret: str, issuer: str = "Payday") -> str:
    from urllib.parse import quote
    return f"otpauth://totp/{quote(issuer)}:{quote(user.username)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"


def _code_hash(code: str) -> str:
    """Keyed with the site key, so a stolen copy of the database alone can't be used to work the codes out."""
    return hmac.new(vault._derive("backup-codes"), code.encode(), hashlib.sha256).hexdigest()


def new_backup_codes() -> tuple[list[str], str]:
    codes = [f"{secrets.randbelow(10**8):08d}" for _ in range(10)]
    stored = json.dumps([_code_hash(c) for c in codes])
    return [f"{c[:4]}-{c[4:]}" for c in codes], stored


def use_backup_code(user: User, code: str) -> bool:
    code = "".join(c for c in code if c.isdigit())
    if len(code) != 8:
        return False
    digest = _code_hash(code)
    try:
        left = json.loads(user.backup_codes or "[]")
    except ValueError:
        left = []
    if digest not in left:
        return False
    left.remove(digest)
    user.backup_codes = json.dumps(left)
    return True


def backup_codes_left(user: User) -> int:
    try:
        return len(json.loads(user.backup_codes or "[]"))
    except ValueError:
        return 0


# ---------- people and profiles ----------
def any_users(session: Session) -> bool:
    return session.exec(select(User)).first() is not None


def create_user(session: Session, username: str, password: str, owner: bool = False) -> User:
    user = User(username=username.strip().lower(), password_hash=hash_password(password), is_owner=owner,
                created_at=datetime.now(timezone.utc))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def memberships(session: Session, user_id: int) -> list[tuple[Membership, Profile]]:
    rows = session.exec(select(Membership, Profile).where(Membership.user_id == user_id,
                                                          Membership.profile_id == Profile.id)
                        .order_by(Profile.name)).all()
    return list(rows)


def profile_admins(session: Session, profile_id: int) -> int:
    return len(session.exec(select(Membership).where(Membership.profile_id == profile_id,
                                                     Membership.role == "admin")).all())


def create_profile(session: Session, name: str, admin: User | None = None) -> Profile:
    first = session.exec(select(Profile)).first() is None
    prof = Profile(name=" ".join(name.split())[:60] or "Payday", db_file=new_profile_file(),
                   created_at=datetime.now(timezone.utc))
    session.add(prof)
    session.commit()
    session.refresh(prof)
    init_profile_db(engine_for(prof.id))
    if first:
        with Session(engine_for(prof.id)) as ps:
            set_setting(ps, "legacy_env_tokens", "1")  # tokens in stack.env, if any, belong to the first profile
    if admin:
        session.add(Membership(user_id=admin.id, profile_id=prof.id, role="admin"))
        session.commit()
    return prof


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def make_invite(session: Session, kind: str, created_by: int, profile_id: int | None = None,
                user_id: int | None = None, role: str = "viewer", note: str = "", days: int = 7) -> str:
    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    session.add(Invite(token_hash=token_hash(token), kind=kind, profile_id=profile_id, user_id=user_id,
                       role=role if role in ("admin", "viewer") else "viewer", note=note.strip()[:60],
                       created_by=created_by, created_at=now, expires_at=now + timedelta(days=days)))
    session.commit()
    return token


def find_invite(session: Session, token: str) -> Optional[Invite]:
    if not token or len(token) > 100:
        return None
    inv = session.exec(select(Invite).where(Invite.token_hash == token_hash(token))).first()
    if not inv or inv.used_at:
        return None
    exp = inv.expires_at.replace(tzinfo=timezone.utc) if inv.expires_at and inv.expires_at.tzinfo is None else inv.expires_at
    if not exp or exp < datetime.now(timezone.utc):
        return None
    return inv


def bootstrap_admin() -> None:
    """Turn APP_USER / APP_PASSWORD into the site owner on a brand-new install (nobody can sign in yet).
    They'll be asked to set up a 2FA code on first sign-in."""
    user, pw = os.getenv("APP_USER", "").strip(), os.getenv("APP_PASSWORD", "")
    vault.session_secret()  # create the key file on first start
    with Session(site_engine) as s:
        if not any_users(s) and user and pw:
            owner = create_user(s, user, pw, owner=True)
            if not s.exec(select(Profile)).first():
                create_profile(s, os.getenv("PROFILE_NAME", "Home"), owner)
            else:
                for prof in s.exec(select(Profile)):
                    s.add(Membership(user_id=owner.id, profile_id=prof.id, role="admin"))
                s.commit()
