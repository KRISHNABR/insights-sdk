"""Evidence for ADR-002: a connection is DECLARED, never improvised.

These tests assert something about the SHAPE of the SDK rather than its behaviour,
because the guarantee that matters is not "connect() checks the manifest", it is
"there is no other way to open a connection".

The file used to assert the opposite - that the SDK exported no connection primitive
at all, because the platform brokered every read. When teams took ownership of their
connections that became false, and this test failed, which is exactly what it was for.
What survives the change is the narrower and more durable rule: **the manifest is the
only place a connection can come from.** A tenant cannot pass a host, a DSN or a
credential at the call site, so what an app can reach is reviewable in git rather than
buried in a function three directories down.
"""

import pytest

import insights_sdk
from insights_sdk import broker


def test_the_sdk_exports_no_raw_connection_primitive():
    """`connect` is fine - it takes a NAME. These take a target or hand back a driver."""
    forbidden = {"get_connection", "cursor", "session", "dsn", "engine", "create_engine"}
    exported = {name.lower() for name in dir(insights_sdk) if not name.startswith("_")}
    assert not (forbidden & exported), f"raw connection primitive exported: {forbidden & exported}"


def test_connect_takes_a_name_and_nothing_that_could_be_a_target():
    """The signature IS the control. The moment `connect(host=..., password=...)` is
    possible, what an app can reach stops being reviewable in git."""
    import inspect

    parameters = inspect.signature(insights_sdk.connect).parameters
    assert list(parameters) == ["name", "manifest"], list(parameters)
    assert parameters["manifest"].kind is inspect.Parameter.KEYWORD_ONLY
    # `manifest` exists for tests and the CLI; it is not a credential channel.
    for banned in ("host", "password", "token", "dsn", "url", "secret", "credential"):
        assert banned not in parameters


def test_a_connection_not_in_the_manifest_cannot_be_opened(tmp_path, monkeypatch):
    from insights_sdk import config
    from insights_sdk.connectors import ConnectionFailed

    (tmp_path / "app.yaml").write_text("""
app: locked
team: demo-team
kind: job
access:
  manage:
    owners: [MG-DEMO]
runtime:
  sdk: ">=0.1,<1"
  base: python-data
connections:
  - name: declared-one
    engine: sqlite
    path: /tmp/x.db
job:
  schedule: "0 6 * * MON"
""")
    monkeypatch.setenv("INSIGHTS_APP_MANIFEST", str(tmp_path / "app.yaml"))
    config.reset()
    try:
        with pytest.raises(ConnectionFailed) as exc:
            insights_sdk.connect("not-declared")
        assert exc.value.kind == "not_declared"
    finally:
        config.reset()


def test_the_broker_exposes_exactly_two_verbs():
    assert set(broker.__all__) == {"query", "fetch"}


def test_neither_verb_can_return_a_connection(platform, as_app):
    """Both return plain rows. Nothing a tenant holds afterwards can be reused to read again."""
    from conftest import signed_in

    as_app("app-web.yaml")
    with signed_in("krishna@corp.example", "MG-PEOPLE-OPS"):
        rows = broker.query("hr.headcount", "SELECT dept FROM hr.headcount")

    assert isinstance(rows, list)
    assert all(isinstance(row, dict) for row in rows)
