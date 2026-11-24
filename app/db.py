"""Tables, and which database they live in.

Shared tables (people, profiles, memberships, invites, site details) are kept apart from each household's money.
With SQLite each household has its own file; with PostgreSQL each has its own schema. Every request works in
exactly one household's store, chosen after checking you belong to it.
"""
import os
import re
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date as Date, datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, create_engine, select

def _db_url() -> str:
    """DATABASE_URL, or the contents of the file DATABASE_URL_FILE points at (keeps the password out of env files)."""
    path = os.getenv("DATABASE_URL_FILE", "")
    if path:
        try:
            return Path(path).read_text().strip()
        except OSError:
            pass
    return os.getenv("DATABASE_URL", "sqlite:///./budget.db")


DB_URL = _db_url()
PG = DB_URL.startswith(("postgresql", "postgres://"))
if DB_URL.startswith("postgres://"):
    DB_URL = "postgresql+psycopg://" + DB_URL[len("postgres://"):]
elif DB_URL.startswith("postgresql://"):
    DB_URL = "postgresql+psycopg://" + DB_URL[len("postgresql://"):]


def data_dir() -> Path:
    if os.getenv("DATA_DIR"):
        return Path(os.environ["DATA_DIR"])
    if DB_URL.startswith("sqlite:///"):
        return Path(DB_URL[len("sqlite:///"):]).resolve().parent
    return Path("/data") if Path("/data").is_dir() else Path(".").resolve()


LEGACY_DB = Path(DB_URL[len("sqlite:///"):]).resolve() if DB_URL.startswith("sqlite:///") else None
SITE_DB = Path(os.getenv("SITE_DB") or data_dir() / "site.db")
PROFILE_DIR = data_dir() / "profiles"
SCHEMA_RE = re.compile(r"^p_[0-9a-f]{8,32}$")


def _make_engine(path: Path) -> Engine:
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 15})

    @event.listens_for(eng, "connect")
    def _pragmas(conn, _record):
        cur = conn.cursor()
        cur.execute("PRAGMA secure_delete = ON")
        cur.close()
    return eng


if PG:
    base_engine = create_engine(DB_URL, pool_size=int(os.getenv("DB_POOL", "5")), max_overflow=10,
                                pool_pre_ping=True, pool_recycle=1800)
    site_engine = base_engine
else:
    base_engine = None
    site_engine = _make_engine(SITE_DB)
_engines: dict[int, Engine] = {}
_current: ContextVar[Optional[int]] = ContextVar("current_profile", default=None)


class NoProfile(RuntimeError):
    """Code tried to reach a profile's data without a profile chosen (a bug, never shown as data)."""


def profile_path(profile: "Profile") -> Path:
    p = Path(profile.db_file)
    return p if p.is_absolute() else data_dir() / p


def store_engine(name: str) -> Engine:
    """The engine for one household's store: a file name (SQLite) or a schema name (PostgreSQL)."""
    if PG:
        if not SCHEMA_RE.match(name):
            raise NoProfile(f"Bad schema name {name!r}")
        return base_engine.execution_options(schema_translate_map={None: name})
    p = Path(name)
    return _make_engine(p if p.is_absolute() else data_dir() / p)


def schema_of(eng: Engine) -> Optional[str]:
    return (eng.get_execution_options().get("schema_translate_map") or {}).get(None)


def create_store(name: str) -> None:
    if PG:
        with base_engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{name}"'))
    else:
        (data_dir() / name).parent.mkdir(parents=True, exist_ok=True)


def drop_store(name: str) -> None:
    if PG:
        if SCHEMA_RE.match(name):
            with base_engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
    else:
        path = Path(name) if Path(name).is_absolute() else data_dir() / name
        path.unlink(missing_ok=True)


def store_exists(name: str) -> bool:
    if PG:
        return inspect(base_engine).has_schema(name)
    path = Path(name) if Path(name).is_absolute() else data_dir() / name
    return path.exists()


def engine_for(profile_id: int) -> Engine:
    if profile_id not in _engines:
        with Session(site_engine) as s:
            prof = s.get(Profile, profile_id)
            if not prof:
                raise NoProfile(f"No profile {profile_id}")
            name = prof.db_file
        _engines[profile_id] = store_engine(name)
    return _engines[profile_id]


def forget_engine(profile_id: int) -> None:
    eng = _engines.pop(profile_id, None)
    if eng is not None and not PG:
        eng.dispose()


def current_profile_id() -> int:
    pid = _current.get()
    if pid is None:
        raise NoProfile("No profile chosen")
    return pid


def current_engine() -> Engine:
    return engine_for(current_profile_id())


@contextmanager
def use_profile(profile_id: int):
    """Work in one profile's store for the duration of the block (requests, the sync, tests)."""
    token = _current.set(profile_id)
    try:
        yield engine_for(profile_id)
    finally:
        _current.reset(token)


class Account(SQLModel, table=True):
    id: str = Field(primary_key=True)  # Akahu acc_... id
    name: str  # what Payday shows: your nickname, or the bank's name if you haven't given one
    bank_name: str = ""  # the bank's own name for the account
    nickname: str = ""
    meta: str = ""  # extra details from Akahu as JSON (for a loan: rate, fixed-term end, next repayment)
    institution: str = ""
    type: str = ""
    balance_current: float = 0
    balance_available: Optional[float] = None
    balance_limit: Optional[float] = None  # overdraft or credit limit, when the bank tells us
    in_budget: bool = True  # its balance counts towards "money available"
    count_spending: bool = True  # its spending counts in the pay-period budget
    opening_balance: Optional[float] = None  # if set, balance = this + transactions since opening_date
    opening_date: Optional[Date] = None
    updated_at: Optional[datetime] = None
    bank_status: str = ""  # Akahu's ACTIVE / INACTIVE for the bank connection
    refreshed_txns: str = ""  # when Akahu last fetched this account's transactions from the bank (ISO, UTC)
    refreshed_balance: str = ""  # ... and its balance


class Category(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    kind: str = "expense"  # income | expense | transfer
    limit_mode: str = "auto"  # auto (usual day-to-day over the last 3 pay periods + bills due) | fixed | off
    limit_amount: Optional[float] = None  # used when limit_mode == fixed


class Rule(SQLModel, table=True):
    """Text pattern -> category. Plain text = case-insensitive 'contains'; prefix 're:' for regex."""
    id: Optional[int] = Field(default=None, primary_key=True)
    pattern: str = ""  # may be blank when other conditions are set
    category_id: int = Field(foreign_key="category.id")
    source: str = "user"  # user | learned
    priority: int = 100
    direction: str = "any"  # any | out | in
    min_amount: Optional[float] = None  # compared with the amount ignoring sign
    max_amount: Optional[float] = None
    account_id: Optional[str] = None


class Bill(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    amount: float  # expected amount, positive
    tolerance_pct: float = 10
    frequency: str = "monthly"  # weekly | fortnightly | monthly | quarterly | yearly
    anchor_date: Date  # any known due date
    match_pattern: str  # text seen in bank description/merchant
    window_days: int = 4  # how far either side of due date a payment can land
    category_id: Optional[int] = Field(default=None, foreign_key="category.id")
    active: bool = True
    account_id: Optional[str] = None  # the account it comes out of; None = worked out from its past payments


class Transaction(SQLModel, table=True):
    id: str = Field(primary_key=True)  # Akahu trans_... id
    account_id: str = Field(index=True)
    date: Date = Field(index=True)
    description: str
    merchant: str = ""
    amount: float  # negative = money out
    akahu_type: str = ""
    akahu_group: str = ""
    category_id: Optional[int] = Field(default=None, foreign_key="category.id")
    category_source: str = ""  # bill | rule | learned | akahu | refund | default | manual
    bill_id: Optional[int] = Field(default=None, index=True)
    bill_due_date: Optional[Date] = None
    transfer_id: Optional[str] = Field(default=None, index=True)  # the matching other half of a transfer
    note: str = ""
    tags: str = ""  # comma separated, lower case
    seq: Optional[int] = Field(default=None, index=True)  # bank order within a day: higher = later
    reviewed: bool = False  # you've checked a guessed category (Akahu's, or the fallback) and it's right


class User(SQLModel, table=True):
    """A person who can sign in (site database). What they can do depends on the profile: see Membership."""
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    role: str = "viewer"  # set per request from the membership of the profile being viewed; not stored meaningfully
    is_owner: bool = False  # runs this Payday site: creates profiles, resets sign-ins. Sees no money by itself
    session_version: int = 1  # bump to sign this user out everywhere
    text_size: str = "normal"  # small | normal | large
    privacy: bool = False  # blur amounts for this user
    theme: str = "bush"  # see app/themes.py
    mode: str = "auto"  # auto | light | dark
    totp_secret: str = ""  # encrypted with the site key
    totp_enabled: bool = False
    totp_last_step: int = 0  # a code can't be used twice
    backup_codes: str = ""  # JSON list of hashed one-time codes
    last_profile_id: Optional[int] = None
    created_at: Optional[datetime] = None
    email: Optional[str] = Field(default=None, index=True)  # for sign-up checks and password resets
    email_verified: bool = False
    status: str = "active"  # active | unverified (email not confirmed yet) | pending (waiting for the owner)
    terms_accepted_at: Optional[datetime] = None
    signup_household: str = ""  # the household name chosen at sign-up, made into a profile once approved
    badge_emoji: str = ""  # shown instead of the initial, from BADGE_EMOJI
    badge_colour: str = ""  # from BADGE_COLOURS


class SiteSetting(SQLModel, table=True):
    """Site-wide details the owner fills in (business name, contact email, sign-up mode...)."""
    key: str = Field(primary_key=True)
    value: str = ""


class HeldUpload(SQLModel, table=True):
    """A checked backup waiting for the person to confirm the restore (kept in the database so any
    copy of the app can finish it). Removed when used or after an hour."""
    id: Optional[int] = Field(default=None, primary_key=True)
    token_hash: str = Field(index=True, unique=True)
    profile_id: int
    user_id: int
    body: str
    created_at: Optional[datetime] = None


class Profile(SQLModel, table=True):
    """One household's Payday (site database). Its data lives in its own database file."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    db_file: str  # SQLite: file relative to the data folder. PostgreSQL: the schema name
    created_at: Optional[datetime] = None


class Membership(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    profile_id: int = Field(index=True)
    role: str = "viewer"  # admin | viewer (read only)
    notify: str = ""  # this person's alert choices for this household, as JSON (see app/notify.py)


class PushDevice(SQLModel, table=True):
    """A phone or browser that has said yes to notifications."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    endpoint: str = Field(unique=True)
    p256dh: str
    auth: str
    label: str = ""  # e.g. "iPhone", "Chrome on Windows"
    created: Optional[datetime] = None
    last_sent: Optional[datetime] = None


class NotifySent(SQLModel, table=True):
    """One alert already sent, so it isn't sent again."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    profile_id: int
    key: str = Field(index=True)
    sent_at: Optional[datetime] = None


class Invite(SQLModel, table=True):
    """A one-time link: join a profile, or (from the site owner) set a new password."""
    id: Optional[int] = Field(default=None, primary_key=True)
    token_hash: str = Field(index=True, unique=True)  # the link holds the token; only its hash is stored
    kind: str = "join"  # join | reset | verify
    profile_id: Optional[int] = None
    user_id: Optional[int] = None  # for a password reset
    role: str = "viewer"
    note: str = ""  # who it's for, to recognise it in the list
    created_by: Optional[int] = None
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    used_at: Optional[datetime] = None


class PendingTransaction(SQLModel, table=True):
    """Not yet settled. Akahu gives these no stable id, so the table is rebuilt on every sync."""
    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: str
    date: Date
    description: str
    amount: float
    type: str = ""


class Loan(SQLModel, table=True):
    """A mortgage. Its history comes from imported rows (your spreadsheet) and then the bank's loan account."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = "Mortgage"
    account_id: Optional[str] = None  # the Akahu loan account, if connected
    pay_account_id: Optional[str] = None  # the account repayments come out of (kept topped up)
    original_amount: float = 0
    start_date: Optional[Date] = None
    term_years: int = 30
    deposit: float = 0
    payment_day: int = 20  # day of the month repayments come out
    fixed_until: Optional[Date] = None
    rate_after: Optional[float] = None  # % p.a. you expect after the fixed term ends
    payment_after: Optional[float] = None  # the bank's new repayment after refixing, if you know it


class LoanRate(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    loan_id: int = Field(index=True)
    effective: Date
    rate: float  # % per year
    note: str = ""


class LoanEntry(SQLModel, table=True):
    """One repayment: from your spreadsheet (source "import") or worked out from the bank ("bank")."""
    id: Optional[int] = Field(default=None, primary_key=True)
    loan_id: int = Field(index=True)
    date: Date = Field(index=True)
    payment: float
    interest: float
    principal: float
    balance: float  # owed straight after this payment
    rate: Optional[float] = None
    source: str = "import"


class PropertyValue(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    loan_id: int = Field(index=True)
    date: Date
    value: float
    note: str = ""


class Goal(SQLModel, table=True):
    """Something to save for. Progress comes from a linked account's balance, or amounts added by hand."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    target: float
    target_date: Optional[Date] = None
    account_id: Optional[str] = None  # a savings account whose balance is the progress
    start_amount: float = 0  # with an account: only the balance above this counts
    manual_saved: float = 0  # without an account: what's been put aside so far
    budget_it: bool = False  # take the per-pay amount out of the spending money, like a bill
    created: Optional[Date] = None
    done: bool = False


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


DEFAULTS = {
    "payday_day": os.getenv("PAYDAY_DAY", "15"),
    "payday_adjust": os.getenv("PAYDAY_ADJUST", "previous_business_day"),
    "buffer": "0",
    "sync_hour": os.getenv("SYNC_HOUR", "6"),
    "last_sync": "",
    "last_sync_status": "",
}


def get_setting(session: Session, key: str) -> str:
    row = session.get(Setting, key)
    return row.value if row else DEFAULTS.get(key, "")


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key) or Setting(key=key, value=value)
    row.value = value
    session.add(row)
    session.commit()


SPENDING_TYPES = ("CHECKING", "CREDITCARD")


NEW_COLUMNS = {  # added to profile databases after the first release
    "transaction": [("transfer_id", "VARCHAR"), ("note", "VARCHAR NOT NULL DEFAULT ''"),
                    ("tags", "VARCHAR NOT NULL DEFAULT ''"), ("seq", "INTEGER"),
                    ("reviewed", "BOOLEAN NOT NULL DEFAULT 1")],
    "user": [("theme", "VARCHAR NOT NULL DEFAULT 'bush'"), ("mode", "VARCHAR NOT NULL DEFAULT 'auto'")],
    "account": [("count_spending", "BOOLEAN NOT NULL DEFAULT 1"), ("opening_balance", "FLOAT"),
                ("opening_date", "DATE"), ("balance_limit", "FLOAT"), ("bank_name", "VARCHAR NOT NULL DEFAULT ''"),
                ("nickname", "VARCHAR NOT NULL DEFAULT ''"), ("meta", "VARCHAR NOT NULL DEFAULT ''"),
                ("bank_status", "VARCHAR NOT NULL DEFAULT ''"), ("refreshed_txns", "VARCHAR NOT NULL DEFAULT ''"),
                ("refreshed_balance", "VARCHAR NOT NULL DEFAULT ''")],
    "bill": [("account_id", "VARCHAR")],
    "category": [("limit_mode", "VARCHAR NOT NULL DEFAULT 'auto'"), ("limit_amount", "FLOAT")],
    "rule": [("direction", "VARCHAR NOT NULL DEFAULT 'any'"), ("min_amount", "FLOAT"),
             ("max_amount", "FLOAT"), ("account_id", "VARCHAR")],
}


SITE_TABLES = [User.__table__, Profile.__table__, Membership.__table__, Invite.__table__,
               SiteSetting.__table__, PushDevice.__table__, NotifySent.__table__, HeldUpload.__table__]
PROFILE_TABLES = [t for t in SQLModel.metadata.sorted_tables if t not in SITE_TABLES]
SITE_COLUMNS = {
    "user": [("is_owner", "BOOLEAN NOT NULL DEFAULT 0"), ("totp_secret", "VARCHAR NOT NULL DEFAULT ''"),
             ("totp_enabled", "BOOLEAN NOT NULL DEFAULT 0"), ("totp_last_step", "INTEGER NOT NULL DEFAULT 0"),
             ("backup_codes", "VARCHAR NOT NULL DEFAULT ''"), ("last_profile_id", "INTEGER"), ("email", "VARCHAR"),
             ("email_verified", "BOOLEAN NOT NULL DEFAULT 0"), ("status", "VARCHAR NOT NULL DEFAULT 'active'"),
             ("terms_accepted_at", "TIMESTAMP"), ("signup_household", "VARCHAR NOT NULL DEFAULT ''"),
             ("badge_emoji", "VARCHAR NOT NULL DEFAULT ''"), ("badge_colour", "VARCHAR NOT NULL DEFAULT ''")],
    "membership": [("notify", "VARCHAR NOT NULL DEFAULT ''")],
}


def _ddl(ddl: str) -> str:
    """SQLite and PostgreSQL write true/false defaults differently."""
    if PG and "BOOLEAN" in ddl:
        ddl = ddl.replace("DEFAULT 1", "DEFAULT true").replace("DEFAULT 0", "DEFAULT false")
    return ddl


def _add_columns(eng: Engine, wanted: dict) -> None:
    schema = schema_of(eng)
    insp = inspect(base_engine if PG else eng)
    qual = (lambda t: f'"{schema}"."{t}"') if schema else (lambda t: f'"{t}"')
    with (base_engine if PG else eng).begin() as conn:
        for table, cols in wanted.items():
            if not insp.has_table(table, schema=schema):
                continue
            have = {c["name"] for c in insp.get_columns(table, schema=schema)}
            for name, ddl in cols:
                if name in have:
                    continue
                conn.execute(text(f"ALTER TABLE {qual(table)} ADD COLUMN {name} {_ddl(ddl)}"))
                if PG:
                    continue  # the fixes below are for SQLite files from before PostgreSQL support
                if (table, name) == ("account", "count_spending"):
                    conn.execute(text("UPDATE account SET count_spending = CASE WHEN type IN "
                                      "('CHECKING','CREDITCARD') THEN 1 ELSE 0 END"))
                if (table, name) == ("account", "bank_name"):
                    conn.execute(text("UPDATE account SET bank_name = name"))
                if (table, name) == ("transaction", "reviewed"):
                    conn.execute(text('UPDATE "transaction" SET reviewed = 0 WHERE category_source IN '
                                      "('akahu', 'default') AND date >= date('now', '-35 days')"))
                if (table, name) == ("transaction", "seq"):
                    conn.execute(text('UPDATE "transaction" SET seq = -rowid'))
                if (table, name) == ("category", "limit_mode"):
                    conn.execute(text("UPDATE category SET limit_mode = 'off' WHERE kind != 'expense'"))
                if (table, name) == ("user", "email_verified"):
                    conn.execute(text('UPDATE "user" SET email_verified = 0'))


def init_profile_db(eng: Engine) -> None:
    """Create or upgrade one household's store."""
    if schema_of(eng):
        create_store(schema_of(eng))
    SQLModel.metadata.create_all(eng, tables=PROFILE_TABLES)
    _add_columns(eng, NEW_COLUMNS)
    with Session(eng) as s:
        for name, kind in [("Income", "income"), ("Transfer", "transfer"),
                           ("Bills", "expense"), ("Uncategorised", "expense")]:
            if not s.exec(select(Category).where(Category.name == name)).first():
                s.add(Category(name=name, kind=kind))
        s.commit()


def fix_sequences(eng: Engine) -> None:
    """After rows are copied in with their ids (restore, move to PostgreSQL), start new ids after the highest."""
    schema = schema_of(eng)
    if not PG:
        return
    with base_engine.begin() as conn:
        for t in (PROFILE_TABLES if schema else SITE_TABLES):
            col = t.columns.get("id")
            if col is None or not str(col.type).upper().startswith("INT"):
                continue
            full = f'"{schema}"."{t.name}"' if schema else f'"{t.name}"'
            conn.execute(text(f"SELECT setval(pg_get_serial_sequence('{full}', 'id'), "
                              f"COALESCE((SELECT MAX(id) FROM {full}), 0) + 1, false)"))


def new_profile_file() -> str:
    """A name for a new household store."""
    if PG:
        return f"p_{secrets.token_hex(8)}"
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return str(Path("profiles") / f"{secrets.token_hex(8)}.db")


def _legacy_users() -> list[dict]:
    """People from before profiles existed, kept in the old single database."""
    if not LEGACY_DB or not LEGACY_DB.exists():
        return []
    eng = _make_engine(LEGACY_DB)
    try:
        if not inspect(eng).has_table("user"):
            return []
        with eng.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(text('SELECT * FROM "user" ORDER BY id'))]
    finally:
        eng.dispose()


def init_db() -> None:
    """Create the shared tables, and on the first start after the profiles upgrade turn the old single SQLite
    database into the first profile (its people move across with the same passwords)."""
    if not PG:
        SITE_DB.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(site_engine, tables=SITE_TABLES)
    _add_columns(site_engine, SITE_COLUMNS)
    with Session(site_engine) as s:
        if not PG and not s.exec(select(Profile)).first() and LEGACY_DB and LEGACY_DB.exists() and LEGACY_DB != SITE_DB:
            try:
                rel = str(LEGACY_DB.relative_to(data_dir()))
            except ValueError:
                rel = str(LEGACY_DB)
            first = Profile(name=os.getenv("PROFILE_NAME", "Home"), db_file=rel, created_at=datetime.now(timezone.utc))
            s.add(first)
            s.commit()
            s.refresh(first)
            old = _legacy_users()
            owner_id = next((u["id"] for u in old if u.get("role") == "admin"), None)
            for u in old:
                if s.exec(select(User).where(User.username == u["username"])).first():
                    continue
                user = User(id=u["id"], username=u["username"], password_hash=u["password_hash"],
                            session_version=(u.get("session_version") or 1) + 1,
                            text_size=u.get("text_size") or "normal", privacy=bool(u.get("privacy")),
                            theme=u.get("theme") or "bush", mode=u.get("mode") or "auto",
                            is_owner=u["id"] == owner_id, created_at=datetime.now(timezone.utc), last_profile_id=first.id)
                s.add(user)
                s.add(Membership(user_id=u["id"], profile_id=first.id,
                                 role="admin" if u.get("role") == "admin" else "viewer"))
            s.commit()
            init_profile_db(engine_for(first.id))
            with Session(engine_for(first.id)) as ps:
                set_setting(ps, "legacy_env_tokens", "1")
    with Session(site_engine) as s:
        profiles = s.exec(select(Profile)).all()
    for prof in profiles:
        if not PG:
            profile_path(prof).parent.mkdir(parents=True, exist_ok=True)
        init_profile_db(engine_for(prof.id))
        if not PG:
            _drop_legacy_users(engine_for(prof.id))


def _drop_legacy_users(eng: Engine) -> None:
    """Sign-in details live only in the shared tables; a household's file (and its backups) holds none."""
    with Session(site_engine) as s:
        moved = {u.username for u in s.exec(select(User))}
    insp = inspect(eng)
    if not insp.has_table("user"):
        return
    with eng.begin() as conn:
        names = {r[0] for r in conn.execute(text('SELECT username FROM "user"'))}
        if names <= moved:
            conn.execute(text('DROP TABLE "user"'))


def site_setting(key: str, default: str = "") -> str:
    with Session(site_engine) as s:
        row = s.get(SiteSetting, key)
        return row.value if row and row.value else default


def set_site_setting(s: Session, key: str, value: str) -> None:
    row = s.get(SiteSetting, key) or SiteSetting(key=key)
    row.value = value
    s.add(row)
    s.commit()
