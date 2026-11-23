"""Every test run gets its own empty data folder, set up before the app is imported."""
import os
import sys
import tempfile
from pathlib import Path

_DATA = tempfile.mkdtemp(prefix="payday-test-")
os.environ["DATA_DIR"] = _DATA
os.environ["DATABASE_URL"] = f"sqlite:///{_DATA}/budget.db"
os.environ.pop("PUBLIC_URL", None)
os.environ.pop("SMTP_HOST", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clear_limits():
    from app import main
    main._failures.clear()
    yield
    main._failures.clear()
