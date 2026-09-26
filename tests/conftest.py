"""Test harness.

The SDK is its own repository, so these tests run against fixtures in tests/fixtures
rather than anything in the platform repo. Deliberate twice over: the SDK must not
require the platform to be checked out beside it, and the tests then also prove the
SDK is not coupled to one particular deployment.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from insights_sdk import config, identity, telemetry

FIXTURES = Path(__file__).parent / "fixtures"
EDGE_TOKEN = "test-edge-token"


def _seed(db: Path) -> None:
    """A team's own warehouse. The platform has no opinion about what is in it - this
    exists so a connector has something real to connect to."""
    conn = sqlite3.connect(db)
    with conn:
        conn.executescript(
            """
            CREATE TABLE hr_headcount    (month TEXT, dept TEXT, headcount INTEGER);
            CREATE TABLE hr_compensation (employee_id INTEGER, employee_name TEXT,
                                          dept TEXT, base_salary INTEGER, bonus_pct REAL);
            """
        )
        conn.executemany("INSERT INTO hr_headcount VALUES (?,?,?)",
                         [("2026-09", "Engineering", 184), ("2026-09", "Sales", 96)])
        conn.executemany("INSERT INTO hr_compensation VALUES (?,?,?,?,?)",
                         [(1, "Krishna Murari", "People Ops", 94000, 8.0),
                          (2, "Vidya Raman", "People Analytics", 112000, 12.0)])
    conn.close()


@pytest.fixture
def platform(tmp_path, monkeypatch):
    """A team's environment: their warehouse, their secret store, and an edge token."""
    db = tmp_path / "warehouse.db"
    _seed(db)

    store = tmp_path / "secret-store" / "demo"
    store.mkdir(parents=True)
    (store / "warehouse-token").write_text("not-a-real-token")

    monkeypatch.setenv("INSIGHTS_WAREHOUSE_PATH", str(db))
    monkeypatch.setenv("INSIGHTS_SECRET_DIR", str(tmp_path / "secret-store"))
    monkeypatch.setenv("INSIGHTS_EDGE_TOKEN", EDGE_TOKEN)
    monkeypatch.setenv("INSIGHTS_ENV", "local")
    config.reset()
    yield tmp_path
    config.reset()


@pytest.fixture
def as_app(monkeypatch):
    """Point the SDK at one of the fixture manifests."""
    def use(fixture_name: str):
        monkeypatch.setenv("INSIGHTS_APP_MANIFEST", str(FIXTURES / fixture_name))
        config.reset()
    return use


@contextmanager
def signed_in(subject: str, groups: str = ""):
    """A caller the edge vouched for. Built the same way `from_headers` builds one, so
    a test cannot accidentally construct a trusted caller the edge could not."""
    caller = identity.Caller(
        subject=subject,
        request_id="req-test",
        trusted=True,
        _groups=tuple(g for g in groups.split(",") if g),
    )
    with identity.as_caller(caller):
        yield caller


def edge_headers(subject: str, groups: str = "") -> dict[str, str]:
    """Exactly what the platform edge injects after stripping the client's own."""
    return {
        "X-Auth-User": subject,
        "X-Auth-Groups": groups,
        "X-Auth-Request-Id": "req-test",
        "X-Auth-Edge-Token": EDGE_TOKEN,
    }
