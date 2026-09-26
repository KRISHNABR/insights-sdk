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


class WarehouseEngine:
    """The analytics warehouse. Stubbed as SQLite; the seam is the DSN."""

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
    "warehouse": WarehouseEngine(),
    "rest": RestEngine(),
}


def engine_for(resolved: Resolved) -> Engine:
    engine = _ENGINES.get(resolved.connection.engine)
    if engine is None:
        raise ConfigError(f"no adapter for engine '{resolved.connection.engine}'")
    return engine
