import json
import time
import os
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text
from sqlmodel import Session, delete, select

from . import splits, vault
from .categorise import Categoriser, categorise, pair_transfers
from .db import PG, SPENDING_TYPES, base_engine, Account, PendingTransaction, Transaction, current_engine, current_profile_id, set_setting, use_profile

BASE = "https://api.akahu.io/v1"
TZ = ZoneInfo(os.getenv("TZ_NAME", "Pacific/Auckland"))
FIRST_SYNC_DAYS = 120
OVERLAP_DAYS = 10  # banks sometimes post transactions late
_locks: dict[int, threading.Lock] = {}  # one sync at a time per profile


class Progress:
    """What the running (or last) sync is doing, readable from the web page while it runs."""

    def __init__(self):
        self._guard = threading.Lock()
        self.state = {"running": False, "ok": None, "steps": [], "started": "", "finished": "", "trigger": ""}

    def start(self, trigger: str):
        with self._guard:
            self.state = {"running": True, "ok": None, "steps": [], "trigger": trigger,
                          "started": datetime.now(TZ).strftime("%a %-d %b %H:%M:%S"), "finished": ""}

    def step(self, label: str, detail: str = ""):
        with self._guard:
            if self.state["steps"] and self.state["steps"][-1]["status"] == "running":
                self.state["steps"][-1]["status"] = "done"
            self.state["steps"].append({"label": label, "detail": detail, "status": "running"})

    def detail(self, detail: str):
        with self._guard:
            if self.state["steps"]:
                self.state["steps"][-1]["detail"] = detail

    def finish(self, ok: bool, message: str):
        with self._guard:
            if self.state["steps"] and self.state["steps"][-1]["status"] == "running":
                self.state["steps"][-1]["status"] = "done" if ok else "failed"
            self.state["steps"].append({"label": message, "detail": "", "status": "done" if ok else "failed"})
            self.state.update(running=False, ok=ok, finished=datetime.now(TZ).strftime("%a %-d %b %H:%M:%S"))

    def snapshot(self) -> dict:
        with self._guard:
            return json.loads(json.dumps(self.state))


_progress: dict[int, Progress] = {}


def _lock_for(pid: int) -> threading.Lock:
    return _locks.setdefault(pid, threading.Lock())


def progress_for(pid: int | None = None) -> Progress:
    pid = current_profile_id() if pid is None else pid
    return _progress.setdefault(pid, Progress())


def is_running(pid: int | None = None) -> bool:
    return _lock_for(current_profile_id() if pid is None else pid).locked()


def start_in_background(trigger: str = "manual", pid: int | None = None) -> bool:
    pid = current_profile_id() if pid is None else pid
    if _lock_for(pid).locked():
        return False
    threading.Thread(target=sync_profile, args=(pid, trigger), daemon=True).start()
    return True


def sync_profile(pid: int, trigger: str = "scheduled") -> str:
    """Sync one profile (used by the scheduler and background threads, which have no request).
    With PostgreSQL several copies of Payday may run; a database lock makes sure only one syncs a household."""
    msg = _sync_locked(pid, trigger)
    try:
        from .notify import run_for_profile  # noqa: PLC0415 - notify needs the pages' helpers, loaded after this module
        run_for_profile(pid)
    except Exception:  # noqa: BLE001 - a notification problem must never look like a failed sync
        import logging
        logging.getLogger("payday.notify").exception("notifications after sync failed")
    return msg


def _sync_locked(pid: int, trigger: str) -> str:
    with use_profile(pid):
        if not PG:
            return sync(trigger)
        with base_engine.connect() as conn:
            got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": 7_300_000 + pid}).scalar()
            if not got:
                return "A sync is already running."
            try:
                return sync(trigger)
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": 7_300_000 + pid})
                conn.commit()


class SyncProblem(Exception):
    """Something the person can fix; the message is shown to them as written."""


def _headers(app_token: str | None = None, user_token: str | None = None) -> dict:
    if app_token is None or user_token is None:
        t = vault.tokens()
        app_token, user_token = t["app"], t["user"]
    if not user_token or not app_token:
        raise SyncProblem("No bank is connected yet. An admin can connect one in Settings, Bank connection.")
    return {"Authorization": f"Bearer {user_token}", "X-Akahu-Id": app_token}


# ---------- Akahu Connect: subscribers sign in with Akahu and approve access (needs an approved Full App) ----------
OAUTH_URL = "https://oauth.akahu.nz"


def connect_enabled() -> bool:
    return bool(os.getenv("AKAHU_CLIENT_ID") and os.getenv("AKAHU_CLIENT_SECRET"))


def authorise_url(redirect_uri: str, state: str, email: str = "") -> str:
    from urllib.parse import urlencode
    q = {"response_type": "code", "client_id": os.environ["AKAHU_CLIENT_ID"], "redirect_uri": redirect_uri,
         "scope": "ENDURING_CONSENT", "state": state}
    if email:
        q["login_hint"] = email
    return f"{OAUTH_URL}?{urlencode(q)}"


def exchange_code(code: str, redirect_uri: str) -> str:
    """Swap the one-time code from Akahu for this household's access token (must happen within 60 seconds)."""
    r = httpx.post(f"{BASE}/token", timeout=30, json={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": os.environ["AKAHU_CLIENT_ID"], "client_secret": os.environ["AKAHU_CLIENT_SECRET"]})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not body.get("success") or not body.get("access_token"):
        raise SyncProblem(body.get("error_description") or body.get("message") or "Akahu didn't accept the sign-in. Try again.")
    return body["access_token"]


def disconnect(s) -> None:
    """Ask Akahu to stop sharing this household's data (Connect only), then forget the token."""
    t = vault.tokens(s)
    if t.get("via") == "connect" and t.get("user") and t.get("app"):
        try:
            httpx.delete(f"{BASE}/token", headers=_headers(t["app"], t["user"]), timeout=20)
        except httpx.HTTPError:
            pass  # forgetting it here still stops Payday using it; the person can also revoke at my.akahu.nz
    vault.forget_tokens(s)


def test_connection(app_token: str, user_token: str) -> tuple[bool, str]:
    """Try the tokens without syncing anything. Returns (worked, message to show)."""
    try:
        with httpx.Client(headers=_headers(app_token, user_token), timeout=30) as client:
            r = client.get(f"{BASE}/accounts")
            r.raise_for_status()
            n = len(r.json().get("items", []))
        return True, f"Connected. Akahu can see {n} account{'s' if n != 1 else ''}."
    except Exception as e:  # noqa: BLE001 - every failure becomes a message for the person
        return False, _explain(e)


def _explain(e: Exception) -> str:
    """Turn a failure into something a person can act on."""
    if isinstance(e, SyncProblem):
        return str(e)
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code in (401, 403):
            return (f"Akahu didn't accept the bank connection ({code}). Connect your bank again in Settings, "
                    "Bank connection, or check the app still has access at my.akahu.nz.")
        if code == 429:
            return "Akahu says too many requests were made. Wait a few minutes and try again."
        return f"Akahu returned an error ({code}). This is usually temporary; try again shortly."
    if isinstance(e, httpx.TimeoutException):
        return "Akahu took too long to answer. Try again shortly."
    if isinstance(e, httpx.TransportError):
        return "Couldn't reach Akahu. Check the server's internet connection."
    return f"{type(e).__name__}: {e}"


def _get_all(client: httpx.Client, path: str, params: dict | None = None, label: str = "") -> list[dict]:
    items, params, page = [], dict(params or {}), 0
    while True:
        r = client.get(f"{BASE}{path}", params=params)
        r.raise_for_status()
        body = r.json()
        items.extend(body.get("items", []))
        page += 1
        if label:
            progress_for().detail(f"{len(items)} {label} so far (page {page})")
        nxt = (body.get("cursor") or {}).get("next")
        if not nxt:
            return items
        params["cursor"] = nxt


def _local_date(iso: str):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ).date()


def _replace_stale(s: Session, stale: list[Transaction], added: list[Transaction]) -> int:
    """Delete transactions Akahu no longer has. Where a new copy arrived in this sync (same account and
    amount, within a few days), move your note, tags and hand-set category onto it."""
    spare = [t for t in added if t.category_source != "manual"]
    moved = 0
    for old in stale:
        if old.note or old.tags or old.category_source == "manual":
            twin = min((t for t in spare if t.account_id == old.account_id
                        and round(t.amount, 2) == round(old.amount, 2) and abs((t.date - old.date).days) <= 5),
                       key=lambda t: abs((t.date - old.date).days), default=None)
            if twin:
                spare.remove(twin)
                twin.note, twin.tags = old.note, old.tags
                if old.category_source == "manual":
                    twin.category_id, twin.category_source = old.category_id, "manual"
                    twin.bill_id, twin.bill_due_date = old.bill_id, old.bill_due_date
                    splits.move(s, old.id, twin.id)
                s.add(twin)
                moved += 1
        if old.transfer_id:
            other = s.get(Transaction, old.transfer_id)
            if other:
                other.transfer_id = None
                categorise(s, other)
                s.add(other)
        splits.clear(s, old.id)
        s.delete(old)
    s.commit()
    return moved


REFRESH_WAIT = 60  # seconds to wait for the banks to answer a refresh before carrying on


def _ask_for_fresh_data(client: httpx.Client, accounts: list[dict], progress) -> list[dict]:
    """Ask Akahu to fetch from the banks now, then wait a little for the answers.

    Akahu ignores the ask if an account was refreshed in the last 15 minutes, and otherwise
    collects data on its own about once a day, so this only runs when someone presses Sync now.
    """
    progress.step("Asking your bank for the latest")
    before = {a["_id"]: (a.get("refreshed") or {}).get("transactions") or "" for a in accounts
              if a.get("status", "ACTIVE") == "ACTIVE"}
    try:
        client.post("/refresh").raise_for_status()
    except httpx.HTTPError as e:
        progress.detail(f"Akahu didn't take the request ({_explain(e)}) Using what it already has.")
        return accounts
    deadline = time.monotonic() + REFRESH_WAIT
    latest = accounts
    while time.monotonic() < deadline:
        time.sleep(5)
        try:
            latest = _get_all(client, "/accounts")
        except httpx.HTTPError:
            break
        now = {a["_id"]: (a.get("refreshed") or {}).get("transactions") or "" for a in latest}
        done = [k for k, v in before.items() if now.get(k, "") != v]
        progress.detail(f"{len(done)} of {len(before)} account{'s' if len(before) != 1 else ''} updated by the bank")
        if len(done) == len(before):
            break
    else:
        progress.detail("Some banks hadn't answered yet; their new data comes in with the next sync. "
                        "Akahu also skips accounts it refreshed in the last 15 minutes.")
    return latest


def sync(trigger: str = "scheduled") -> str:
    lock, progress = _lock_for(current_profile_id()), progress_for()
    if not lock.acquire(blocking=False):
        return "A sync is already running."
    progress.start(trigger)
    try:
        with httpx.Client(headers=_headers(), timeout=60) as client, Session(current_engine()) as s:
            progress.step("Contacting Akahu")
            raw_accounts = _get_all(client, "/accounts")
            if trigger == "manual":
                raw_accounts = _ask_for_fresh_data(client, raw_accounts, progress)

            progress.step("Updating accounts")
            for a in raw_accounts:
                acc = s.get(Account, a["_id"])
                if not acc:
                    acc = Account(id=a["_id"], name=a.get("name", ""),
                                  in_budget=a.get("type") == "CHECKING",
                                  count_spending=a.get("type") in SPENDING_TYPES)
                bal = a.get("balance") or {}
                acc.bank_name = a.get("name", acc.bank_name or acc.name)
                acc.name = acc.nickname or acc.bank_name
                acc.meta = json.dumps(a.get("meta") or {})
                acc.institution = (a.get("connection") or {}).get("name", "")
                acc.type = a.get("type", "")
                acc.balance_current = bal.get("current") or 0
                acc.balance_available = bal.get("available")
                acc.balance_limit = bal.get("limit")
                acc.updated_at = datetime.now(timezone.utc)
                acc.bank_status = a.get("status", "") or ""
                fresh = a.get("refreshed") or {}
                acc.refreshed_txns = fresh.get("transactions") or acc.refreshed_txns or ""
                acc.refreshed_balance = fresh.get("balance") or acc.refreshed_balance or ""
                s.add(acc)
            s.commit()
            progress.detail(f"{len(raw_accounts)} account{'s' if len(raw_accounts) != 1 else ''} and balances updated")

            latest = s.exec(select(Transaction).order_by(Transaction.date.desc())).first()
            since = (datetime.combine(latest.date, datetime.min.time()) - timedelta(days=OVERLAP_DAYS)
                     if latest else datetime.now() - timedelta(days=FIRST_SYNC_DAYS))
            progress.step(f"Fetching transactions since {since.strftime('%-d %b')}")
            raw = _get_all(client, "/transactions",
                           {"start": since.replace(tzinfo=TZ).astimezone(timezone.utc).isoformat()},
                           label="transactions fetched")
            progress.detail(f"{len(raw)} transactions fetched")

            progress.step("Adding and categorising new transactions")
            engine_ = Categoriser(s)
            added: list[Transaction] = []
            # Akahu lists newest first. Oldest-first with a running number keeps the bank's order within a day.
            top = s.exec(select(Transaction.seq).where(Transaction.seq != None)  # noqa: E711
                         .order_by(Transaction.seq.desc())).first()
            seq = max(top or 0, 0)
            for _, t in sorted(enumerate(raw), key=lambda it: (it[1]["date"], -it[0])):
                if s.get(Transaction, t["_id"]):
                    continue
                seq += 1
                cat = t.get("category") or {}
                txn = Transaction(
                    id=t["_id"], account_id=t["_account"], date=_local_date(t["date"]),
                    description=t.get("description", ""),
                    merchant=(t.get("merchant") or {}).get("name", ""),
                    amount=float(t["amount"]), akahu_type=t.get("type", ""), seq=seq,
                    akahu_group=((cat.get("groups") or {}).get("personal_finance") or {}).get("name", ""),
                )
                engine_.apply(txn)
                s.add(txn)
                added.append(txn)
                if len(added) % 50 == 0:
                    s.commit()
                    progress.detail(f"{len(added)} added so far")
            s.commit()
            new = len(added)
            progress.detail(f"{new} new transaction{'s' if new != 1 else ''}")

            # Akahu occasionally replaces a transaction the bank changed with a new copy under a new id.
            # Remove our old copy so it isn't counted twice, carrying your note, tags and category across.
            progress.step("Checking for transactions the bank changed or removed")
            seen = {t["_id"] for t in raw}
            # Only trust the reply for accounts that are connected AND still have transactions in it.
            # An account that suddenly returns nothing is far more likely a broken bank connection than
            # every one of its transactions being withdrawn, so its history is left alone.
            answered = ({a["_id"] for a in raw_accounts if a.get("status", "ACTIVE") == "ACTIVE"}
                        & {t["_account"] for t in raw})
            window = s.exec(select(Transaction).where(Transaction.date > since.date() + timedelta(days=1))).all()
            missing = [t for t in window if t.id not in seen]
            stale = [t for t in missing if t.account_id in answered]
            if len(stale) > max(5, len(window) * 0.3):
                stale = []  # too many to be routine corrections
            if stale:
                moved = _replace_stale(s, stale, added)
                progress.detail(f"{len(stale)} replaced or removed" + (f", {moved} kept your notes or category" if moved else ""))
            elif missing:
                progress.detail(f"{len(missing)} recent transactions weren't in Akahu's reply. Kept them: "
                                "it looks like a bank connection problem, not corrections")
            else:
                progress.detail("Nothing changed")

            progress.step("Loading pending transactions")
            try:
                pending = _get_all(client, "/transactions/pending")
                s.exec(delete(PendingTransaction))
                for p in pending:
                    s.add(PendingTransaction(account_id=p.get("_account", ""), date=_local_date(p["date"]),
                                             description=p.get("description", ""),
                                             amount=float(p.get("amount", 0)), type=p.get("type", "")))
                s.commit()
                progress.detail(f"{len(pending)} pending")
            except httpx.HTTPError as e:
                progress.detail(f"Skipped: {e}")

            progress.step("Matching transfers and bills")
            pairs = pair_transfers(s)
            progress.detail(f"{pairs} new transfer pair{'s' if pairs != 1 else ''}")

            from .insights import needs_review_query  # local import: insights imports this package's db only
            waiting = len(s.exec(needs_review_query()).all())
            msg = (f"Finished: {new} new transaction{'s' if new != 1 else ''}" + (f", {pairs} transfers" if pairs else "")
                   + (f", {waiting} to review." if waiting else "."))
            set_setting(s, "last_sync", datetime.now(TZ).strftime("%a %d %b %H:%M"))
            set_setting(s, "last_sync_at", datetime.now(timezone.utc).isoformat())
            set_setting(s, "last_sync_status", msg)
            progress.finish(True, msg)
            set_setting(s, "last_sync_log", json.dumps(progress.snapshot()))
            return msg
    except Exception as e:  # surface the failure rather than crashing the scheduler
        msg = f"Sync failed. {_explain(e)}"
        progress.finish(False, msg)
        with Session(current_engine()) as s:
            set_setting(s, "last_sync_status", msg)
            set_setting(s, "last_sync_log", json.dumps(progress.snapshot()))
        return msg
    finally:
        lock.release()
