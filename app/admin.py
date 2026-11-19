"""Command-line tools for the person running Payday.

  python -m app.admin move-to-postgres /path/to/old/data   copy a SQLite install into the PostgreSQL in DATABASE_URL
  python -m app.admin restore-site backup.zip               load a whole-site backup into an empty database
"""
import json
import sys
import zipfile
from pathlib import Path

from sqlalchemy import insert, inspect, text
from sqlmodel import Session, SQLModel, select

from . import data
from .db import (PG, PROFILE_TABLES, SITE_TABLES, Profile, _make_engine, create_store, fix_sequences, init_db,
                 init_profile_db, new_profile_file, site_engine, store_engine)


def _rows(eng, table) -> list[dict]:
    if not inspect(eng).has_table(table.name):
        return []
    have = {c["name"] for c in inspect(eng).get_columns(table.name)}
    with eng.connect() as conn:
        return [{k: v for k, v in dict(r._mapping).items() if k in have}
                for r in conn.execute(text(f'SELECT * FROM "{table.name}"'))]


def _empty() -> bool:
    with Session(site_engine) as s:
        return s.exec(select(Profile)).first() is None


def load(site_tables: dict, households: dict) -> None:
    """site_tables: {table: rows}; households: {old profile id: {table: rows}}."""
    SQLModel.metadata.create_all(site_engine, tables=SITE_TABLES)
    with site_engine.begin() as conn:
        for t in SITE_TABLES:
            model, cols = data.MODELS[t.name], set(t.columns.keys())
            rows = [data.utc(model.model_validate({k: v for k, v in r.items() if k in cols}).model_dump(include=cols))
                    for r in site_tables.get(t.name) or []]
            if rows and t.name != "heldupload":
                conn.execute(insert(t), rows)
    fix_sequences(site_engine)
    with Session(site_engine) as s:
        for prof in s.exec(select(Profile)):
            name = new_profile_file()
            create_store(name)
            eng = store_engine(name)
            SQLModel.metadata.create_all(eng, tables=PROFILE_TABLES)
            data.fill(eng, households.get(prof.id, {}))
            init_profile_db(eng)
            prof.db_file = name
            s.add(prof)
            print(f"  {prof.name}: {sum(len(v) for v in households.get(prof.id, {}).values())} rows")
        s.commit()


def move_to_postgres(old_dir: str) -> None:
    if not PG:
        sys.exit("Set DATABASE_URL to the PostgreSQL database first.")
    init_db()
    if not _empty():
        sys.exit("The PostgreSQL database already has profiles in it. Use an empty database.")
    old = Path(old_dir)
    site = _make_engine(old / "site.db")
    site_tables = {t.name: _rows(site, t) for t in SITE_TABLES}
    households = {}
    for p in site_tables.get("profile", []):
        f = Path(p["db_file"])
        eng = _make_engine(f if f.is_absolute() else old / f)
        households[p["id"]] = {t.name: _rows(eng, t) for t in PROFILE_TABLES}
        eng.dispose()
    print(f"Copying {len(site_tables.get('user', []))} people and {len(households)} households…")
    load(site_tables, households)
    print("Done. Copy payday.key from the old data folder too (or set PAYDAY_KEY), or saved bank connections and 2FA stop working.")


def restore_site(zip_path: str) -> None:
    init_db()
    if not _empty():
        sys.exit("This database already has profiles in it. Restore into an empty database.")
    with zipfile.ZipFile(zip_path) as z:
        site = json.loads(z.read("site.json"))["tables"]
        households = {}
        for name in z.namelist():
            if name.startswith("profiles/") and name.endswith(".json"):
                households[int(name.split("/")[1].split("-")[0])] = json.loads(z.read(name))["tables"]
        key = z.read("payday.key") if "payday.key" in z.namelist() else None
    load(site, households)
    if key:
        print("Put the payday.key from the backup in the data folder (or into PAYDAY_KEY) before starting Payday.")
    print("Done.")


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd == ["move-to-postgres"] and len(sys.argv) == 3:
        move_to_postgres(sys.argv[2])
    elif cmd == ["restore-site"] and len(sys.argv) == 3:
        restore_site(sys.argv[2])
    else:
        print(__doc__)
