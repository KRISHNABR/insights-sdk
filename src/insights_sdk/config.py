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
_ALLOWED_TOP = {
    "apiVersion", "app", "team", "kind",
    "access",        # who manages the app, and who may use it
    "runtime",       # base image, size, sdk floor
    "data",          # dataset names
    "web",           # kind: web  - route, type, health
    "job",           # kind: job  - schedule, timeout, retries, concurrency...
    "outputs",       # what a job produces
    "environments",  # dev / uat / prod, and who approves each
}

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
class AppRole:
    """A role the app checks at runtime, and the corporate groups behind it.

    The platform reconciles `groups` into the edge's authorization table at deploy
    time, so nobody hand-creates a group and nobody hand-checks membership.
    """

    name: str
    groups: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True)
class Manage:
    """Who may change, deploy and govern the app - the CONTROL plane.

    Deliberately separate from AppRole, which is the DATA plane. Being able to
    deploy an app is not the same as being allowed to read what it reads, and
    conflating the two is the most common way an internal platform leaks.
    """

    owners: tuple[str, ...] = ()          # prod approvers; the only group that may request data
    contributors: tuple[str, ...] = ()    # dev/uat approvers; logs; no prod, no data requests
    readers: tuple[str, ...] = ()         # status and telemetry only


@dataclass(frozen=True)
class JobSpec:
    """The operational contract for kind: job."""

    schedule: str
    timezone: str = "UTC"
    timeout: str = "30m"
    retries: int = 0
    concurrency: str = "forbid"           # forbid | allow
    catchup: bool = False
    on_failure: str = "notify-owners"


@dataclass(frozen=True)
class WebSpec:
    """The shape of a kind: web app.

    `type` picks the base image, how identity reaches the code and the health
    contract. It does NOT change how data is reached - query() is identical in
    all of them, because the SDK is a library rather than a framework integration.

    There is deliberately no server-rendered template shape. Supporting one would
    mean the platform owning a UI framework - a layout, a component library, CSS -
    and that is not a thing three engineers should maintain forever when Streamlit
    already does it better.
    """

    route: str
    type: str = "api"                     # api | spa | streamlit
    health: str = "/healthz"


@dataclass(frozen=True)
class Manifest:
    app: str
    team: str
    kind: str                             # "web" | "job"
    manage: Manage
    roles: tuple[AppRole, ...]
    sdk_floor: str
    base: str
    size: str
    system_packages: tuple[str, ...]
    datasets: tuple[DatasetRequest, ...]
    web: WebSpec | None
    job: JobSpec | None
    outputs: tuple[dict, ...]
    environments: dict
    path: Path

    def declares(self, dataset: str) -> bool:
        """Step 2 of the broker. Knowing a dataset's name is not access."""
        return any(d.dataset == dataset for d in self.datasets)

    @property
    def owners(self) -> tuple[str, ...]:
        return self.manage.owners

    @property
    def role_names(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.roles)

    @property
    def schedule(self) -> str | None:
        return self.job.schedule if self.job else None

    @property
    def web_type(self) -> str:
        return self.web.type if self.web else "none"

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


VALID_WEB_TYPES = ("api", "spa", "streamlit")
VALID_BASES = ("python-web", "python-data", "python-min", "python-streamlit")


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

    for required in ("app", "team", "kind"):
        if not raw.get(required):
            raise ManifestError(f"{target}: '{required}' is required")

    kind = raw["kind"]
    if kind not in ("web", "job"):
        raise ManifestError(f"{target}: kind must be 'web' or 'job', got {kind!r}")

    # ---- access: control plane and data plane, kept apart --------------------
    access = raw.get("access") or {}
    manage_raw = access.get("manage") or {}
    manage = Manage(
        owners=tuple(manage_raw.get("owners") or ()),
        contributors=tuple(manage_raw.get("contributors") or ()),
        readers=tuple(manage_raw.get("readers") or ()),
    )
    if not manage.owners:
        raise ManifestError(
            f"{target}: access.manage.owners is required - somebody has to be able to approve a "
            f"production deploy and answer for this app. It must be a corporate group, not a person."
        )
    for group in manage.owners + manage.contributors + manage.readers:
        if "@" in group:
            raise ManifestError(
                f"{target}: '{group}' looks like an individual. Use a corporate group - "
                f"individuals leave, and an app owned by someone who left is an orphan."
            )

    roles = tuple(
        AppRole(
            name=r["name"],
            groups=tuple(r.get("groups") or ()),
            description=r.get("description", ""),
        )
        for r in (access.get("roles") or [])
    )

    # ---- runtime -------------------------------------------------------------
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
    base = runtime.get("base", "python-web" if kind == "web" else "python-data")
    if base not in VALID_BASES:
        raise ManifestError(
            f"{target}: runtime.base {base!r} is not published. Run `insights runtimes`. "
            f"You declare a runtime; you do not build an image."
        )

    # ---- kind-specific blocks: this is what makes `kind` mean something ------
    web = job = None
    if kind == "web":
        if raw.get("job"):
            raise ManifestError(f"{target}: a web app must not declare a job block")
        web_raw = raw.get("web") or {}
        web_type = web_raw.get("type", "api")
        if web_type not in VALID_WEB_TYPES:
            raise ManifestError(f"{target}: web.type must be one of {VALID_WEB_TYPES}, got {web_type!r}")
        web = WebSpec(
            route=web_raw.get("route", f"/{raw['app']}"),
            type=web_type,
            health=web_raw.get("health", "/healthz"),
        )
    else:
        if raw.get("web"):
            raise ManifestError(f"{target}: a job must not declare a web block")
        job_raw = raw.get("job") or {}
        if not job_raw.get("schedule"):
            raise ManifestError(
                f"{target}: a job must declare job.schedule - the platform runs it, you don't"
            )
        concurrency = job_raw.get("concurrency", "forbid")
        if concurrency not in ("forbid", "allow"):
            raise ManifestError(f"{target}: job.concurrency must be 'forbid' or 'allow'")
        job = JobSpec(
            schedule=job_raw["schedule"],
            timezone=job_raw.get("timezone", "UTC"),
            timeout=job_raw.get("timeout", "30m"),
            retries=int(job_raw.get("retries", 0)),
            concurrency=concurrency,
            catchup=bool(job_raw.get("catchup", False)),
            on_failure=job_raw.get("on_failure", "notify-owners"),
        )

    datasets = tuple(
        DatasetRequest(dataset=d["dataset"], access=d.get("access", "read"))
        for d in (raw.get("data") or [])
    )

    return Manifest(
        app=raw["app"],
        team=raw["team"],
        kind=kind,
        manage=manage,
        roles=roles,
        sdk_floor=str(floor),
        base=base,
        size=runtime.get("size", "small"),
        system_packages=tuple(runtime.get("system_packages") or ()),
        datasets=datasets,
        web=web,
        job=job,
        outputs=tuple(raw.get("outputs") or ()),
        environments=raw.get("environments") or {},
        path=target,
    )


# --------------------------------------------------------------------------------
# The platform registry: connections, datasets, grants
# --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Connection:
    name: str
    engine: str                     # "databricks" | "rest"
    description: str
    environments: dict
    governance: str = "none"        # external | local-approximation | none

    def governance_for(self, environment: str) -> str:
        """Who enforces column and row access for this connection, here.

        `external` means Unity Catalog does it and the broker must NOT - see
        ADR-002 s2. `local-approximation` is the laptop stand-in so the
        behaviour is demonstrable without a workspace.
        """
        return (self.environments.get(environment) or {}).get("governance", self.governance)


@dataclass(frozen=True)
class Dataset:
    """A nickname, where it points, and who to ask.

    Note what is absent: classification and masking rules. Those are Unity
    Catalog tags and UC masking functions - enforced on every path to the data,
    not just ours. This registry is a projection (ADR-002 s2).
    """

    name: str
    description: str
    connection: str
    owner: str
    locations: dict
    local_masking: dict             # laptop-only stand-in for UC column masks
    sensitive_fields: tuple[str, ...]   # ours: about our log pipeline, not the data platform

    @property
    def restricted(self) -> bool:
        """True when UC tags this `sensitivity=restricted`.

        Locally we infer it from the presence of masking rules, since there is no
        UC to ask. In dev and prod the deploy pipeline reads the UC tag.
        """
        return bool(self.local_masking) or bool(self.sensitive_fields)


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

    @property
    def physical(self) -> str:
        """The name to substitute for the alias, in this environment.

        Locally that is a table name; in dev and prod it is the Unity Catalog
        three-level name. The tenant's SQL says the alias either way.
        """
        return self.location.get("uc") or self.location["table"]


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
            governance=body.get("governance", "none"),
        )
        for name, body in (raw.get("connections") or {}).items()
    }
    datasets = {
        name: Dataset(
            name=name,
            description=body.get("description", ""),
            connection=body["connection"],
            owner=body["owner"],
            locations=body.get("locations", {}),
            local_masking=body.get("local_masking", {}) or {},
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
