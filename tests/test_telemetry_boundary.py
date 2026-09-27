"""Telemetry structurally cannot carry tenant data.

The platform ships connectors and never sees a row, so the only way tenant data could
reach a platform-wide sink is if an app logged it - by accident, in a debug line
somebody left in. These rules make that raise at the point of writing rather than get
scrubbed later at the sink, because scrubbing at the sink fails open: a pattern nobody
anticipated goes straight through.
"""

import pytest

from insights_sdk import telemetry
from insights_sdk.errors import RedactionError


def test_a_log_record_cannot_carry_rows(platform):
    log = telemetry.get_logger()
    rows = [{"employee_name": "Krishna Murari", "base_salary": 94000}]
    with pytest.raises(RedactionError, match="scalars"):
        log.info("debug", rows=rows)


def test_a_log_record_cannot_carry_a_payload_disguised_as_a_string(platform):
    """The obvious way round the rule above: json.dumps(rows)."""
    log = telemetry.get_logger()
    with pytest.raises(RedactionError, match="payload in string form"):
        log.info("debug", blob="x" * 900)


def test_facts_about_a_run_are_fine(platform):
    """The shape of a result, never the result."""
    log = telemetry.get_logger()
    with telemetry.capture() as records:
        log.info("query_complete", connection="team-warehouse", rows=412, ms=38)
    assert records[0]["rows"] == 412


def test_a_connector_records_shape_and_never_content(platform, as_app):
    """The record a query leaves behind: which connection, how long, how many rows.

    Not the SQL. SQL carries table and column names and often a literal in a WHERE
    clause, so a platform-wide log of tenant SQL is a data inventory nobody agreed to.
    """
    as_app("app-job.yaml")
    from insights_sdk import connect

    from conftest import signed_in

    with signed_in("sp-demo"), telemetry.capture() as records:
        rows = connect("team-warehouse").query(
            "SELECT employee_name, base_salary FROM hr_compensation"
        )

    assert rows and "base_salary" in rows[0]          # the app got its data
    executed = [r for r in records if r["event"] == "query_executed"][0]
    assert executed["connection"] == "team-warehouse"
    assert executed["rows"] == 2
    blob = " ".join(f"{k}={v}" for k, v in executed.items())
    for leaked in ("SELECT", "base_salary", "Krishna", "94000"):
        assert leaked not in blob, f"{leaked!r} reached the sink"


def test_a_failed_connection_records_the_kind_not_the_drivers_message(platform, as_app):
    """A driver puts the host, the user and sometimes a query fragment in its error
    text. The tenant sees all of it in the raised exception; the platform sees which
    of six things went wrong."""
    as_app("app-job.yaml")
    import os

    from insights_sdk import ConnectionFailed, connect

    from conftest import signed_in

    os.environ["INSIGHTS_WAREHOUSE_PATH"] = "/nope/missing.db"
    try:
        with signed_in("sp-demo"), telemetry.capture() as records:
            with pytest.raises(ConnectionFailed):
                connect("team-warehouse").query("SELECT 1")
    finally:
        os.environ.pop("INSIGHTS_WAREHOUSE_PATH", None)

    failed = [r for r in records if r["event"] == "connection_failed"][0]
    assert failed["kind"] == "network"
    assert "missing.db" not in " ".join(str(v) for v in failed.values())


def test_every_stream_the_cli_offers_has_a_writer():
    """A stream nobody writes is worse than no stream at all.

    The `audit` stream was the data broker's: what was read, with the dataset, its
    classification and its owner. The broker was removed (ADR-002) and its writer went
    with it, but the stream stayed in `insights logs --stream` for a while. An operator
    following the RUNBOOK during a suspected data incident ran it and was told
    "no telemetry. Run an app" - which reads as "nothing happened" rather than
    "nothing records this", on the one path where that distinction matters most.

    So the CLI now takes its choices from `telemetry.STREAMS`, and this pins that every
    name in it is actually produced by some module in the SDK.
    """
    import ast
    from pathlib import Path

    src = Path(telemetry.__file__).parent
    written = set()
    for module in src.rglob("*.py"):
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            # _base_record("<stream>", level, event) - the only way a record is made
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_base_record":
                if node.args and isinstance(node.args[0], ast.Constant):
                    written.add(node.args[0].value)

    assert written, "found no _base_record call - has the record constructor been renamed?"
    for stream in telemetry.STREAMS:
        assert stream in written, (
            f"`insights logs --stream {stream}` is offered, but nothing in the SDK "
            f"writes to it. Streams with a writer: {sorted(written)}"
        )


def test_the_cli_cannot_offer_a_stream_the_sdk_does_not_define():
    """The CLI's --stream choices are derived, not typed a second time."""
    from insights_sdk.cli import main as cli

    parser = cli._build_parser() if hasattr(cli, "_build_parser") else None
    if parser is None:
        import inspect
        source = inspect.getsource(cli)
        assert '"--stream", default="all", choices=("all", *telemetry.STREAMS)' in source, (
            "`insights logs --stream` should take its choices from telemetry.STREAMS, "
            "not from a hand-written tuple that can drift from the writers."
        )
