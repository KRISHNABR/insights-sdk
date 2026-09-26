"""Test harness.

The SDK is its own repository, so these tests run against fixture registries in
tests/fixtures rather than the platform's real catalog. That is deliberate twice over:
the SDK must not require the platform repo to be checked out beside it, and the tests
then also prove the SDK is not coupled to one particular catalog.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from insights_sdk import config, identity, telemetry

FIXTURES = Path(__file__).parent / "fixtures"
EDGE_TOKEN = "test-edge-token"


def _seed(db: Path) -> None:
    conn = sqlite3.connect(db)
    with conn:
        conn.executescript(
            """
            CREATE TABLE hr_headcount    (month TEXT, dept TEXT, headcount INTEGER);
            CREATE TABLE hr_headcount_v2 (month TEXT, dept TEXT, headcount INTEGER);
            CREATE TABLE hr_compensation (employee_id INTEGER, employee_name TEXT,
                                          dept TEXT, base_salary INTEGER, bonus_pct REAL);
            CREATE TABLE sales_pipeline  (region TEXT, account TEXT, value INTEGER);
            """
        )
        conn.executemany("INSERT INTO hr_headcount VALUES (?,?,?)",
                         [("2026-09", "Engineering", 184), ("2026-09", "Sales", 96)])
        # The prod table holds DIFFERENT data, so a test that reads it cannot pass by accident.
        conn.executemany("INSERT INTO hr_headcount_v2 VALUES (?,?,?)",
                         [("2026-09", "Engineering", 999)])
        conn.executemany("INSERT INTO hr_compensation VALUES (?,?,?,?,?)",
                         [(1, "Dana Okafor", "People Ops", 94000, 8.0),
                          (2, "Sam Whitfield", "People Analytics", 112000, 12.0)])
        conn.executemany("INSERT INTO sales_pipeline VALUES (?,?,?)",
                         [("EMEA", "Acme Corp", 240000)])
    conn.close()


@pytest.fixture
def platform(tmp_path, monkeypatch):
    """A running platform: a seeded warehouse, the registry, and an edge token."""
    db = tmp_path / "warehouse.db"
    _seed(db)
    monkeypatch.setenv("INSIGHTS_WAREHOUSE_DSN", str(db))
    monkeypatch.setenv("INSIGHTS_REGISTRY_DIR", str(FIXTURES))
    monkeypatch.setenv("INSIGHTS_EDGE_TOKEN", EDGE_TOKEN)
    monkeypatch.setenv("INSIGHTS_ENV", "local")
    config.reset()
    telemetry.clear_sensitive_fields()
    yield tmp_path
    config.reset()
    telemetry.clear_sensitive_fields()


@pytest.fixture
def as_app(monkeypatch):
    """Run as a given tenant app: point the SDK at that app's manifest."""

    def _use(fixture_name: str):
        monkeypatch.setenv("INSIGHTS_APP_MANIFEST", str(FIXTURES / fixture_name))
        config.reset()

    return _use


def edge_headers(subject: str, groups: str = "") -> dict[str, str]:
    """Exactly what the platform edge injects after stripping the client's own."""
    return {
        "X-Auth-User": subject,
        "X-Auth-Groups": groups,
        "X-Auth-Request-Id": "req-test",
        "X-Auth-Edge-Token": EDGE_TOKEN,
    }


def signed_in(subject: str, groups: str = ""):
    """Context manager: a request arriving through the edge as this user."""
    return identity.as_caller(identity.from_headers(edge_headers(subject, groups)))
