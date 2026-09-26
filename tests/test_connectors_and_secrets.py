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

from conftest import signed_in


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
connections:
  - name: team-warehouse
    engine: sqlite
    path: {db}
    secret: warehouse-token
job:
  schedule: "0 6 * * MON"
""")
    (tmp_path / ".env").write_text("warehouse-token=s3cr3t-value\n")
    monkeypatch.setenv("INSIGHTS_APP_MANIFEST", str(tmp_path / "app.yaml"))
    config.reset()
    yield tmp_path
    config.reset()


def test_a_team_reads_their_own_data_through_a_declared_connection(app):
    with signed_in("sp-demo"):
        rows = connect("team-warehouse").query("SELECT name, amount FROM salaries")
    assert rows == [{"name": "vidya", "amount": 112000}]


def test_no_connection_without_a_caller_the_platform_vouched_for(app):
    """The credential belongs to the APP, not to whoever is asking. Outside the edge
    there is no caller, so there is no connection either."""
    from insights_sdk.errors import IdentityError

    with pytest.raises(IdentityError):
        connect("team-warehouse")


def test_a_connection_that_was_not_declared_is_refused(app):
    with signed_in("sp-demo"), pytest.raises(ConnectionFailed) as exc:
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


def test_a_missing_secret_says_where_to_put_it(app):
    """The error has one job: tell you the file and whose value it is."""
    (app / ".env").unlink()
    with pytest.raises(secrets.SecretError) as exc:
        secrets.resolve("demo", "warehouse-token")
    message = str(exc.value)
    assert ".env" in message
    assert "gitignored" in message
    assert "cannot read" in message, "must say the platform team cannot read it"


def test_dotenv_is_refused_outside_local(app, monkeypatch):
    """A .env that escapes into an image must not quietly become the source of a
    production credential. Enforced, not documented - "remember not to ship a .env"
    is not a control."""
    monkeypatch.setenv("INSIGHTS_ENV", "prod")
    with pytest.raises(secrets.SecretError) as exc:
        secrets.resolve("demo", "warehouse-token")
    message = str(exc.value)
    assert ".env" not in message
    assert "INSIGHTS_SECRET_WAREHOUSE_TOKEN" in message, "must name the injected variable"
    assert "insights/demo/warehouse-token" in message


def test_an_injected_variable_wins_over_dotenv(app, monkeypatch):
    """How production delivers a secret, and how a developer points at a sandbox
    credential without editing a file."""
    monkeypatch.setenv("INSIGHTS_SECRET_WAREHOUSE_TOKEN", "from-the-environment")
    assert secrets.resolve("demo", "warehouse-token").reveal() == "from-the-environment"


def test_secrets_are_scoped_per_app(app):
    """A flat namespace would let any app's identity read any other app's credential,
    and the isolation story would be a comment rather than a boundary."""
    assert secrets.path_for("comp-report", "tok") == "insights/comp-report/tok"
    assert secrets.path_for("other-app", "tok") != secrets.path_for("comp-report", "tok")


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


def test_probe_does_a_real_round_trip(app):
    """`connect()` only builds the object and resolves the credential - for REST there
    is no socket until a request is made. A probe that skips the round trip reports
    "connected" for a host that does not exist, which is worse than no probe."""
    import inspect

    from insights_sdk.connectors import RestConnector, SqlConnector, _Base

    # the base refuses to answer: every engine must implement it
    with pytest.raises(NotImplementedError):
        _Base.probe(object())

    assert "SELECT 1" in inspect.getsource(SqlConnector.probe)
    assert "httpx" in inspect.getsource(RestConnector.probe)


def test_a_probe_against_a_dead_host_fails(platform, as_app, monkeypatch):
    """The point of the round trip: an unreachable host must fail the probe, not pass
    it. `app-job.yaml` resolves its path from ${INSIGHTS_WAREHOUSE_PATH}, so pointing
    that somewhere dead is the whole test."""
    from insights_sdk import ConnectionFailed, connect

    as_app("app-job.yaml")
    monkeypatch.setenv("INSIGHTS_WAREHOUSE_PATH", "/nope/missing.db")
    with signed_in("sp-demo"):
        with pytest.raises(ConnectionFailed) as exc:
            connect("team-warehouse").probe()
    assert exc.value.kind == "network"


@pytest.mark.parametrize("url, expected", [
    ("http://127.0.0.1:8081", True),
    ("http://localhost:8081/people", True),
    ("http://[::1]:9000", True),
    ("https://directory.internal.bms.com", False),
    ("https://adb-123.azuredatabricks.net/sql", False),
])
def test_loopback_is_recognised(url, expected):
    """A corporate HTTP_PROXY bypass list often has `localhost` but not `127.0.0.1`,
    so half the platform's own traffic gets handed to a proxy that correctly refuses
    to route it. Anything loopback must bypass the proxy; anything else must not,
    because a tenant's real REST connection may genuinely need it."""
    from insights_sdk.connectors import is_loopback

    assert is_loopback(url) is expected


def test_a_local_rest_connection_ignores_a_broken_http_proxy(tmp_path, monkeypatch):
    """The regression, end to end: a real local HTTP server, a dead HTTP_PROXY, and a
    REST connection that must still reach it.

    Reported from a corporate laptop: the proxy bypass list had `localhost` but not
    `127.0.0.1`, so every health check and every edge hop was handed to a proxy that
    correctly refused to route loopback. The apps were fine; everything said they
    were not.
    """
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):                                   # noqa: N802
            body = _json.dumps([{"dept": "Engineering"}]).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):                       # keep pytest output clean
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        (tmp_path / "app.yaml").write_text(f"""
app: demo
team: demo-team
kind: job
access:
  manage:
    owners: [MG-DEMO]
runtime: {{size: small}}
connections:
  - name: directory
    engine: rest
    base_url: http://127.0.0.1:{port}
job:
  schedule: "0 6 * * MON"
""")
        monkeypatch.setenv("INSIGHTS_APP_MANIFEST", str(tmp_path / "app.yaml"))
        # a proxy that nothing listens on: if the connector honours it, this fails
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        config.reset()

        with signed_in("sp-demo"):
            rows = connect("directory").query("/people")
        assert rows == [{"dept": "Engineering"}]
    finally:
        server.shutdown()
        config.reset()
