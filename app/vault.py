"""Secrets: the app's own key, and the Akahu tokens.

The key lives in a file next to the database (payday.key, readable only by the app's user). It signs sign-in
cookies and encrypts the Akahu tokens you save in Settings, which are stored encrypted in the database.
So a copy of the database on its own reveals no tokens and can't be used to forge a sign-in.

Where the sync looks for the Akahu tokens, first match wins:
  1. Saved in the app (Settings > Bank connection), encrypted.
  2. AKAHU_APP_TOKEN_FILE / AKAHU_USER_TOKEN_FILE: paths to files, e.g. Docker secrets.
  3. AKAHU_APP_TOKEN / AKAHU_USER_TOKEN environment variables (the original stack.env setup).
Options 2 and 3 only apply to the first profile (the one made from the original single database).
Each profile's saved tokens are in its own database.
"""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlmodel import Session

from .db import current_engine, data_dir, get_setting, set_setting

NAMES = {"app": "AKAHU_APP_TOKEN", "user": "AKAHU_USER_TOKEN"}


KEY_FILE = Path(os.getenv("PAYDAY_KEY_FILE") or data_dir() / "payday.key")
_key_cache: bytes | None = None


def _key() -> bytes:
    """The app's 32-byte key, created on first use with owner-only permissions."""
    global _key_cache
    if _key_cache:
        return _key_cache
    if os.getenv("PAYDAY_KEY"):  # several copies of the app (cloud) share one key from a secret
        _key_cache = os.environ["PAYDAY_KEY"].strip().encode()
        return _key_cache
    try:
        raw = KEY_FILE.read_bytes().strip()
    except FileNotFoundError:
        raw = base64.urlsafe_b64encode(os.urandom(32))
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw + b"\n")
    _key_cache = raw
    return raw


def _derive(purpose: str) -> bytes:
    return hmac.new(_key(), purpose.encode(), hashlib.sha256).digest()


def session_secret() -> bytes:
    return _derive("session-cookie")


def _fernet(purpose: str = "akahu-tokens") -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_derive(purpose)))


def seal(purpose: str, value: str) -> str:
    """Encrypt a small secret (e.g. a 2FA key) for storing in a database."""
    return _fernet(purpose).encrypt(value.encode()).decode() if value else ""


def unseal(purpose: str, blob: str) -> str:
    try:
        return _fernet(purpose).decrypt(blob.encode()).decode() if blob else ""
    except InvalidToken:
        return ""


def _saved(s: Session) -> dict:
    blob = get_setting(s, "akahu_tokens")
    if not blob:
        return {}
    try:
        return json.loads(_fernet().decrypt(blob.encode()))
    except (InvalidToken, ValueError):
        return {}  # the key file was replaced: the saved tokens can't be read, fall back to the others


def _from_file(name: str) -> str:
    path = os.getenv(f"{name}_FILE", "")
    try:
        return Path(path).read_text().strip() if path else ""
    except OSError:
        return ""


def tokens(s: Session | None = None) -> dict:
    """{"app": ..., "user": ..., "source": {"app": where, "user": where}}. Empty strings if not set."""
    if s is None:
        with Session(current_engine()) as fresh:
            return tokens(fresh)
    saved = _saved(s)
    if saved.get("via") == "connect":  # connected through Akahu sign-in: the app is this site's Akahu app
        return {"app": os.getenv("AKAHU_CLIENT_ID", "").strip(), "user": saved.get("user", ""),
                "via": "connect", "source": {"app": "site", "user": "connect"}}
    legacy = get_setting(s, "legacy_env_tokens") == "1"  # only the profile made from the original database
    out, source = {"via": "personal"}, {}
    for k, env in NAMES.items():
        options = [("app", saved.get(k, ""))]
        if legacy:
            options += [("file", _from_file(env)), ("env", os.getenv(env, "").strip())]
        for where, value in options:
            if value:
                out[k], source[k] = value, where
                break
        else:
            out[k], source[k] = "", ""
    out["source"] = source
    return out


def save_tokens(s: Session, app_token: str, user_token: str) -> None:
    """Save either or both; a blank one keeps whatever is saved now."""
    current = _saved(s)
    for k, v in (("app", app_token.strip()), ("user", user_token.strip())):
        if v:
            current[k] = v
    set_setting(s, "akahu_tokens", _fernet().encrypt(json.dumps(current).encode()).decode())


def save_connect_token(s: Session, user_token: str) -> None:
    """A token from "Connect your bank" (Akahu sign-in), replacing any pasted tokens."""
    set_setting(s, "akahu_tokens", _fernet().encrypt(json.dumps({"user": user_token, "via": "connect"}).encode()).decode())


def forget_tokens(s: Session) -> None:
    set_setting(s, "akahu_tokens", "")


def masked(value: str) -> str:
    """The last four characters, so you can tell which token it is without it being shown."""
    return f"…{value[-4:]}" if len(value) > 12 else ""
