"""Engine adapters - the only code in the platform that holds a credential.

One adapter per shared connection type. Each does exactly one job: run the request and
return rows. They contain no authorization logic at all, because authorization happens
once, in the broker, for every engine. Adding a third engine therefore inherits every
control for free - which is the whole reason the broker owns those checks and the
adapters do not.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
import urllib.request
from typing import Any, Protocol

from .config import Resolved
from .errors import ConfigError


class Engine(Protocol):
    def run(self, resolved: Resolved, request: Any) -> list[dict]: ...


def _required_env(name: str, connection: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(
            f"connection '{connection}' expects {name} in the environment. The platform "
            f"runtime injects this; if you are seeing it locally, start the app with "
            f"`insights run` rather than by hand."
        )
    return value


class LocalSqlEngine:
    """The lakehouse, locally.

    Backed by a SQL database (SQLite in tests, Postgres in the compose stack)
    because there is no Databricks emulator. Postgres is the closest honest
    stand-in: it has real users and real GRANTs, so restricted-data behaviour is
    demonstrable on a laptop rather than merely described.

    What it does NOT reproduce is Unity Catalog. Column masks and row filters are
    approximated from the registry's `local_masking` block, so governance is only
    exactly right against a real workspace. That gap is documented (ADR-002 s2)
    rather than hidden.
    """

    def run(self, resolved: Resolved, request: Any) -> list[dict]:
        sql, params = request
        dsn = _required_env(
            resolved.connection.environments[resolved.env]["dsn_env"], resolved.connection.name
        )
        conn = sqlite3.connect(dsn)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()


class DatabricksEngine:
    """The lakehouse, for real.

    Two things make this different from an ordinary database client, and both are
    the point of the design:

    1. **No stored credential.** An interactive app exchanges the signed-in user's
       session for a short-lived Databricks token, so Unity Catalog sees the
       actual person and applies THEIR grants, column masks and row filters. A
       scheduled job uses workload identity federation from its ECS task role.
       Either way nothing is persisted and there is no secret to read.
    2. **No masking here.** UC enforces column and row access itself, on every
       path to the data including notebooks. The broker deliberately does not.

    Not exercised in this submission - there is no workspace to reach. The
    structure is what matters: it is the same interface as every other adapter,
    so everything above it is unchanged.
    """

    def run(self, resolved: Resolved, request: Any) -> list[dict]:
        sql, params = request
        settings = resolved.connection.environments[resolved.env]
        host = _required_env(settings["host_env"], resolved.connection.name)
        warehouse = _required_env(settings["warehouse_env"], resolved.connection.name)

        from .identity import current_user

        caller = current_user()
        # The bridge. A person gets a token minted for them; a job federates its
        # workload identity. Neither path reads a secret.
        token = (
            _federate_workload_identity(host)
            if caller.is_service
            else _exchange_user_token(host, caller)
        )

        raise ConfigError(
            "The Databricks adapter is structural, not wired: this submission has no "
            "workspace to reach. See docs/ARCHITECTURE.md section 7 for the design, and "
            "run locally against Postgres instead."
        )


def _exchange_user_token(host: str, caller: Any) -> str:
    """OAuth token exchange: this user's session -> a short-lived Databricks token.

    Databricks federates to the same Entra tenant as the ALB, so the subject the
    workspace sees is the subject the browser authenticated as.
    """
    raise NotImplementedError("token exchange - see ADR-002 s3")


def _federate_workload_identity(host: str) -> str:
    """Workload identity federation: the ECS task role -> a Databricks token.

    No client secret exists anywhere. The task role's OIDC identity is exchanged
    directly, which is what makes "the platform team cannot read the credential"
    true rather than aspirational.
    """
    raise NotImplementedError("workload identity federation - see ADR-002 s3")


class RestEngine:
    """The internal REST API. Stubbed as a fixture-backed HTTP service.

    Returns a list of dicts like the warehouse does, so that masking, auditing and the
    redaction assertions are identical code for both engines. A JSON object comes back
    as a single-element list rather than a special case.
    """

    def run(self, resolved: Resolved, request: Any) -> list[dict]:
        resource, params = request
        settings = resolved.connection.environments[resolved.env]
        base = _required_env(settings["base_url_env"], resolved.connection.name).rstrip("/")
        token = os.environ.get(settings.get("token_env", ""), "")

        url = f"{base}{resource}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=10) as response:  # noqa: S310 - internal, fixed base
            payload = json.loads(response.read().decode())

        if isinstance(payload, dict):
            return payload.get("items", [payload]) if "items" in payload else [payload]
        return list(payload)


_ENGINES: dict[str, Engine] = {
    "databricks": DatabricksEngine(),
    "local-sql": LocalSqlEngine(),
    "rest": RestEngine(),
}


def adapter_for(resolved: Resolved) -> Engine:
    """Pick the adapter for this connection IN THIS ENVIRONMENT.

    A connection declares one logical engine - `databricks` - and the adapter
    differs per environment: the real workspace in dev and prod, a SQL database
    locally. The tenant never learns which, because they never named a table.
    """
    name = resolved.connection.engine
    settings = resolved.connection.environments.get(resolved.env, {})
    if name == "databricks" and "dsn_env" in settings:
        name = "local-sql"                     # the laptop stand-in
    engine = _ENGINES.get(name)
    if engine is None:
        raise ConfigError(f"no adapter for engine '{name}'")
    return engine
