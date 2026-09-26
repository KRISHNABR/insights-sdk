"""Loading and validating the two things that configure an app.

    app.yaml        the TENANT's declaration of intent. Lives in the tenant repo.
    catalog.yaml    the PLATFORM's registry of mechanism. Tenants cannot edit it.
    grants.yaml     the PLATFORM's record of approvals. The second key (ADR-002 s4).

The split is the design. A tenant says *what* it needs; the platform decides *how* that
is satisfied, and *whether* it may be.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError, ManifestError, UnknownDatasetError

DEFAULT_ENV = "local"

# Keys a tenant manifest may contain. Anything else is rejected rather than ignored,
# because a silently-ignored key is a tenant believing something is configured.
_ALLOWED_TOP = {"apiVersion", "app", "team", "kind", "owners", "runtime", "data", "access", "schedule"}

# Words that describe MECHANISM. A manifest declares intent, never mechanism - so these
# may not appear anywhere in it, at any depth. This is "declare, don't wire" made
# structural: a tenant cannot pin a table, choose an engine, smuggle in a DSN, or
# downgrade a classification, because the loader refuses the file.
_FORBIDDEN_ANYWHERE = {
    "classification",  # ADR-002 s4 - sensitivity is the catalog's to state
    "connection",      # ADR-002 s2 - nobody is granted a connection
    "engine",
    "credential",
    "secret",
    "dsn",
    "table",
    "host",
}


def env() -> str:
    """Which environment we are in. Drives catalog location resolution."""
    return os.environ.get("INSIGHTS_ENV", DEFAULT_ENV)


def now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------
# The tenant manifest
# --------------------------------------------------------------------------------

@dataclass(frozen=True)
class DatasetRequest:
    dataset: str
    access: str


@dataclass(frozen=True)
class Manifest:
    app: str
    team: str
    kind: str                       # "web" | "job"
    owners: tuple[str, ...]
    sdk_floor: str
    datasets: tuple[DatasetRequest, ...]
    roles: tuple[str, ...]
    schedule: str | None
    path: Path

    def declares(self, dataset: str) -> bool:
        """Step 2 of the broker. Knowing a dataset's name is not access."""
        return any(d.dataset == dataset for d in self.datasets)

    @property
    def service_subject(self) -> str:
        """The identity a scheduled run acts as. Jobs have no interactive caller."""
        return f"svc:{self.app}"


def _scan_forbidden(node: Any, where: str = "app.yaml") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() in _FORBIDDEN_ANYWHERE:
                raise ManifestError(
                    f"{where}: '{key}' may not appear in a tenant manifest. "
                    f"A manifest declares intent; the platform catalog decides mechanism."
                )
            _scan_forbidden(value, f"{where}:{key}")
    elif isinstance(node, list):
        for item in node:
            _scan_forbidden(item, where)


def _find_manifest() -> Path:
    explicit = os.environ.get("INSIGHTS_APP_MANIFEST")
    if explicit:
        return Path(explicit)
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        found = candidate / "app.yaml"
        if found.is_file():
            return found
    raise ManifestError(
        "No app.yaml found. Every app on this platform has one - run `insights new-app` "
        "to generate a valid manifest, or set INSIGHTS_APP_MANIFEST."
    )


def load_manifest(path: str | Path | None = None) -> Manifest:
    target = Path(path) if path else _find_manifest()
    if not target.is_file():
        raise ManifestError(f"No manifest at {target}")

    raw = yaml.safe_load(target.read_text()) or {}
    if not isinstance(raw, dict):
        raise ManifestError(f"{target}: expected a mapping at the top level")

    unknown = set(raw) - _ALLOWED_TOP
    if unknown:
        raise ManifestError(f"{target}: unknown key(s) {sorted(unknown)}. Allowed: {sorted(_ALLOWED_TOP)}")
    _scan_forbidden(raw, str(target))

    for required in ("app", "team", "kind", "owners"):
        if not raw.get(required):
            raise ManifestError(f"{target}: '{required}' is required")

    kind = raw["kind"]
    if kind not in ("web", "job"):
        raise ManifestError(f"{target}: kind must be 'web' or 'job', got {kind!r}")

    schedule = raw.get("schedule")
    # `kind` has to mean something, or it is decoration. These two rules are what make it real.
    if kind == "job" and not schedule:
        raise ManifestError(f"{target}: a job must declare a schedule - the platform runs it, not you")
    if kind == "web" and schedule:
        raise ManifestError(f"{target}: a web app must not declare a schedule")

    runtime = raw.get("runtime") or {}
    floor = runtime.get("sdk")
    if not floor:
        raise ManifestError(f"{target}: runtime.sdk is required - declare a FLOOR such as '>=0.1,<1', not a pin")
    if "==" in str(floor):
        # ADR-001: pinning freezes an app on a version we will eventually stop supporting,
        # and makes the upgrade story a negotiation instead of a default.
        raise ManifestError(
            f"{target}: runtime.sdk is pinned ({floor!r}). Declare a floor and a major bound, "
            f"e.g. '>=0.1,<1', so patches and minors reach you automatically. See ADR-001."
        )

    datasets = tuple(
        DatasetRequest(dataset=d["dataset"], access=d.get("access", "read"))
        for d in (raw.get("data") or [])
    )
    roles = tuple((raw.get("access") or {}).get("roles") or [])

    return Manifest(
        app=raw["app"],
        team=raw["team"],
        kind=kind,
        owners=tuple(raw["owners"]),
        sdk_floor=str(floor),
        datasets=datasets,
        roles=roles,
        schedule=schedule,
        path=target,
    )


# --------------------------------------------------------------------------------
# The platform registry: connections, datasets, grants
# --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Connection:
    name: str
    engine: str                     # "warehouse" | "rest"
    description: str
    environments: dict


@dataclass(frozen=True)
class Dataset:
    name: str
    description: str
    connection: str
    classification: str             # internal | confidential | restricted
    owner: str
    locations: dict
    masking: dict                   # field -> role required to see it unmasked
    sensitive_fields: tuple[str, ...]

    @property
    def restricted(self) -> bool:
        return self.classification == "restricted"


@dataclass(frozen=True)
class Resolved:
    """Everything the broker needs, assembled in step 4. The only seam that would change
    if this registry were replaced by a client against a real data catalog (ADR-002 s6)."""

    dataset: Dataset
    connection: Connection
    location: dict                  # env-specific: {"table": ...} or {"resource": ...}
    env: str

    @property
    def restricted(self) -> bool:
        return self.dataset.restricted


@dataclass(frozen=True)
class Catalog:
    connections: dict[str, Connection]
    datasets: dict[str, Dataset]
    path: Path

    def resolve(self, alias: str, environment: str | None = None) -> Resolved:
        environment = environment or env()
        ds = self.datasets.get(alias)
        if ds is None:
            # Deliberately does NOT list what does exist. There is no discovery here
            # (ADR-002 s5) and an error message is a poor place to start one.
            raise UnknownDatasetError(f"'{alias}' is not a dataset on this platform.")
        conn = self.connections.get(ds.connection)
        if conn is None:
            raise ConfigError(f"catalog: dataset '{alias}' names unknown connection '{ds.connection}'")
        location = ds.locations.get(environment)
        if not location:
            raise ConfigError(f"catalog: dataset '{alias}' has no location for env '{environment}'")
        return Resolved(dataset=ds, connection=conn, location=location, env=environment)


def _registry_dir() -> Path:
    explicit = os.environ.get("INSIGHTS_REGISTRY_DIR")
    if explicit:
        return Path(explicit)
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        found = candidate / "insights-platform" / "control" / "registry"
        if found.is_dir():
            return found
    raise ConfigError(
        "Cannot locate the platform registry. In a deployed app it is mounted at "
        "/etc/insights; locally set INSIGHTS_REGISTRY_DIR."
    )


def load_catalog(path: str | Path | None = None) -> Catalog:
    target = Path(path) if path else _registry_dir() / "catalog.yaml"
    if not target.is_file():
        raise ConfigError(f"No catalog at {target}")
    raw = yaml.safe_load(target.read_text()) or {}

    connections = {
        name: Connection(
            name=name,
            engine=body["engine"],
            description=body.get("description", ""),
            environments=body.get("environments", {}),
        )
        for name, body in (raw.get("connections") or {}).items()
    }
    datasets = {
        name: Dataset(
            name=name,
            description=body.get("description", ""),
            connection=body["connection"],
            classification=body.get("classification", "internal"),
            owner=body["owner"],
            locations=body.get("locations", {}),
            masking=body.get("masking", {}) or {},
            sensitive_fields=tuple(body.get("sensitive_fields", []) or []),
        )
        for name, body in (raw.get("datasets") or {}).items()
    }
    return Catalog(connections=connections, datasets=datasets, path=target)


@dataclass(frozen=True)
class Grants:
    grants: tuple[dict, ...]
    break_glass: tuple[dict, ...]
    path: Path

    @staticmethod
    def _active(entry: dict, at: datetime) -> bool:
        if entry.get("revoked_at"):
            return False
        expires = entry.get("expires_at")
        if not expires:
            return True                     # standing grant
        return at < datetime.fromisoformat(str(expires).replace("Z", "+00:00"))

    def for_app(self, dataset: str, app: str, at: datetime | None = None) -> dict | None:
        at = at or now()
        for entry in self.grants:
            if entry.get("dataset") == dataset and entry.get("app") == app and self._active(entry, at):
                return entry
        return None

    def break_glass_for(self, dataset: str, operator: str, at: datetime | None = None) -> dict | None:
        at = at or now()
        for entry in self.break_glass:
            if entry.get("dataset") == dataset and entry.get("operator") == operator and self._active(entry, at):
                return entry
        return None


def load_grants(path: str | Path | None = None) -> Grants:
    target = Path(path) if path else _registry_dir() / "grants.yaml"
    if not target.is_file():
        # An absent grants file means nothing is granted. Fail closed, do not assume.
        return Grants(grants=(), break_glass=(), path=target)
    raw = yaml.safe_load(target.read_text()) or {}
    return Grants(
        grants=tuple(raw.get("grants") or []),
        break_glass=tuple(raw.get("break_glass") or []),
        path=target,
    )


# --------------------------------------------------------------------------------
# Process-wide cache. Loaded once at startup so a hot request path does no file I/O,
# and so a mid-run edit to the registry cannot change enforcement under a running app.
# --------------------------------------------------------------------------------

_manifest: Manifest | None = None
_catalog: Catalog | None = None
_grants: Grants | None = None


def manifest() -> Manifest:
    global _manifest
    if _manifest is None:
        _manifest = load_manifest()
    return _manifest


def catalog() -> Catalog:
    global _catalog
    if _catalog is None:
        _catalog = load_catalog()
    return _catalog


def grants() -> Grants:
    global _grants
    if _grants is None:
        _grants = load_grants()
    return _grants


def reset() -> None:
    """Drop the cache. For tests and for the CLI, which may act on several apps in one process."""
    global _manifest, _catalog, _grants
    _manifest = _catalog = _grants = None
