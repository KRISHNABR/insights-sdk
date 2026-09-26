"""Connectors: the platform ships the plumbing, the team owns the connection.

WHAT CHANGED, AND WHY
---------------------
An earlier design brokered DATA. The platform held a catalog of datasets, resolved a
nickname to a physical table, checked an entitlement, applied masking and audited the
read. It was coherent, and it was too much: it put the platform team in the middle of
every team's data governance, maintaining a second opinion beside the data platform's
own - and nobody had asked us to.

Teams already have access to their data. What they do not have is a good way to
CONNECT: a pooled, retried, TLS-correct client per engine, a credential that is not
in their repo, and an error message that says what is actually wrong. That is a
tractable job for three people, and it is this module.

THE LINE
--------
    ours     the driver, pooling, timeouts, retries, TLS, error translation,
             the secret binding, and telemetry ABOUT THE CONNECTION
    theirs   which system, which database, which query, and every row that
             comes back - we never see it and never record it

Read `_translate` before anything else. Turning a driver's stack trace into "which
connection, what is wrong, who fixes it" is the entire value this module adds, and
the rest is a thin wrapper around somebody else's client.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import secrets, telemetry
from .errors import InsightsError


class ConnectionFailed(InsightsError):
    """A connection could not be established or a query could not run.

    Deliberately one class with a `kind`, not a tree of six. A tenant handles these
    the same way whatever went wrong - log it and fail the run - and the useful
    detail is in the message, not in the type they would have to import to catch.
    """

    def __init__(self, message: str, *, connection: str, kind: str):
        super().__init__(message)
        self.connection = connection
        self.kind = kind


def _expand(value: Any, connection: str) -> Any:
    """Substitute ${VAR} from the environment in a connection option.

    How one manifest describes a connection whose host differs per environment,
    without the file carrying three copies or the platform inventing a templating
    language. Done at CONNECT time, not load time: reading a manifest must not
    require the environment the app will run in.

    Only ever a host, path or URL - never a credential. A credential here would be an
    environment variable, readable by anything that can see the process. Those go
    through `secret:` and are fetched at the point of use.
    """
    if not isinstance(value, str) or "${" not in value:
        return value

    def swap(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in os.environ:
            raise ConnectionFailed(
                f"connection '{connection}' refers to ${{{key}}}, which is not set. "
                f"The platform injects INSIGHTS_* at deploy; export it yourself to run "
                f"locally.",
                connection=connection,
                kind="config_missing",
            )
        return os.environ[key]

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", swap, value)


class Connector(Protocol):
    def query(self, sql: str, **params: Any) -> list[dict]: ...


# ---------------------------------------------------------------------------
# Error translation - the part that earns the wrapper
# ---------------------------------------------------------------------------

#: Substrings a driver puts in its exception, and what they actually mean to the
#: person reading the failure. Matching on text is crude, and it is what is available:
#: every driver spells these differently and none of them expose a stable code.
_SIGNATURES = (
    ("auth",       ("authentication", "unauthorized", "401", "invalid credentials",
                    "access denied", "permission denied", "403")),
    ("network",    ("connection refused", "could not connect", "name or service not known",
                    "temporary failure in name resolution", "no route to host", "econnrefused",
                    "unable to open database file", "could not translate host name")),
    ("timeout",    ("timed out", "timeout", "etimedout")),
    ("tls",        ("certificate", "ssl", "tls handshake")),
    ("not_found",  ("does not exist", "no such table", "not found", "404",
                    "unknown database", "undefined table")),
    ("syntax",     ("syntax error", "parse error", "near \"")),
)

#: What to tell someone for each kind. The rule for every line: name the connection,
#: say what to check, and say WHO fixes it - because on a multi-tenant platform the
#: answer is usually not the platform team, and a message that does not say so sends
#: a ticket to the wrong place.
_ADVICE = {
    "auth": ("the credential was rejected",
             "Check the secret's value and that it has not expired or been rotated. "
             "Your team owns it - the platform stores the slot and cannot read it."),
    "network": ("could not reach the host",
                "Check the host and port in app.yaml. If they are right, this is "
                "usually a firewall or VPC route - that one IS ours, so tell us."),
    "timeout": ("the connection or query timed out",
                "A long query or a busy server. Raise `timeout` on the connection in "
                "app.yaml if the query is genuinely slow."),
    "tls": ("the TLS handshake failed",
            "Usually an internal CA the image does not trust. That is ours - tell us "
            "the host and we will add it to the base image."),
    "not_found": ("the object does not exist",
                  "Check the database, schema and table names. The platform does not "
                  "hold a catalog - what exists is whatever your connection can see."),
    "syntax": ("the query is not valid for this engine",
               "SQL dialects differ. The platform does not rewrite your SQL."),
    "unknown": ("the driver raised an error we do not recognise",
                "The original message is below. If it is a connection problem rather "
                "than a query problem, tell the platform team."),
}


def _translate(exc: Exception, *, connection: str, engine: str) -> ConnectionFailed:
    text = str(exc).lower()
    kind = next((k for k, needles in _SIGNATURES if any(n in text for n in needles)), "unknown")
    summary, advice = _ADVICE[kind]
    return ConnectionFailed(
        f"connection '{connection}' ({engine}): {summary}.\n"
        f"  {advice}\n"
        f"  driver said: {exc}",
        connection=connection,
        kind=kind,
    )


# ---------------------------------------------------------------------------
# The connectors
# ---------------------------------------------------------------------------

@dataclass
class _Base:
    name: str
    engine: str
    config: dict
    secret: secrets.Secret | None

    def _observe(self, sql: str, started: float, rows: int) -> None:
        """Record that a query happened - never what it returned or asked for.

        No SQL text and no rows. SQL carries column and table names, and often a
        literal in a WHERE clause; a platform-wide log of tenant SQL is a data
        inventory nobody consented to. Shape only: which connection, how long, how
        many rows.
        """
        telemetry.get_logger().info(
            "query_executed",
            connection=self.name,
            engine=self.engine,
            ms=int((time.monotonic() - started) * 1000),
            rows=rows,
        )


class SqlConnector(_Base):
    """Any DB-API/JDBC-shaped engine: Databricks SQL, Redshift, Postgres, SQLite.

    One class rather than four, because the differences that matter to a caller are
    all in the DSN and the driver import. Where they differ in ways a tenant would
    feel - parameter style, for one - that is a per-engine detail below, not a
    per-engine class.
    """

    def query(self, sql: str, **params: Any) -> list[dict]:
        started = time.monotonic()
        try:
            rows = self._execute(sql, params)
        except ConnectionFailed:
            raise
        except Exception as exc:                      # noqa: BLE001 - translated below
            error = _translate(exc, connection=self.name, engine=self.engine)
            # The KIND, never the driver's message. A driver puts the host, the user
            # and sometimes a query fragment in its error text, and this record goes
            # to a platform-wide sink. The tenant sees the full text in the raised
            # exception; the platform sees which of six things went wrong.
            telemetry.get_logger().warn(
                "connection_failed", connection=self.name, engine=self.engine, kind=error.kind,
            )
            raise error from exc
        self._observe(sql, started, len(rows))
        return rows

    def _execute(self, sql: str, params: dict) -> list[dict]:
        if self.engine == "sqlite":                   # the local stand-in
            import sqlite3

            connection = sqlite3.connect(self.config["path"])
            try:
                connection.row_factory = sqlite3.Row
                cursor = connection.execute(sql, params)
                return [dict(row) for row in cursor.fetchall()]
            finally:
                connection.close()

        # Databricks SQL / Redshift / Postgres all take a real driver. They are not
        # vendored here - the base image carries them - so this stays a seam rather
        # than an import that would fail locally for everyone.
        raise ConnectionFailed(
            f"connection '{self.name}' uses engine '{self.engine}', whose driver is not "
            f"installed in this environment. It ships in the `python-data` base image; "
            f"locally, use the `sqlite` engine or point INSIGHTS_SECRET_* at a sandbox.",
            connection=self.name,
            kind="driver_missing",
        )


class RestConnector(_Base):
    """A REST data source. `query` takes a path, not SQL - same contract, different verb."""

    def query(self, path: str, **params: Any) -> list[dict]:
        import httpx

        started = time.monotonic()
        headers = {}
        if self.secret is not None:
            # The one place a secret becomes a string. It goes straight into a header
            # and is never stored, logged, or attached to the exception below.
            headers["Authorization"] = f"Bearer {self.secret.reveal()}"
        url = self.config["base_url"].rstrip("/") + "/" + path.lstrip("/")
        try:
            response = httpx.get(url, params=params, headers=headers,
                                 timeout=self.config.get("timeout", 20))
            response.raise_for_status()
            body = response.json()
        except Exception as exc:                      # noqa: BLE001
            error = _translate(exc, connection=self.name, engine=self.engine)
            telemetry.get_logger().warn(
                "connection_failed", connection=self.name, engine=self.engine, kind=error.kind,
            )
            raise error from exc
        rows = body if isinstance(body, list) else body.get("items", [])
        self._observe(path, started, len(rows))
        return rows


_ENGINES = {
    "sqlite": SqlConnector,
    "databricks-sql": SqlConnector,
    "redshift": SqlConnector,
    "postgres": SqlConnector,
    "rest": RestConnector,
}

SUPPORTED_ENGINES = tuple(sorted(_ENGINES))


def connect(name: str, *, manifest=None) -> Connector:
    """Open the connection this app declared under `name`.

    Everything comes from the manifest and the secret store: nothing about the target
    system is passed in at the call site. That is what makes the same line of code
    work in local, dev and prod - and it is why a tenant cannot quietly point a
    production app at something nobody registered.
    """
    from . import config

    manifest = manifest or config.manifest()
    declared = {c.name: c for c in manifest.connections}
    if name not in declared:
        known = ", ".join(sorted(declared)) or "none"
        raise ConnectionFailed(
            f"this app has no connection called '{name}'. Declared: {known}. "
            f"Add it under `connections:` in app.yaml.",
            connection=name,
            kind="not_declared",
        )

    spec = declared[name]

    # A relative path in a connection resolves against the MANIFEST, not the process
    # working directory. A web app's uvicorn runs in <repo>/src and a job runs in
    # <repo>, so a cwd-relative path works for one and not the other - and which one
    # you hit depends on how the app was started, which is the worst kind of bug.
    options = {k: _expand(v, name) for k, v in spec.options.items()}
    if "path" in options and not Path(str(options["path"])).is_absolute():
        options["path"] = str((manifest.path.parent / str(options["path"])).resolve())

    secret = secrets.resolve(manifest.app, spec.secret) if spec.secret else None
    factory = _ENGINES[spec.engine]
    telemetry.get_logger().info("connection_opened", connection=name, engine=spec.engine)
    return factory(name=name, engine=spec.engine, config=options, secret=secret)
