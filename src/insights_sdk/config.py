"""Loading and validating `app.yaml` - the one file a tenant writes to configure an app.

It declares four things and nothing else:

    who manages it        owners / contributors / readers, as corporate groups
    what it connects to   engine, host, and the NAME of a secret. Never a value
    how it is served      a route and a shape, or a cron schedule
    how big it is         small | medium | large

There used to be two more files here - `catalog.yaml` and `grants.yaml` - through which
the platform brokered every read, resolving dataset nicknames and checking entitlements.
They are gone (ADR-002). Teams already have access to their data; the platform ships the
connector and holds the credential, and does not stand between an app and its warehouse.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError, ManifestError

DEFAULT_ENV = "local"

# Keys a tenant manifest may contain. Anything else is rejected rather than ignored,
# because a silently-ignored key is a tenant believing something is configured.
_ALLOWED_TOP = {
    "apiVersion", "app", "team", "kind",
    "access",        # who manages the app, and who may use it
    "runtime",       # base image, size, sdk floor
    "connections",   # engine, host and a secret NAME. Never a credential
    # Listed only so the dedicated refusal below fires instead of a generic
    # "unknown key" - a removed field deserves a message saying where it went.
    "environments",
    "web",           # kind: web  - route, type, health
    "job",           # kind: job  - schedule, timeout, retries, concurrency...
    "outputs",       # what a job produces
}

# Words that describe MECHANISM. A manifest declares intent, never mechanism - so these
# may not appear anywhere in it, at any depth. This is "declare, don't wire" made
# structural: a tenant cannot pin a table, choose an engine, smuggle in a DSN, or
# downgrade a classification, because the loader refuses the file.
_FORBIDDEN_ANYWHERE = {
    "classification",  # ADR-002 s4 - sensitivity is the data platform's to state
    "credential",      # a value, in a file that lives in git forever
    "dsn",             # a connection string is usually a credential wearing a hat
}



def env() -> str:
    """Which environment we are in. Connections use it to resolve ${VAR} hosts."""
    return os.environ.get("INSIGHTS_ENV", DEFAULT_ENV)


def now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------
# The tenant manifest
# --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Manage:
    """Who may do what with this app. Three tiers, and there is no fourth.

    An earlier version had these PLUS an `access.roles` block where an app defined
    its own named roles and mapped each to corporate groups. It was removed: two
    authorization vocabularies in one file meant every reader had to work out which
    one a given check used, and in practice every app's roles collapsed to some
    restatement of these three anyway.

    What it costs: an app can no longer express "only Finance may see the salary
    tab" in the manifest. That is a real loss and it is the right trade here - with
    the platform shipping connectors rather than brokering data, what a caller may
    READ is decided by the data platform against their own identity, not by a role
    this file invented.

    The tiers nest: an owner can do anything a contributor can, and a contributor
    anything a reader can. Encoded in `groups_for`, so nobody has to remember it.
    """

    owners: tuple[str, ...] = ()          # prod approvers; may change access itself
    contributors: tuple[str, ...] = ()    # dev/uat approvers; logs; no prod
    readers: tuple[str, ...] = ()         # status and telemetry only

    #: Most-privileged first. The order IS the hierarchy.
    TIERS = ("owner", "contributor", "reader")

    def groups_for(self, tier: str) -> tuple[str, ...]:
        """Every group that satisfies `tier`, including the ones above it."""
        if tier not in self.TIERS:
            raise ConfigError(
                f"unknown access tier {tier!r}. This platform has exactly three: "
                f"{', '.join(self.TIERS)}."
            )
        ladder = {"owner": (self.owners,),
                  "contributor": (self.owners, self.contributors),
                  "reader": (self.owners, self.contributors, self.readers)}
        return tuple(dict.fromkeys(g for bucket in ladder[tier] for g in bucket))

    @property
    def everyone(self) -> tuple[str, ...]:
        return self.groups_for("reader")


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

    Two shapes, not more. A server-rendered template shape would mean the platform
    owning a UI framework - layout, components, CSS - which three engineers should
    not maintain forever. A Streamlit shape is genuinely wanted and is NOT accepted
    here, because it needs an identity shim and a health sidecar that do not exist:
    a manifest that accepts a shape the platform cannot deliver fails at deploy
    instead of at `insights doctor`, which is the wrong layer (ADR-004, ADR-005).
    """

    route: str
    type: str = "api"                     # api | spa
    health: str = "/healthz"


@dataclass(frozen=True)
class ConnectionSpec:
    """A connection a team declared. Config, never a credential.

    `secret` is a NAME, not a value. The manifest is in git; a value here would be a
    value in git forever, and no amount of rotation gets it back out of the history.
    The loader refuses anything that looks like it went in by accident - see
    `_FORBIDDEN_IN_CONNECTIONS`.
    """

    name: str
    engine: str
    secret: str | None
    options: dict                         # host, http_path, database, base_url, timeout...


@dataclass(frozen=True)
class Manifest:
    app: str
    team: str
    kind: str                             # "web" | "job"
    manage: Manage
    size: str
    system_packages: tuple[str, ...]
    connections: tuple[ConnectionSpec, ...]
    web: WebSpec | None
    job: JobSpec | None
    outputs: tuple[dict, ...]
    path: Path

    @property
    def owners(self) -> tuple[str, ...]:
        return self.manage.owners


    @property
    def schedule(self) -> str | None:
        return self.job.schedule if self.job else None

    @property
    def web_type(self) -> str:
        return self.web.type if self.web else "none"

    @property
    def service_identity(self) -> str:
        """The service identity this app's unattended work runs as.

        DERIVED from the registered app name - never read from a field a tenant
        can edit. That is the whole of the platform's job for unattended work: a
        team must not be able to make their job run as somebody else's identity
        and inherit their data access.

        This is the principal a data owner grants to, the one that appears in the
        data platform's audit, AND the subject we stamp on our own telemetry. One
        string, deliberately: a compliance reviewer joins our audit trail to Unity
        Catalog's on a literal match. We used to write `svc:comp-report` in our logs
        while the grant said `sp-comp-report`, which made the two trails joinable
        only by someone who knew the renaming rule.
        """
        return f"sp-{self.app}"



#: Keys that must never appear on a connection. Each one is a credential someone
#: pasted in while debugging and meant to remove. The manifest is in git, so "meant
#: to" is not good enough: refuse the file rather than accept the commit.
_FORBIDDEN_IN_CONNECTIONS = {
    "password", "passwd", "pwd", "token", "access_token", "api_key", "apikey",
    "client_secret", "secret_key", "private_key", "credential", "credentials",
    "connection_string", "dsn", "sas_token", "account_key",
}


def _parse_connection(entry: dict, target: Path) -> "ConnectionSpec":
    from .connectors import SUPPORTED_ENGINES

    if not isinstance(entry, dict) or "name" not in entry:
        raise ManifestError(f"{target}: every entry under `connections:` needs a `name`")
    name = str(entry["name"])

    engine = entry.get("engine") or entry.get("type")
    if engine not in SUPPORTED_ENGINES:
        raise ManifestError(
            f"{target}: connection '{name}' has engine {engine!r}. "
            f"Supported: {', '.join(SUPPORTED_ENGINES)}. Adding one is a platform "
            f"change - a driver in the base image and an entry here - so ask rather "
            f"than working around it."
        )

    leaked = sorted(k for k in entry if str(k).lower() in _FORBIDDEN_IN_CONNECTIONS)
    if leaked:
        raise ManifestError(
            f"{target}: connection '{name}' contains {', '.join(leaked)}. "
            f"app.yaml is in git - a credential here is a credential in the history "
            f"forever. Use `secret: <name>` and put the value in the secret store, "
            f"which the platform team cannot read."
        )

    # ${VAR} placeholders are kept RAW here and expanded at connect() time.
    #
    # Loading a manifest must not require the runtime environment: `insights doctor`,
    # the deploy gate and the console all read manifests, and none of them is the
    # environment the app will run in. Expanding here made the gate fail on a
    # perfectly valid manifest because CI had not exported a variable that only
    # matters at runtime.
    options = {k: v for k, v in entry.items()
               if k not in ("name", "engine", "type", "secret", "local")}
    secret = str(entry["secret"]) if entry.get("secret") else None

    # A `local:` block replaces the connection when INSIGHTS_ENV=local, and only then.
    #
    # It exists because there is no Databricks on a laptop. Without it a manifest
    # either describes production and cannot run locally, or describes the laptop and
    # is a lie about production - and the previous version did the latter, with a
    # relative path to a sibling repo baked into a tenant's contract.
    #
    # Scoped to exactly one environment on purpose: it cannot be used to vary dev
    # from prod, which is where "it worked in dev" comes from.
    override = entry.get("local")
    if override is not None and env() == "local":
        if not isinstance(override, dict):
            raise ManifestError(f"{target}: connection '{name}': `local:` must be a block")
        engine = override.get("engine", engine)
        if engine not in SUPPORTED_ENGINES:
            raise ManifestError(
                f"{target}: connection '{name}' local override has engine {engine!r}. "
                f"Supported: {', '.join(SUPPORTED_ENGINES)}."
            )
        leaked = sorted(k for k in override if str(k).lower() in _FORBIDDEN_IN_CONNECTIONS)
        if leaked:
            raise ManifestError(
                f"{target}: connection '{name}' local override contains {', '.join(leaked)}. "
                f"A local credential is still a credential in git."
            )
        # MERGE, do not replace. A field the override does not name is inherited,
        # so a local run still resolves the same secret and still exercises the
        # credential path - which is the half of the connection most likely to be
        # wrong in production and least likely to be tested if local skips it.
        if "secret" in override:
            secret = str(override["secret"]) if override["secret"] else None
        options = {**options, **{k: v for k, v in override.items()
                                 if k not in ("engine", "secret")}}
        # A local sqlite file and a production host are alternatives, not a pair.
        if override.get("engine") == "sqlite":
            options.pop("host", None)
            options.pop("http_path", None)

    return ConnectionSpec(name=name, engine=engine, secret=secret, options=options)


def _scan_forbidden(node: Any, where: str = "app.yaml") -> None:
    """Refuse keys a tenant must not set, at any depth.

    `classification` is the one that matters most: a tenant marking their own data
    non-sensitive is the whole reason this scanner exists, and nesting it three levels
    deep must not get it past.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if lowered in _FORBIDDEN_ANYWHERE:
                raise ManifestError(
                    f"{where}: '{key}' may not appear in a tenant manifest. "
                    f"Sensitivity is the data platform's to state, and a credential "
                    f"belongs in the secret store - app.yaml is in git."
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


VALID_WEB_TYPES = ("api", "spa")


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

    # Unknown keys inside a BLOCK, not just at the top level.
    #
    # `service_identity: sp-someone-else` at the top level was refused, but nested
    # under `runtime:` it was silently ignored - accepted, with no effect. The derived
    # identity still won, so nothing was insecure; what broke is the rule this
    # platform leans on everywhere: refuse, do not ignore. A key a team writes and the
    # loader drops is a team believing something is configured.
    _ALLOWED_IN = {
        # `sdk` and `base` are listed so the dedicated "was removed, here is where it
        # lives now" message below fires instead of a generic unknown-key error. A
        # removed field deserves better than being told it is a typo.
        "runtime": {"size", "system_packages", "sdk", "base"},
        "web": {"route", "type", "health"},
        "job": {"schedule", "timezone", "timeout", "retries", "concurrency",
                "catchup", "on_failure", "schedule_enabled"},
        # `roles` is listed for the same reason as runtime.sdk: so its dedicated
        # removal message fires rather than a generic unknown-key error.
        "access": {"manage", "roles"},
    }
    for block, allowed in _ALLOWED_IN.items():
        body = raw.get(block)
        if not isinstance(body, dict):
            continue
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise ManifestError(
                f"{target}: unknown key(s) {unknown} under `{block}:`. "
                f"Allowed: {sorted(allowed)}. Refused rather than ignored - a key we "
                f"drop is a key you think is doing something."
            )

    if raw.get("environments"):
        raise ManifestError(
            f"{target}: `environments:` was removed. It said who approves a deploy and "
            f"whether one is automatic - both of which are already true elsewhere: the "
            f"approval gate is a GitHub environment whose reviewers the platform "
            f"reconciles from `access.manage`, and 'automatic' is simply which of the "
            f"three deploy workflows exists. Nothing ever read this block."
        )

    if access.get("roles"):
        raise ManifestError(
            f"{target}: `access.roles` was removed. This platform has exactly three "
            f"tiers - owners, contributors, readers - and `require_role()` takes one "
            f"of those. Refused rather than ignored: a silently-ignored roles block "
            f"is a team believing an authorization rule is in force when it is not."
        )

    # ---- runtime -------------------------------------------------------------
    #
    # `sdk` and `base` used to live here and no longer do. Both were second copies of
    # something already stated elsewhere, and a second copy is a thing that drifts:
    #
    #   the SDK version   pyproject.toml and uv.lock already pin it, and uv enforces
    #                     that on every build. A range here could disagree with the
    #                     lockfile and nothing would notice.
    #   the base image    the Dockerfile's FROM says it, and the tenant owns that file
    #                     now (ADR-004). We had a CI rule that the two must AGREE -
    #                     which is the smell: a reconciliation between two sources of
    #                     truth that should have been one.
    #
    # `size` stays, because cpu/memory/replicas has nowhere else to be said.
    runtime = raw.get("runtime") or {}
    for removed, where in (("sdk", "pyproject.toml"), ("base", "the Dockerfile's FROM line")):
        if removed in runtime:
            raise ManifestError(
                f"{target}: runtime.{removed} was removed - {where} is the single source "
                f"of truth for it now. Delete the line."
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


    connections = tuple(_parse_connection(entry, target) for entry in (raw.get("connections") or []))

    return Manifest(
        app=raw["app"],
        team=raw["team"],
        kind=kind,
        manage=manage,
        size=runtime.get("size", "small"),
        system_packages=tuple(runtime.get("system_packages") or ()),
        connections=connections,
        web=web,
        job=job,
        outputs=tuple(raw.get("outputs") or ()),
        path=target,
    )


# --------------------------------------------------------------------------------

_manifest: Manifest | None = None


def manifest() -> Manifest:
    """The manifest for this process, loaded once.

    Cached because it is read on every request and every connection, and re-parsing
    YAML per call is a measurable cost in a web app.
    """
    global _manifest
    if _manifest is None:
        _manifest = load_manifest()
    return _manifest


def reset() -> None:
    """Drop the cache. For tests, and for the CLI, which may act on several apps in
    one process - `insights up` loads every manifest in the workspace."""
    global _manifest
    _manifest = None
