import os
import sys
import tempfile
from pathlib import Path

# Point Payday at a throwaway data folder before any app module is imported.
_tmp = tempfile.mkdtemp(prefix="payday-tests-")
os.environ.setdefault("DATA_DIR", _tmp)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/budget.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from sqlmodel import Session  # noqa: E402


@pytest.fixture
def session(tmp_path):
    """A fresh, empty household store."""
    from app.db import _make_engine, init_profile_db, set_setting
    eng = _make_engine(tmp_path / "household.db")
    init_profile_db(eng)
    with Session(eng) as s:
        set_setting(s, "pay_frequency", "monthly")
        set_setting(s, "payday_day", "15")
        set_setting(s, "payday_adjust", "none")
        yield s
    eng.dispose()
