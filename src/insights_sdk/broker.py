"""The data broker - the single path from a tenant app to any shared connection.

    rows   = query("hr.headcount", "SELECT dept, headcount FROM hr.headcount", ...)
    people = fetch("directory.people", params={"dept": "Engineering"})

That is the entire data API. There is no `connect()`, no cursor, no engine selection and
no exported way to obtain a connection - on purpose, and the test suite asserts it stays
that way.

Every control in the platform (entitlement, the two-key grant, masking, audit, the
telemetry field assertions) is enforceable only because there is exactly ONE code path
to data. Hand out a connection and all of them become advisory. See ADR-002.
"""

from __future__ import annotations

import re
import time
from typing import Any

from . import adapters, config, identity, telemetry
from .config import Manifest, Resolved
from .errors import EntitlementError, InsightsError

MASK = "***"


def _sensitivity(resolved: Resolved) -> str:
    """What the audit record says about the data's sensitivity.

    Authoritatively this is a Unity Catalog tag. We record our view of it so the
    correlation record is self-describing - UC's own audit remains the source of
    truth, and `insights compliance-report` reads both.
    """
    return "restricted" if resolved.restricted else "standard"


def _verb_for(engine: str) -> str:
    return {"databricks": "query()", "local-sql": "query()", "rest": "fetch()"}.get(engine, engine)


def _effective_roles(caller: identity.Caller, grant: dict | None) -> tuple[str, ...]:
    """Which roles apply, for LOCAL masking only.

    In dev and prod this is unused: the data platform applies column masks and row
    filters itself, per principal, on every path to the data. Locally there is no
    data platform, so the registry's local_masking block is approximated against
    whatever the local grant fixture says the identity may see.

    Note what is NOT consulted: the tenant's own manifest. It has no power over
    data access at all - which is why there is nothing here to defend against.
    """
    roles = caller.groups
    if caller.is_service and grant:
        roles = roles + tuple(grant.get("roles") or ())
    return roles


def _authorize(alias: str, expected_engine: str) -> tuple[Manifest, identity.Caller, Resolved, dict | None]:
    """Steps 1-5. Everything that must be true before a byte is read.

    Shared by every engine, which is why adding an engine cannot accidentally skip a check.
    """
    manifest = config.manifest()                                            # 1
    grant: dict | None = None

    if not manifest.declares(alias):                                        # 2  ENTITLEMENT
        raise EntitlementError(
            f"'{manifest.app}' is not entitled to '{alias}'. Add it to the `data:` block of "
            f"app.yaml and redeploy. Knowing a dataset's name is not access."
        )

    caller = identity.require_trusted()                                     # 3  IDENTITY
    resolved = config.catalog().resolve(alias)                              # 4  RESOLVE

    if resolved.restricted:                                                 # 5  VERIFY
        # NOT an approval step. The data owner grants their data to this app's
        # service identity in THEIR system; this checks that it happened, so the
        # failure is legible here instead of a PERMISSION_DENIED at query time in
        # production. In dev and prod the data platform would refuse us anyway -
        # this just refuses earlier and says who to ask.
        grant = config.grants().for_identity(alias, manifest.service_identity)
        if grant is None:
            raise EntitlementError(
                f"'{alias}' has not been granted to {manifest.service_identity}. "
                f"The platform cannot grant it - ask {resolved.dataset.owner}, who owns the "
                f"data. Run `insights access` for the exact request to send them."
            )
        # Teach the logger what must never appear in telemetry from this process. The list
        # comes from the platform catalog, so a tenant cannot shorten it (ADR-003).
        telemetry.register_sensitive_fields(alias, resolved.dataset.sensitive_fields)

    if resolved.connection.engine != expected_engine:
        raise InsightsError(
            f"'{alias}' lives on a {resolved.connection.engine} connection - use "
            f"{_verb_for(resolved.connection.engine)}, not {_verb_for(expected_engine)}."
        )
    return manifest, caller, resolved, grant


def _rewrite_and_scope(sql: str, alias: str, resolved: Resolved) -> str:
    """Step 6. Substitute the physical name, and refuse SQL that reaches past the declaration.

    The rewrite is what makes tenant SQL portable: `hr.headcount` is one table locally and
    another in production, and the app never learns that.

    The scope check closes the obvious hole - declare a harmless dataset, then select from
    a physical table you were never granted. It is not airtight (SQL built at runtime can
    evade it) and it does not need to be: the threat model here is accident, and the
    accident this prevents is a copy-pasted query silently reading the wrong table.
    """
    token = re.compile(r"(?<![\w.])" + re.escape(alias) + r"(?![\w.])")
    if not token.search(sql):
        raise EntitlementError(
            f"your SQL does not reference '{alias}', the dataset you named. Query the alias "
            f"directly - the platform substitutes the physical table for this environment."
        )
    physical = resolved.physical
    rewritten = token.sub(physical, sql)

    catalog = config.catalog()
    for other_name, other in catalog.datasets.items():
        if other_name == alias:
            continue
        other_loc = other.locations.get(resolved.env) or {}
        other_physical = other_loc.get("uc") or other_loc.get("table")
        for needle in filter(None, (other_name, other_physical)):
            if re.search(r"(?<![\w.])" + re.escape(needle) + r"(?![\w.])", rewritten):
                raise EntitlementError(
                    f"this query reaches '{needle}', which belongs to dataset '{other_name}'. "
                    f"One query, one declared dataset."
                )
    return rewritten


def _mask(rows: list[dict], resolved: Resolved, roles: tuple[str, ...]) -> tuple[list[dict], int]:
    """Apply column masking - ONLY where the data platform cannot.

    In dev and prod this is a no-op. Unity Catalog applies column masks and row
    filters itself, for every reader on every path including notebooks, and a
    second masking implementation beside it would be a second source of truth
    that drifts silently. See ADR-002 s2.

    Locally there is no Unity Catalog, so this approximates it from the
    registry's `local_masking` block. The behaviour a developer sees on a laptop
    therefore matches production; the ENFORCER differs, and that difference is
    documented rather than hidden.

    Masking rather than omission, deliberately: a column returned as `***` keeps
    the app working and tells its author the column exists but is not for them. A
    column that silently vanishes produces a confusing bug report instead.
    """
    if resolved.connection.governance_for(resolved.env) == "external":
        return rows, 0              # Unity Catalog already did this, per person

    rules = resolved.dataset.local_masking
    if not rules or not rows:
        return rows, 0
    hidden = [field for field, role in rules.items() if role not in roles]
    if not hidden:
        return rows, 0
    masked = [{k: (MASK if k in hidden else v) for k, v in row.items()} for row in rows]
    present = [f for f in hidden if f in rows[0]]
    return masked, len(present)


def _execute(alias: str, expected_engine: str, request: Any, sql_for_rewrite: str | None = None) -> list[dict]:
    manifest, caller, resolved, grant = _authorize(alias, expected_engine)
    roles = _effective_roles(caller, grant)

    if sql_for_rewrite is not None:                                         # 6
        sql = _rewrite_and_scope(sql_for_rewrite, alias, resolved)
        request = (sql, request)

    started = time.perf_counter()
    rows = adapters.adapter_for(resolved).run(resolved, request)              # 7
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    rows, masked_count = _mask(rows, resolved, roles)                       # 8

    telemetry.audit_read(                                                         # 9
        dataset=alias,
        classification=_sensitivity(resolved),
        owner=resolved.dataset.owner,
        connection=resolved.connection.name,
        rows=len(rows),
        ms=elapsed_ms,
        masked_fields=masked_count,
        via_grant=bool(grant) and caller.is_service,
    )
    return rows


def query(dataset: str, sql: str, **params: Any) -> list[dict]:
    """Read from a warehouse-backed dataset.

    Write SQL against the dataset ALIAS - the platform substitutes the physical table for
    whichever environment this is, so the same query works everywhere. Bind values with
    named parameters (`:month`); never format them into the string.
    """
    return _execute(dataset, "databricks", params, sql_for_rewrite=sql)


def fetch(dataset: str, *, params: dict[str, Any] | None = None) -> list[dict]:
    """Read from a REST-backed dataset.

    The resource path is resolved from the catalog, not passed in: the dataset names the
    thing, the platform knows where it lives in this environment. Always a list of dicts,
    so masking and auditing are identical code for both engines.
    """
    manifest, caller, resolved, grant = _authorize(dataset, "rest")
    roles = _effective_roles(caller, grant)
    resource = resolved.location["resource"]

    started = time.perf_counter()
    rows = adapters.adapter_for(resolved).run(resolved, (resource, params or {}))
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    rows, masked_count = _mask(rows, resolved, roles)
    telemetry.audit_read(
        dataset=dataset,
        classification=_sensitivity(resolved),
        owner=resolved.dataset.owner,
        connection=resolved.connection.name,
        rows=len(rows),
        ms=elapsed_ms,
        masked_fields=masked_count,
        via_grant=bool(grant) and caller.is_service,
    )
    return rows


__all__ = ["query", "fetch"]
