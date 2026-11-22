"""Backups, restores and CSV exports.

A household backup is a zip holding backup.json: every row of every household table except secrets (bank tokens).
It can be locked with a passphrase. Restoring builds a new store from the backup, checks it, and only then
switches the household over; the store it replaces is kept until the next restore.
"""
import csv
import hashlib
import io
import json
import secrets
import time
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import base64

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, insert
from sqlmodel import Session, SQLModel, select

from .db import (PG, PROFILE_TABLES, SITE_TABLES, HeldUpload, Profile, Setting, create_store, drop_store,
                 fix_sequences, forget_engine, init_profile_db, new_profile_file, schema_of, site_engine,
                 store_engine, store_exists)

FORMAT, VERSION = "payday-profile-backup", 1
SECRET_SETTINGS = {"akahu_tokens", "akahu_test", "last_sync_log", "legacy_env_tokens"}
MAX_UPLOAD = 100 * 1024 * 1024  # bytes, compressed
MAX_JSON = 400 * 1024 * 1024  # bytes, once unzipped
LOCKED = b"PAYDAY-LOCKED-1\n"  # start of a passphrase-locked backup
MODELS = {m.__table__.name: m for m in SQLModel.__subclasses__() if hasattr(m, "__table__")}


class BadBackup(ValueError):
    """The file isn't a Payday backup we can restore. The message is shown as written."""


def _key(passphrase: str, salt: bytes) -> bytes:
    return base64.urlsafe_b64encode(hashlib.scrypt(passphrase.encode(), salt=salt, n=2**15, r=8, p=1,
                                                   maxmem=64 * 1024 * 1024, dklen=32))


def lock(raw: bytes, passphrase: str) -> bytes:
    """Encrypt a backup with a passphrase (scrypt, then AES via Fernet)."""
    salt = secrets.token_bytes(16)
    return LOCKED + base64.b64encode(salt) + b"\n" + Fernet(_key(passphrase, salt)).encrypt(raw)


def is_locked(raw: bytes) -> bool:
    return raw.startswith(LOCKED)


def unlock(raw: bytes, passphrase: str) -> bytes:
    try:
        salt_line, blob = raw[len(LOCKED):].split(b"\n", 1)
        return Fernet(_key(passphrase, base64.b64decode(salt_line))).decrypt(blob)
    except (ValueError, InvalidToken):
        raise BadBackup("That passphrase doesn’t open this backup.") from None


def _plain(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _rows(s: Session, table, keep_secrets: bool = False) -> list[dict]:
    rows = [dict(r._mapping) for r in s.connection().execute(table.select())]
    if table.name == "setting" and not keep_secrets:
        rows = [r for r in rows if r["key"] not in SECRET_SETTINGS]
    return [{k: _plain(v) for k, v in r.items()} for r in rows]


def snapshot(s: Session) -> dict:
    """Everything in one profile, ready to write as JSON."""
    return {t.name: _rows(s, t) for t in PROFILE_TABLES}


def backup_zip(s: Session, profile_name: str, passphrase: str = "") -> bytes:
    body = {"format": FORMAT, "version": VERSION, "created": datetime.now(timezone.utc).isoformat(),
            "profile": profile_name, "tables": snapshot(s)}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("backup.json", json.dumps(body, separators=(",", ":")))
        z.writestr("README.txt", "Payday backup of one household. Restore it in Payday: Settings, Backup and export.\n"
                                 "Bank connections are not included: connect again after restoring.\n")
    return lock(buf.getvalue(), passphrase) if passphrase else buf.getvalue()


def _safe_cell(v) -> str:
    """Stop a spreadsheet treating text from the bank as a formula (e.g. a description starting with =)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    text = str(_plain(v))
    if text[:1] in ("=", "+", "@", "\t", "\r") or (text[:1] == "-" and not _number(text)):
        return "'" + text
    return text


def _number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def export_zip(s: Session) -> bytes:
    """One CSV per table, for a spreadsheet."""
    buf = io.BytesIO()
    names = {"transaction": "transactions", "pendingtransaction": "pending", "account": "accounts",
             "category": "categories", "rule": "rules", "bill": "bills", "loan": "mortgage", "loanrate": "mortgage-rates",
             "loanentry": "mortgage-repayments", "propertyvalue": "property-values", "goal": "goals", "setting": "settings"}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for t in PROFILE_TABLES:
            rows = _rows(s, t)
            out = io.StringIO()
            w = csv.writer(out)
            cols = [c.name for c in t.columns]
            w.writerow(cols)
            for r in rows:
                w.writerow([_safe_cell(r.get(c)) for c in cols])
            z.writestr(f"{names.get(t.name, t.name)}.csv", "﻿" + out.getvalue())  # BOM: Excel reads UTF-8
    return buf.getvalue()


def read_backup(raw: bytes, passphrase: str = "") -> dict:
    """Parse and check an uploaded backup. Raises BadBackup with a message for the person."""
    if len(raw) > MAX_UPLOAD:
        raise BadBackup("That file is too big to be a Payday backup.")
    if is_locked(raw):
        if not passphrase:
            raise BadBackup("This backup is locked. Enter its passphrase.")
        raw = unlock(raw, passphrase)
    text = None
    if raw[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                info = next((i for i in z.infolist() if i.filename == "backup.json"), None)
                if not info:
                    raise BadBackup("That zip has no backup.json in it. Choose a backup made by Payday.")
                if info.file_size > MAX_JSON:
                    raise BadBackup("That backup is too big to restore here.")
                with z.open(info) as f:
                    text = f.read(MAX_JSON + 1)
        except zipfile.BadZipFile:
            raise BadBackup("That zip file is damaged.") from None
    else:
        text = raw
    try:
        body = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        raise BadBackup("That file isn't a Payday backup.") from None
    if not isinstance(body, dict) or body.get("format") != FORMAT:
        raise BadBackup("That file isn't a Payday profile backup.")
    if not isinstance(body.get("version"), int) or body["version"] > VERSION:
        raise BadBackup("That backup was made by a newer Payday. Update Payday first.")
    tables = body.get("tables")
    if not isinstance(tables, dict):
        raise BadBackup("That backup has no data in it.")
    known = {t.name for t in PROFILE_TABLES}
    clean: dict[str, list[dict]] = {}
    for name, rows in tables.items():
        if name not in known or not isinstance(rows, list):
            continue  # a table this version doesn't know is skipped, never created
        model = MODELS[name]
        cols = set(model.__table__.columns.keys())
        good = []
        for r in rows:
            if not isinstance(r, dict):
                raise BadBackup(f"Something in that backup's {name} list isn't a row.")
            row = {k: v for k, v in r.items() if k in cols}
            if name == "setting" and row.get("key") in SECRET_SETTINGS:
                continue
            try:  # check and convert every value with the table's own rules (dates, numbers, yes/no)
                good.append(model.model_validate(row).model_dump(mode="json", include=cols))
            except Exception as e:  # noqa: BLE001 - one message for any bad value
                raise BadBackup(f"A row in that backup's {name} list has a value Payday can't use.") from e
        clean[name] = good
    if not clean.get("category") and not clean.get("transaction"):
        raise BadBackup("That backup is empty.")
    body["tables"] = clean
    return body


def counts(body: dict) -> dict:
    t = body["tables"]
    return {"transactions": len(t.get("transaction", [])), "accounts": len(t.get("account", [])),
            "bills": len(t.get("bill", [])), "rules": len(t.get("rule", [])),
            "categories": len(t.get("category", [])), "mortgage repayments": len(t.get("loanentry", []))}


# ---------- holding an upload between "check" and "restore" ----------
def hold(body: dict, profile_id: int, user_id: int) -> str:
    clean_temp()
    token = secrets.token_urlsafe(24)
    with Session(site_engine) as s:
        s.add(HeldUpload(token_hash=hashlib.sha256(token.encode()).hexdigest(), profile_id=profile_id,
                         user_id=user_id, body=json.dumps(body), created_at=datetime.now(timezone.utc)))
        s.commit()
    return token


def take(token: str, profile_id: int, user_id: int) -> dict | None:
    if not token or len(token) > 100:
        return None
    with Session(site_engine) as s:
        held = s.exec(select(HeldUpload).where(HeldUpload.token_hash == hashlib.sha256(token.encode()).hexdigest())).first()
        if not held or held.profile_id != profile_id or held.user_id != user_id:
            return None
        body = json.loads(held.body)
        s.delete(held)
        s.commit()
    return body


def clean_temp(max_age: int = 3600) -> None:
    cutoff = datetime.fromtimestamp(time.time() - max_age, timezone.utc)
    with Session(site_engine) as s:
        s.exec(delete(HeldUpload).where(HeldUpload.created_at < cutoff))
        s.commit()


def utc(row: dict) -> dict:
    """SQLite gives times back without a time zone; they were saved in UTC, so say so."""
    return {k: (v.replace(tzinfo=timezone.utc) if isinstance(v, datetime) and v.tzinfo is None else v) for k, v in row.items()}


def fill(eng, tables: dict) -> None:
    """Copy checked rows (from read_backup or another database) into an empty household store."""
    with eng.begin() as conn:
        for t in PROFILE_TABLES:  # parents before children
            model, cols = MODELS[t.name], set(t.columns.keys())
            rows = [utc(model.model_validate(r).model_dump(include=cols)) for r in tables.get(t.name) or []]
            if rows:
                conn.execute(insert(t), rows)
    fix_sequences(eng)


def restore(profile_id: int, body: dict) -> None:
    """Replace one household's data with a checked backup (from read_backup)."""
    name = new_profile_file()
    create_store(name)
    eng = store_engine(name)
    try:
        SQLModel.metadata.create_all(eng, tables=PROFILE_TABLES)
        fill(eng, body["tables"])
        init_profile_db(eng)  # adds anything a newer version needs
        with Session(eng) as s:
            if s.get(Setting, "akahu_tokens"):
                raise RuntimeError("tokens in a restore")
    except Exception:
        if not PG:
            eng.dispose()
        drop_store(name)
        raise
    if not PG:
        eng.dispose()
    with Session(site_engine) as s:
        prof = s.get(Profile, profile_id)
        old, prof.db_file = prof.db_file, name
        prev = s.exec(select(SiteSettingRow).where(SiteSettingRow.key == f"before_restore_{profile_id}")).first()
        if prev and prev.value and prev.value != old:
            drop_store(prev.value)  # keep only the most recent replaced copy
        if prev:
            prev.value = old
        else:
            prev = SiteSettingRow(key=f"before_restore_{profile_id}", value=old)
        s.add(prev)
        s.add(prof)
        s.commit()
    forget_engine(profile_id)


def drop_profile_data(prof: Profile) -> None:
    """Delete a household's store and the copy kept from its last restore."""
    forget_engine(prof.id)
    drop_store(prof.db_file)
    with Session(site_engine) as s:
        prev = s.get(SiteSettingRow, f"before_restore_{prof.id}")
        if prev:
            if prev.value:
                drop_store(prev.value)
            s.delete(prev)
            s.commit()


def site_backup_zip(profiles: list[Profile], key_file: Path) -> bytes:
    """Everything: the shared tables and every household, as JSON, plus the key file."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        with Session(site_engine) as s:
            shared = {t.name: _rows(s, t) for t in SITE_TABLES if t.name != "heldupload"}
        z.writestr("site.json", json.dumps({"format": "payday-site-backup", "version": 1,
                                            "created": datetime.now(timezone.utc).isoformat(), "tables": shared}))
        for p in profiles:
            eng = store_engine(p.db_file)
            if not store_exists(p.db_file):
                continue
            with Session(eng) as s:
                body = {"format": FORMAT, "version": VERSION, "profile": p.name, "tables": {t.name: _rows(s, t, keep_secrets=True)
                                                                                           for t in PROFILE_TABLES}}
            z.writestr(f"profiles/{p.id}-{_slug(p.name)}.json", json.dumps(body, separators=(",", ":")))
        if key_file.exists():
            z.write(key_file, "payday.key")
        z.writestr("README.txt", "Whole Payday site backup. payday.key decrypts saved bank tokens and 2FA keys:\n"
                                 "keep this file safe. Restore with: python -m app.admin restore-site <this zip>\n")
    return buf.getvalue()


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")[:30] or "profile"


def filename(kind: str, name: str, locked: bool = False) -> str:
    return f"payday-{_slug(name)}-{kind}-{date.today().isoformat()}.{'payday' if locked else 'zip'}"


from .db import SiteSetting as SiteSettingRow  # noqa: E402  (kept apart to avoid a name clash above)

__all__ = ["BadBackup", "backup_zip", "export_zip", "read_backup", "counts", "hold", "take", "restore",
           "site_backup_zip", "filename", "clean_temp", "drop_profile_data", "fill", "lock", "schema_of"]
