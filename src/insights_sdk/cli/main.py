"""`insights` - the one command a tenant team learns.

Design rule for this CLI: **every command either generates something correct, or shows
you evidence.** There is no command that explains a rule, because a rule that needs
explaining is enforced in the wrong place (ADR-004).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__, config, connectors, secrets
from ..errors import InsightsError
from . import scaffold

PLATFORM_MARKER = Path("insights-platform")


# --------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------

def _workspace() -> Path:
    """The directory holding the four repos. Only needed by the local dev commands."""
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / PLATFORM_MARKER).is_dir():
            return candidate
    raise InsightsError("Run this from inside the insights-hub workspace.")


def _platform() -> Path:
    return _workspace() / PLATFORM_MARKER


def _registry() -> Path:
    return _platform() / "control" / "registry"


def _apps_file(local: bool = False) -> Path:
    """Where the app registry lives.

    `apps.json` is the real thing: CI appends to it when an app deploys, and it is
    version-controlled because "what is deployed" is a fact worth reviewing.

    `apps.local.json` is what `insights up` writes - the same shape, but with localhost
    ports in it. Separate file so running the local stack never leaves the platform repo
    dirty, and so nobody accidentally commits a port number as if it were a deployment.
    """
    return _registry() / ("apps.local.json" if local else "apps.json")


def _registered() -> dict:
    for path in (_apps_file(local=True), _apps_file()):
        if path.is_file():
            body = json.loads(path.read_text())
            return {k: v for k, v in body.items() if not k.startswith("_")}
    return {}


def _pythonpath(platform: Path) -> str:
    """Make the PLATFORM's runtime importable by a child process. Not the SDK.

    Child processes need `runtime.edge.main` and friends, which live here and are not
    packaged. They must NOT get the SDK this way.

    It used to prepend `../insights-sdk/src`, which meant `insights run` executed the
    sibling working copy instead of the version in the app's own lockfile - so a
    tenant testing locally was running code their lock did not describe, and a run
    could pass here and fail in CI on the same commit. It also silently required a
    sibling checkout that a tenant cloning only their own repo does not have.

    The SDK now comes from the app's virtualenv, like every other dependency. To work
    on the SDK and an app together, overlay it explicitly for one command:

        uv run --with-editable ../insights-sdk insights run
    """
    return os.pathsep.join(
        [str(platform), os.environ.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)


def _sink_dir() -> Path:
    return _platform() / "runtime" / "sinks"


def _proc_log_dir() -> Path:
    """Process logs - stdout/stderr of each local process.

    Deliberately NOT the same place as the telemetry sinks. A sink record is
    structured, redacted and treated as evidence; a process log is whatever uvicorn
    felt like printing, including tracebacks. Keeping them apart stops anyone
    reasoning about one as if it were the other.
    """
    return _platform() / "runtime" / "sinks" / "proc"


def _read_sink(name: str) -> list[dict]:
    path = _sink_dir() / f"{name}.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _ok(text: str) -> str:
    return f"  \033[32mok\033[0m    {text}"


def _bad(text: str) -> str:
    return f"  \033[31mFAIL\033[0m  {text}"


# --------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------

def cmd_new_app(args) -> int:
    """Generate an app. The generator is the cheapest enforcement layer there is:
    if it is generated, it cannot be got wrong (ADR-004)."""
    target = Path(args.directory or f"insights-{args.name}")
    created = scaffold.generate(
        target=target, name=args.name, kind=args.kind, team=args.team, owner=args.owner
    )
    print(f"created {target}/")
    for path in created:
        print(f"  {path.relative_to(target)}")
    print(
        "\nnext:\n"
        f"  cd {target}\n"
        "  insights doctor          # check the manifest before you push\n"
        "  insights connections     # what it talks to, and whether it can\n"
    )
    return 0


def cmd_upgrade_scaffold(args) -> int:
    """Re-render the platform-owned files in this tenant repo (ADR-001, mechanism 7).

    The library upgrades itself when a tenant rebuilds; the SCAFFOLD does not, and that
    is the failure mode nobody notices - an app created eighteen months ago carrying an
    eighteen-month-old Dockerfile, with nothing to tell anyone.

    This works only because those files were GENERATED rather than cloned from a template
    repo: the SDK knows which files it owns, so it can safely rewrite exactly those and
    touch nothing else. A cloned template can never be refreshed, because nothing knows
    where it came from.
    """
    manifest = config.manifest()
    root = manifest.path.parent
    rendered = scaffold.render_platform_owned(manifest.app)

    changed, same = [], []
    for relative, content in rendered.items():
        path = root / relative
        current = path.read_text() if path.is_file() else None
        (same if current == content else changed).append(relative)
        if current != content and not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    for relative in same:
        print(_ok(f"{relative} up to date"))
    for relative in changed:
        print(_bad(f"{relative} out of date") if args.check else f"  updated  {relative}")

    if not changed:
        print(f"\nscaffold is current (sdk {__version__})")
        return 0
    if args.check:
        print(f"\n{len(changed)} file(s) behind sdk {__version__}. Run `insights upgrade-scaffold`.")
        return 1
    print(f"\nupdated {len(changed)} file(s) to sdk {__version__}. Review the diff and commit.")
    return 0


def cmd_build(args) -> int:
    """Build this app's image, or show what would be built.

    The Dockerfile belongs to the repo now (ADR-004), so this builds YOUR file - it
    does not render one behind your back. `--show` prints it, and if it is missing
    prints the template `insights new-app` would have generated, so a repo that
    predates the change can recover it with one redirect.
    """
    manifest = config.manifest()
    root = manifest.path.parent
    dockerfile = root / "Dockerfile"

    if args.write:
        # A subcommand rather than `--show > Dockerfile`, because that recipe
        # destroys the file it is meant to create: the shell truncates the target
        # before the command runs, so --show then reads back an empty file. Writing
        # it here means the read and the write cannot race.
        if dockerfile.is_file() and not args.force:
            print(f"{dockerfile.name} already exists. It is yours - we will not "
                  f"overwrite it. Use --force if you really mean to.")
            return 1
        dockerfile.write_text(scaffold.render_dockerfile(manifest))
        print(f"wrote {dockerfile}\n\nIt belongs to this repo now. CI checks the base "
              f"image, the version pin and the final USER; it does not check the rest.")
        return 0

    if args.show:
        if dockerfile.is_file():
            print(f"# {dockerfile}  (yours - the platform does not rewrite it)\n")
            print(dockerfile.read_text().rstrip())
        else:
            print("# NO Dockerfile in this repo. Below is what `insights build --write`\n"
                  "# would generate today.\n")
            print(scaffold.render_dockerfile(manifest).rstrip())
        return 0

    if not dockerfile.is_file():
        print("no Dockerfile in this repo. It is yours to own now:\n\n"
              "    insights build --write\n\n"
              "then read it - CI checks the base image, the pin and the final USER.")
        return 1

    tag = f"insights/{manifest.app}:local"
    command = ["docker", "build", "-f", str(dockerfile), "-t", tag, str(root)]
    print(f"$ {' '.join(command)}")
    try:
        return subprocess.run(command).returncode
    except FileNotFoundError:
        print("\ndocker is not installed — `insights build --show` prints the Dockerfile "
              "without needing it.")
        return 0


def cmd_doctor(args) -> int:
    """Everything CI will check, checked locally first. Same code path, so it cannot
    disagree with the pipeline."""
    print(f"insights doctor  (sdk {__version__})\n")
    problems = 0
    try:
        manifest = config.manifest()
        print(_ok(f"manifest {manifest.path.name}: {manifest.app} ({manifest.kind}, team {manifest.team})"))
    except InsightsError as exc:
        print(_bad(f"manifest: {exc}"))
        return 1

    # The registry is only needed to resolve `data:` entries - the older brokered
    # model. An app that declares only `connections:` has nothing to look up, and a
    # freshly generated app declares neither.
    #
    # Loading it unconditionally meant `insights doctor` in a brand new app failed
    for connection in manifest.connections:
        detail = connection.engine
        if connection.secret:
            try:
                secrets.resolve(manifest.app, connection.secret)
                detail += f", secret '{connection.secret}' present"
            except InsightsError:
                print(_bad(f"connection {connection.name}: secret '{connection.secret}' is not set"))
                print(f"        expected at {secrets.path_for(manifest.app, connection.secret)}")
                print("        your team sets it. The platform cannot read or write the value.")
                problems += 1
                continue
        print(_ok(f"connection {connection.name} ({detail})"))

    # The Dockerfile belongs to the repo. There is no platform base image to be
    # behind any more, so the only thing worth saying here is whether it exists and
    # whether it would pass the two CI rules - pinned base, non-root final user.
    dockerfile = manifest.path.parent / "Dockerfile"
    if not dockerfile.is_file():
        print(_bad("no Dockerfile. It belongs to this repo - run `insights build --write`"))
        problems += 1
    else:
        lines = [ln.strip() for ln in dockerfile.read_text().splitlines()]
        froms = [ln.split()[1] for ln in lines if ln.upper().startswith("FROM ")]
        users = [ln.split()[1] for ln in lines if ln.upper().startswith("USER ")]
        unpinned = [i for i in froms if "${" not in i and (i.endswith(":latest") or ":" not in i)]
        if not froms:
            print(_bad("Dockerfile has no FROM"))
            problems += 1
        elif unpinned:
            print(_bad(f"Dockerfile base is not pinned: {unpinned[0]}"))
            problems += 1
        elif not users or users[-1] in ("root", "0"):
            print(_bad("Dockerfile would run as root - set a non-root USER last"))
            problems += 1
        else:
            print(_ok(f"Dockerfile on {froms[-1]}, runs as {users[-1]}"))

    # The SDK version is pyproject.toml's to state and uv.lock's to pin, and uv
    # enforces it on every build. Reporting it here is informational only - there is
    # nothing left for this command to catch that `uv sync` would not.
    print(_ok(f"sdk {__version__} (supported: {', '.join(__import__('insights_sdk').SUPPORTED_VERSIONS)})"))

    print(f"\n{'no problems' if not problems else f'{problems} problem(s)'}")
    return 1 if problems else 0


def cmd_connections(args) -> int:
    """What this app connects to, and whether it actually can.

    Replaces `insights datasets`, which listed dataset nicknames the platform would
    resolve. The platform does not resolve anything now: a team declares a connection
    and we open it, so the useful question is "will this work", answered by trying.

    `--probe` opens each connection for real. Off by default, because a command that
    silently touches production systems is a surprise nobody wants.
    """
    manifest = config.manifest()
    if not manifest.connections:
        print(f"{manifest.app} declares no connections.\n\n"
              "Add one under `connections:` in app.yaml - engine, host, and the NAME\n"
              "of a secret. The value goes in the secret store; never in the manifest.")
        return 0

    print(f"{manifest.app}")
    print(f"  unattended work runs as   {manifest.service_identity}")
    print(f"  interactive requests run as the signed-in user\n")

    problems = 0
    for spec in manifest.connections:
        print(f"  {spec.name}")
        print(f"    engine    {spec.engine}")
        for key, value in sorted(spec.options.items()):
            print(f"    {key:<9} {value}")
        if spec.secret:
            path = secrets.path_for(manifest.app, spec.secret)
            try:
                secrets.resolve(manifest.app, spec.secret)
                print(f"    secret    {spec.secret}  ->  {path}  [present]")
            except InsightsError:
                print(f"    secret    {spec.secret}  ->  {path}  [MISSING]")
                print(f"              your team sets this. The platform cannot read it.")
                problems += 1
        else:
            print(f"    secret    none")

        if args.probe:
            try:
                connectors.connect(spec.name, manifest=manifest)
                print(f"    probe     connected")
            except InsightsError as exc:
                print(f"    probe     FAILED ({getattr(exc, 'kind', 'unknown')})")
                print(f"              {str(exc).splitlines()[0]}")
                problems += 1
        print()

    if problems:
        print(f"{problems} problem(s). `insights connections --probe` opens each one for real.")
    return 1 if problems else 0


def cmd_status(args) -> int:
    """The operator view. Four facts per app, which is the minimum to run 25 of them."""
    registry = _registered()
    if not registry:
        print("no apps registered. run `insights up` to start the local platform.")
        return 0

    events = _read_sink("events")
    last_seen: dict[str, str] = {}
    for record in events:
        last_seen[record.get("app", "?")] = record["ts"]

    print(f"{'APP':<24}{'TEAM':<20}{'KIND':<6}{'SDK':<8}{'LAST SEEN':<22}STATUS")
    for name, entry in sorted(registry.items()):
        seen = last_seen.get(name, "-")
        flag = ", ".join(entry.get("connections") or ())
        state = "ok" if seen != "-" else "no telemetry"
        print(f"{name:<24}{entry['team']:<20}{entry['kind']:<6}{entry.get('sdk','-'):<8}{seen:<22}{state}  {flag}")
    return 0


def cmd_compliance_report(args) -> int:
    """The artefact you hand a compliance reviewer.

    Everything in it is read from the registry, the manifests and the audit sink -
    nothing is asserted by this command. That is the point: a report the platform
    team writes by hand is a claim, and a reviewer is right not to accept it.

    What it can show has changed with the platform. It used to list who was granted
    which dataset, because the platform brokered every read. It does not broker reads
    now, so it reports what it actually knows: which apps hold which connections,
    whose credential each one uses, and every connection failure by kind.

    What it deliberately cannot show is a row of anybody's data. There is none in the
    sink to show.
    """
    registry = _registered()
    events = _read_sink("events")

    apps = sorted(k for k in registry if not k.startswith("_"))
    if args.app:
        apps = [a for a in apps if a == args.app]

    print("CONNECTIONS AND CREDENTIALS\n")
    print(f"  {'APP':<24} {'CONNECTION':<20} {'ENGINE':<16} SECRET")
    for name in apps:
        path = Path(registry[name].get("path", "")) / "app.yaml"
        if not path.is_file():
            continue
        try:
            manifest = config.load_manifest(path)
        except InsightsError:
            continue
        for spec in manifest.connections:
            secret = secrets.path_for(name, spec.secret) if spec.secret else "-"
            print(f"  {name:<24} {spec.name:<20} {spec.engine:<16} {secret}")

    print("\nWHO CAN READ EACH SECRET")
    print("  the app's own identity (sp-<app>), on its own prefix, and the owning group.")
    print("  NOT the platform team: an explicit IAM Deny on insights/* that no Allow")
    print("  overrides. Every read is in CloudTrail, including ours.")

    failures = [r for r in events if r.get("event") == "connection_failed"]
    print(f"\nCONNECTION FAILURES IN PERIOD{'':22}{len(failures)}")
    kinds: dict[str, int] = {}
    for record in failures:
        kinds[record.get("kind", "unknown")] = kinds.get(record.get("kind", "unknown"), 0) + 1
    for kind, count in sorted(kinds.items(), key=lambda kv: -kv[1]):
        owner = "platform" if kind in ("tls", "network") else "tenant"
        print(f"  {kind:<18} {count:>4}   likely {owner}")

    queries = [r for r in events if r.get("event") == "query_executed"]
    print(f"\nQUERIES IN PERIOD{'':33}{len(queries)}")
    print("  Recorded per query: connection, engine, duration, row count.")
    print("  NOT recorded, by construction: the SQL, or any row it returned.")

    violations = [r for r in events if r.get("event") == "redaction_violation"]
    print(f"\nREDACTION ASSERTIONS{'':30}active, {len(violations)} violations")
    print(f"\nevidence read from {_registry()} and {_sink_dir()}")
    return 0


def cmd_logs(args) -> int:
    """Read what the platform recorded - process logs and telemetry, one command.

    "Where are the logs?" is the first question anyone asks when something does not
    work, and until this existed the honest answer was "scroll up in the terminal
    that is running `insights up`". That is not an answer on a platform whose whole
    claim is that operability comes for free.

    Two different things live behind one command on purpose, because the user does
    not know which one they need yet:

      --startup   the PROCESS log: uvicorn's own output, import errors, tracebacks.
                  Where an app that never came up explains itself.
      (default)   the TELEMETRY: structured records the SDK emitted. Where a running
                  app explains what it did, for whom, and how many rows it touched.

    In production these separate cleanly - process logs to CloudWatch, telemetry to
    the observability stack, audit to Unity Catalog - and this command is the local
    stand-in for all three. Same distinction, same question answered.
    """
    if args.startup:
        log_dir = _proc_log_dir()
        available = sorted(path.stem for path in log_dir.glob("*.log")) if log_dir.is_dir() else []
        if not available:
            print("no process logs. Start the stack with `insights up` first.")
            return 1
        wanted = [args.app] if args.app else available
        missing = [name for name in wanted if name not in available]
        if missing:
            print(f"no process log for {', '.join(missing)}. Running: {', '.join(available)}")
            return 1
        for name in wanted:
            raw = (log_dir / f"{name}.log").read_text().splitlines()
            # Drop the structured telemetry. The SDK writes every record to stdout
            # AS WELL AS to the sink - correct in production, where stdout is the
            # shipping path - but it means the process log contains both. The whole
            # point of --startup is the output that is NOT telemetry: uvicorn's
            # banner, an import error, a traceback. Showing both buries the one
            # line someone is looking for under a hundred they are not.
            lines = [ln for ln in raw if not ln.lstrip().startswith('{"ts"')]
            dropped = len(raw) - len(lines)
            if not lines:
                print(f"===== {name}: started cleanly, no process output =====")
            else:
                print(f"===== {name} ({len(lines)} lines) =====")
                for line in lines[-args.lines:]:
                    print(f"  {line}")
            if dropped:
                print(f"      ({dropped} telemetry record(s) hidden - see `insights logs --app {name}`)")
        return 0

    records = []
    for stream in (("events", "audit") if args.stream == "all" else (args.stream,)):
        for record in _read_sink(stream):
            records.append(record)
    if args.app:
        records = [r for r in records if r.get("app") == args.app]
    if args.event:
        records = [r for r in records if r.get("event") == args.event]
    records.sort(key=lambda r: r.get("ts", ""))

    if not records:
        where = f" for app '{args.app}'" if args.app else ""
        print(f"no telemetry{where}. Run an app, or try --startup for process logs.")
        return 0

    if args.json:
        for record in records[-args.lines:]:
            print(json.dumps(record))
        return 0

    # The default view answers "who did what" - the fields that are the same for
    # every record, every time. Anything app-specific goes in the tail, so a wide
    # record never pushes the identity off the line.
    common = {"ts", "stream", "level", "event", "env", "caller", "request_id",
              "sdk", "app", "team"}
    print(f"{'TIME':<21} {'STREAM':<7} {'APP':<20} {'CALLER':<24} EVENT")
    for record in records[-args.lines:]:
        extra = " ".join(
            f"{k}={v}" for k, v in record.items() if k not in common and v is not None
        )
        marker = "*" if record.get("stream") == "audit" else " "
        print(f"{record.get('ts',''):<21} {marker}{record.get('stream',''):<6} "
              f"{record.get('app',''):<20} {record.get('caller',''):<24} "
              f"{record.get('event','')}  {extra}")

    audits = sum(1 for r in records if r.get("stream") == "audit")
    print(f"\n{len(records)} record(s), {audits} marked * - a read of a governed dataset.")
    print(f"read from {_sink_dir()}")
    return 0


def _seed_local_warehouse(platform: Path) -> Path:
    """Make sure the local stub warehouse exists.

    A team's own warehouse, standing in for whatever they really connect to. Called by
    both `up` and `run` - it used to be called only by `up`, so a fresh clone running
    `insights run` hit a missing database. Seeding is idempotent, so the cheapest fix
    is also the right one: local dev should just work.
    """
    database = platform / "runtime" / "fakes" / "warehouse" / "warehouse.db"
    if not database.is_file():
        seed = platform / "runtime" / "fakes" / "warehouse" / "seed.py"
        subprocess.run([sys.executable, str(seed), str(database)], check=True)
    return database


def _port_free(port: int) -> bool:
    import socket

    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def cmd_up(args) -> int:
    """Start the whole local platform: warehouse, directory stub, every web app, the edge."""
    workspace, platform = _workspace(), _platform()

    # Check before starting anything. A half-started stack that fails on the third
    # process is worse than a clear refusal, and 8080 is a popular port.
    edge_port, directory_port = args.port, args.port + 1
    for label, port in (("edge", edge_port), ("directory stub", directory_port)):
        if not _port_free(port):
            print(f"port {port} ({label}) is already in use. Try `insights up --port 9000`.")
            return 1
    sys.path.insert(0, str(platform))

    # 1. seed the stub warehouse (idempotent)
    _seed_local_warehouse(platform)

    # 2. register every app in the workspace. In production this happens in CI, once per
    #    deploy; locally we discover the sibling repos so `up` is a single command.
    registry, port = {}, args.port + 21
    for manifest_path in sorted(workspace.glob("insights-*/app.yaml")):
        config.reset()
        manifest = config.load_manifest(manifest_path)
        # Every group that may reach this app at all. Since `access.roles` was
        # removed, that is exactly the union of the three management tiers - which
        # `manage.everyone` already computes, so the edge's table and `require_role`
        # cannot disagree about who is listed.
        groups = sorted(manifest.manage.everyone)
        registry[manifest.app] = {
            "team": manifest.team, "kind": manifest.kind, "sdk": __version__,
            "path": str(manifest_path.parent), "schedule": manifest.schedule,
            "connections": [c.name for c in manifest.connections], "groups": groups,
            "port": port if manifest.kind == "web" else None,
        }
        if manifest.kind == "web":
            port += 1
    # The console registers like any other app, on purpose.
    #
    # It is the platform team's own tool, and it goes through the edge, gets the same
    # session cookie and the same group check as a tenant. If the edge breaks, the
    # thing you would use to diagnose it breaks the same way - which is a far better
    # bug to have than a console that works when nothing else does.
    #
    # `groups` is empty, which the edge reads as "any signed-in user". Deliberate: a
    # tenant should be able to see their own app's health without asking us. There is
    # nothing here to gate, because there is no tenant data in it.
    registry["console"] = {
        "team": "platform", "kind": "web", "sdk": __version__,
        "path": str(platform / "runtime" / "console"), "schedule": None,
        "restricted": False, "groups": [],
        "port": port,
    }
    port += 1

    _apps_file(local=True).write_text(json.dumps(registry, indent=2) + "\n")
    print(f"registered {len(registry)} app(s) -> {_apps_file(local=True).name}")

    env = dict(os.environ)
    env.update(
        INSIGHTS_ENV="local",
        INSIGHTS_REGISTRY_DIR=str(_registry()),
        INSIGHTS_EDGE_TOKEN=env.get("INSIGHTS_EDGE_TOKEN", "local-edge-token"),
        INSIGHTS_WAREHOUSE_DSN=str(platform / "runtime" / "fakes" / "warehouse" / "warehouse.db"),
        INSIGHTS_DIRECTORY_URL=f"http://127.0.0.1:{directory_port}",
        INSIGHTS_SINK_DIR=str(_sink_dir()),
        # The local stand-in for AWS Secrets Manager. Same path shape, same
        # per-app scoping; only the thing enforcing it differs.
        INSIGHTS_SECRET_DIR=str(platform / "runtime" / "fakes" / "secret-store"),
        INSIGHTS_WAREHOUSE_PATH=str(platform / "runtime" / "fakes" / "warehouse" / "warehouse.db"),
        # In production these come from the base image. Locally the four repos are
        # not installed, so the CLI points at the same files the image would carry.
        INSIGHTS_TEMPLATES=str(platform / "runtime" / "base-image" / "design-system"),
        INSIGHTS_OUTPUT_DIR=str(platform / "runtime" / "outputs"),
        PYTHONPATH=_pythonpath(platform),
    )

    # Every child writes to its OWN file rather than the shared terminal.
    #
    # Two reasons. One, `up` used to interleave four processes' output into one
    # stream, so the first thing anyone saw was a wall of JSON from apps they had
    # not asked about. Two, "show me why my app did not start" needs the startup
    # log SEPARATE from the app's structured events - uvicorn's traceback is not a
    # telemetry record and never reaches the sink. `insights logs` reads both.
    log_dir = _proc_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    handles: list = []

    def _spawn(name: str, argv: list[str], cwd: Path | None, proc_env: dict):
        handle = (log_dir / f"{name}.log").open("w")
        handles.append(handle)
        return subprocess.Popen(
            argv, cwd=cwd, env=proc_env, stdout=handle, stderr=subprocess.STDOUT
        )

    procs = [_spawn(
        "directory-stub",
        [sys.executable, str(platform / "runtime" / "fakes" / "directory" / "serve.py"), str(directory_port)],
        None, env,
    )]

    for name, entry in registry.items():
        if entry["kind"] != "web":
            continue
        # A tenant app's code is in <repo>/src; the console lives directly in
        # runtime/console and has no manifest of its own to point at.
        source = Path(entry["path"]) if name == "console" else Path(entry["path"]) / "src"
        app_env = dict(env, INSIGHTS_APP=name)
        if name != "console":
            app_env["INSIGHTS_APP_MANIFEST"] = str(Path(entry["path"]) / "app.yaml")
        procs.append(_spawn(
            name,
            [sys.executable, "-m", "uvicorn", "main:app", "--port", str(entry["port"]), "--log-level", "warning"],
            source, app_env,
        ))
        print(f"  {name} on :{entry['port']}")

    procs.append(_spawn(
        "edge",
        [sys.executable, "-m", "uvicorn", "runtime.edge.main:app", "--port", str(edge_port),
         "--log-level", "warning"],
        platform, env,
    ))

    # Wait for each app to bind before telling anyone the URLs. `up` used to return
    # as soon as the processes were spawned, so the very first curl in the README
    # returned 500 - the edge was listening, the app was not. A first-run 500 is an
    # expensive way to greet someone.
    import urllib.error
    import urllib.request

    for name, entry in registry.items():
        if entry["kind"] != "web":
            continue
        for _ in range(60):                      # 15s, 250ms apart
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{entry['port']}/healthz", timeout=1)
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.25)
        else:
            print(f"  ! {name} did not become healthy - try `insights doctor` in {entry['path']}")

    print(
        f"\nedge on http://localhost:{edge_port}\n"
        f"  the console     http://localhost:{edge_port}/a/console/?as=suraj@corp.example\n"
        f"  the dashboard   http://localhost:{edge_port}/a/headcount-dashboard/?as=krishna@corp.example\n"
        f"  who am I        http://localhost:{edge_port}/a/headcount-dashboard/api/me\n"
        f"  the data        http://localhost:{edge_port}/a/headcount-dashboard/api/headcount?month=2026-09\n"
        f"\n  the ?as= is only on the FIRST url - it stands in for the IdP redirect and sets\n"
        f"  a session cookie. Without it every route returns 401, which is the point.\n"
        f"\nlogs     insights logs --app headcount-dashboard --startup   (why it did or did not boot)\n"
        f"         insights logs --app headcount-dashboard             (what it did once running)\n"
        f"         insights logs --event query_executed                 (every query, by connection)\n"
        f"\nctrl-c to stop"
    )
    # Tear the stack down on ANY exit, not just ctrl-c.
    #
    # This used to catch KeyboardInterrupt only, so `kill` on the parent - or any
    # exception in here - left the directory stub, both app servers and the edge
    # running and holding their ports. The next `insights up` then refused to start
    # because 8080 was busy, blaming the user for processes it had orphaned itself.
    # A supervisor that does not clean up is worse than no supervisor.
    def _shutdown() -> None:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:                       # give each one a moment to go quietly
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        for handle in handles:
            handle.close()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # so `kill` reaches the finally
    try:
        while True:
            if any(proc.poll() is not None for proc in procs):
                print("\na process exited - shutting the rest of the stack down")
                return 1
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown()
    return 0


def cmd_run(args) -> int:
    """Run the current app the way the platform runs it - same identity, same env."""
    manifest = config.manifest()
    platform = _platform()
    _seed_local_warehouse(platform)          # a fresh clone has no database yet
    env = dict(os.environ)
    env.update(
        INSIGHTS_ENV="local",
        INSIGHTS_REGISTRY_DIR=str(_registry()),
        INSIGHTS_EDGE_TOKEN=env.get("INSIGHTS_EDGE_TOKEN", "local-edge-token"),
        INSIGHTS_WAREHOUSE_DSN=str(platform / "runtime" / "fakes" / "warehouse" / "warehouse.db"),
        INSIGHTS_DIRECTORY_URL="http://127.0.0.1:8081",
        INSIGHTS_SINK_DIR=str(_sink_dir()),
        # The local stand-in for AWS Secrets Manager. Same path shape, same
        # per-app scoping; only the thing enforcing it differs.
        INSIGHTS_SECRET_DIR=str(platform / "runtime" / "fakes" / "secret-store"),
        INSIGHTS_WAREHOUSE_PATH=str(platform / "runtime" / "fakes" / "warehouse" / "warehouse.db"),
        INSIGHTS_APP_MANIFEST=str(manifest.path),
        INSIGHTS_APP=manifest.app,
        INSIGHTS_TEMPLATES=str(platform / "runtime" / "base-image" / "design-system"),
        INSIGHTS_OUTPUT_DIR=str(platform / "runtime" / "outputs"),
        PYTHONPATH=_pythonpath(platform),
    )
    if manifest.kind == "job":
        return subprocess.run([sys.executable, "src/main.py"], env=env).returncode
    print("this is a web app - start the platform with `insights up` instead")
    return 1


# --------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="insights", description="Insights Hub platform CLI")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new-app", help="generate a new app")
    new.add_argument("name")
    new.add_argument("--kind", choices=["web", "job"], required=True)
    new.add_argument("--team", required=True)
    new.add_argument("--owner", required=True, help="corporate group, e.g. MG-YOUR-TEAM")
    new.add_argument("--directory")
    new.set_defaults(func=cmd_new_app)

    sub.add_parser("doctor", help="check this app the way CI will").set_defaults(func=cmd_doctor)

    build = sub.add_parser("build", help="show or run the container build for this app")
    build.add_argument("--show", action="store_true", help="print the Dockerfile and exit")
    build.add_argument("--write", action="store_true",
                       help="write the generated Dockerfile into this repo (it is yours after that)")
    build.add_argument("--force", action="store_true", help="with --write, overwrite an existing one")
    build.set_defaults(func=cmd_build)

    upgrade = sub.add_parser("upgrade-scaffold", help="re-render the platform-owned files in this repo")
    upgrade.add_argument("--check", action="store_true", help="report drift without writing")
    upgrade.set_defaults(func=cmd_upgrade_scaffold)
    sub.add_parser("status", help="every registered app").set_defaults(func=cmd_status)
    connections = sub.add_parser("connections", help="what this app connects to, and whether it can")
    connections.add_argument("--probe", action="store_true",
                             help="actually open each connection (touches real systems)")
    connections.set_defaults(func=cmd_connections)

    logs = sub.add_parser("logs", help="process logs and telemetry for a local app")
    logs.add_argument("--app", help="only this app")
    logs.add_argument("--startup", action="store_true",
                      help="the process log (uvicorn, import errors) instead of telemetry")
    logs.add_argument("--stream", default="all", choices=("all", "events", "audit"))
    logs.add_argument("--event", help="only this event name, e.g. query_executed")
    logs.add_argument("-n", "--lines", type=int, default=40)
    logs.add_argument("--json", action="store_true", help="raw records, for piping to jq")
    logs.set_defaults(func=cmd_logs)

    up = sub.add_parser("up", help="start the local platform")
    up.add_argument("--port", type=int, default=8080,
                    help="edge port (the stubs and apps take the next few). Default 8080")
    up.set_defaults(func=cmd_up)
    sub.add_parser("run", help="run this app locally, as the platform would").set_defaults(func=cmd_run)

    report = sub.add_parser("compliance-report", help="evidence for a reviewer")
    report.add_argument("--app", help="limit to one app")
    report.set_defaults(func=cmd_compliance_report)


    return parser


def _local_defaults() -> None:
    """Set the environment the platform injects at deploy, for local commands.

    `up` and `run` already did this, which meant `insights doctor` failed on a
    manifest referring to ${INSIGHTS_DIRECTORY_URL} - correct behaviour at the wrong
    moment, since in CI and in production that variable IS set. Doctor's whole claim
    is that it checks what CI checks, so it has to run in the same environment CI
    does.

    Only ever defaults: anything already exported wins, so pointing at a sandbox
    still works.

    Silently does nothing outside the workspace. `insights new-app` runs from an
    empty directory by definition - there is no project yet, that is the point of it -
    and an earlier version of this function raised there, so the very first command a
    new team runs failed with "Run this from inside the insights-hub workspace".
    """
    try:
        platform = _platform()
    except InsightsError:
        return
    for key, value in (
        ("INSIGHTS_ENV", "local"),
        ("INSIGHTS_REGISTRY_DIR", str(_registry())),
        ("INSIGHTS_SECRET_DIR", str(platform / "runtime" / "fakes" / "secret-store")),
        ("INSIGHTS_DIRECTORY_URL", "http://127.0.0.1:8081"),
        ("INSIGHTS_WAREHOUSE_PATH",
         str(platform / "runtime" / "fakes" / "warehouse" / "warehouse.db")),
        ("INSIGHTS_SINK_DIR", str(_sink_dir())),
        ("INSIGHTS_OUTPUT_DIR", str(platform / "runtime" / "outputs")),
    ):
        os.environ.setdefault(key, value)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _local_defaults()
        return args.func(args)
    except InsightsError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
