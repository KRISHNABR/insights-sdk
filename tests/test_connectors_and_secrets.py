"""Connections are declared, credentials are referenced, and neither leaks.

The model this pins: teams already have access to their data. The platform ships the
connector and holds the secret SLOT; it never holds the value and never sees a row.
"""

from __future__ import annotations

import sqlite3

import pytest

from insights_sdk import config, connect, secrets
from insights_sdk.connectors import ConnectionFailed, _translate
from insights_sdk.errors import ManifestError


@pytest.fixture
def app(tmp_path, monkeypatch):
    db = tmp_path / "team.db"
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("CREATE TABLE salaries (name TEXT, amount INTEGER)")
        conn.execute("INSERT INTO salaries VALUES ('vidya', 112000)")
    conn.close()

    (tmp_path / "app.yaml").write_text(f"""
app: demo
team: demo-team
kind: job
access:
  manage:
    owners: [MG-DEMO]
runtime:
  sdk: ">=0.1,<1"
  base: python-data
connections:
  - name: team-warehouse
    engine: sqlite
    path: {db}
    secret: warehouse-token
job:
  schedule: "0 6 * * MON"
""")
    store = tmp_path / "secrets"
    (store / "demo").mkdir(parents=True)
    (store / "demo" / "warehouse-token").write_text("s3cr3t-value\n")
    monkeypatch.setenv("INSIGHTS_SECRET_DIR", str(store))
    monkeypatch.setenv("INSIGHTS_APP_MANIFEST", str(tmp_path / "app.yaml"))
    config.reset()
    yield tmp_path
    config.reset()


def test_a_team_reads_their_own_data_through_a_declared_connection(app):
    rows = connect("team-warehouse").query("SELECT name, amount FROM salaries")
    assert rows == [{"name": "vidya", "amount": 112000}]


def test_a_connection_that_was_not_declared_is_refused(app):
    with pytest.raises(ConnectionFailed) as exc:
        connect("someone-elses-warehouse")
    assert exc.value.kind == "not_declared"
    assert "team-warehouse" in str(exc.value), "the error should list what IS declared"


def test_a_credential_in_the_manifest_is_refused(tmp_path, monkeypatch):
    """app.yaml is in git. A password here is a password in the history forever, so
    this fails the file rather than accepting the commit."""
    (tmp_path / "app.yaml").write_text("""
app: leaky
team: demo-team
kind: job
access:
  manage:
    owners: [MG-DEMO]
runtime:
  sdk: ">=0.1,<1"
  base: python-data
connections:
  - name: warehouse
    engine: postgres
    host: db.internal
    password: hunter2
job:
  schedule: "0 6 * * MON"
""")
    monkeypatch.setenv("INSIGHTS_APP_MANIFEST", str(tmp_path / "app.yaml"))
    config.reset()
    with pytest.raises(ManifestError, match="password"):
        config.manifest()
    config.reset()


def test_a_secret_never_stringifies_to_its_value(app):
    """Defence in depth behind the telemetry rules. The expensive leak is the one
    nobody wrote on purpose: an f-string, a repr in a traceback, json default=str."""
    secret = secrets.resolve("demo", "warehouse-token")
    assert secret.reveal() == "s3cr3t-value"
    for rendered in (str(secret), repr(secret), f"{secret}", format(secret)):
        assert "s3cr3t-value" not in rendered
        assert "REDACTED" in rendered


def test_a_missing_secret_says_who_sets_it(app, monkeypatch):
    monkeypatch.delenv("INSIGHTS_SECRET_WAREHOUSE_TOKEN", raising=False)
    (app / "secrets" / "demo" / "warehouse-token").unlink()
    with pytest.raises(secrets.SecretError) as exc:
        secrets.resolve("demo", "warehouse-token")
    message = str(exc.value)
    assert "your team" in message.lower()
    assert "cannot read" in message, "must say the platform team cannot read it"


def test_secrets_are_scoped_per_app(app):
    """A flat namespace would let any app's identity read any other app's credential,
    and the isolation story would be a comment rather than a boundary."""
    assert secrets.path_for("comp-report", "tok") == "insights/comp-report/tok"
    assert secrets.path_for("other-app", "tok") != secrets.path_for("comp-report", "tok")
    with pytest.raises(secrets.SecretError):
        secrets.resolve("other-app", "warehouse-token")


@pytest.mark.parametrize("driver_message, expected_kind", [
    ("FATAL: password authentication failed for user", "auth"),
    ("could not connect to server: Connection refused", "network"),
    ("canceling statement due to statement timeout", "timeout"),
    ("SSL SYSCALL error: certificate verify failed", "tls"),
    ("relation 'hr.salaries' does not exist", "not_found"),
    ("syntax error at or near 'SELCT'", "syntax"),
    ("something nobody has seen before", "unknown"),
])
def test_driver_errors_become_actionable(driver_message, expected_kind):
    """The whole value of wrapping somebody else's client. Every driver spells these
    differently and none expose a stable code, so the mapping is text - crude, and
    what is available."""
    error = _translate(Exception(driver_message), connection="team-warehouse", engine="postgres")
    assert error.kind == expected_kind
    assert "team-warehouse" in str(error), "must name the connection"
    assert driver_message in str(error), "keep the driver's own words too"
