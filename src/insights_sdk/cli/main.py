"""`insights` - the one command a tenant team learns.

Design rule for this CLI: **every command either generates something correct, or shows
you evidence.** There is no command that explains a rule, because a rule that needs
explaining is enforced in the wrong place (ADR-004).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__, config
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


def _apps_file() -> Path:
    return _registry() / "apps.json"


def _registered() -> dict:
    path = _apps_file()
    return json.loads(path.read_text()) if path.is_file() else {}


def _pythonpath(platform: Path) -> str:
    """Local dev only.

    In production the base image pip-installs the SDK, so there is no path juggling.
    Locally the four repos sit side by side and are not installed, so we put the SDK
    source and the platform root on the path for child processes. This is the one place
    the local loop differs from production, and it is deliberately in the CLI rather
    than in anything a tenant writes.
    """
    return os.pathsep.join(
        [str(platform.parent / "insights-sdk" / "src"), str(platform), os.environ.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)


def _sink_dir() -> Path:
    return _platform() / "runtime" / "sinks"


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
        "  insights datasets        # what data you can ask for\n"
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

    try:
        catalog = config.catalog()
        grants = config.grants()
    except InsightsError as exc:
        print(_bad(f"registry: {exc}"))
        return 1

    for request in manifest.datasets:
        try:
            resolved = catalog.resolve(request.dataset)
        except InsightsError as exc:
            print(_bad(f"dataset {request.dataset}: {exc}"))
            problems += 1
            continue
        if resolved.restricted:
            grant = grants.for_app(request.dataset, manifest.app)
            if grant:
                print(_ok(f"dataset {request.dataset} (restricted) granted by {grant['granted_by']}"))
            else:
                print(_bad(f"dataset {request.dataset} is restricted and not granted to {manifest.app}"))
                print(f"        run: insights access request --dataset {request.dataset}")
                problems += 1
        else:
            print(_ok(f"dataset {request.dataset} ({resolved.dataset.classification})"))

    if manifest.sdk_floor and "==" not in manifest.sdk_floor:
        print(_ok(f"sdk floor {manifest.sdk_floor} (supported: {', '.join(__import__('insights_sdk').SUPPORTED_VERSIONS)})"))

    print(f"\n{'no problems' if not problems else f'{problems} problem(s)'}")
    return 1 if problems else 0


def cmd_datasets(args) -> int:
    """What this app can read, and what it could ask for.

    Deliberately NOT a data catalog: a name, an owner and a one-line description, and
    only for datasets whose owner has made them requestable. There is no search, no
    schema and no sample data here, and that is a decision rather than an omission
    (ADR-002 s5).
    """
    manifest = config.manifest()
    catalog = config.catalog()
    grants = config.grants()

    print(f"datasets for {manifest.app}\n")
    print("  ENTITLED")
    for request in manifest.datasets:
        resolved = catalog.resolve(request.dataset)
        note = ""
        if resolved.restricted:
            grant = grants.for_app(request.dataset, manifest.app)
            note = f"  granted {grant['granted_at'][:10]}" if grant else "  NOT GRANTED"
        print(f"    {request.dataset:<22} {resolved.dataset.classification:<12} {resolved.dataset.owner}{note}")

    others = [name for name in sorted(catalog.datasets) if not manifest.declares(name)]
    if others:
        print("\n  AVAILABLE TO REQUEST  (ask the owner - the platform does not decide this)")
        for name in others:
            dataset = catalog.datasets[name]
            print(f"    {name:<22} {dataset.classification:<12} {dataset.owner}")
            print(f"    {'':22} {dataset.description}")
    return 0


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
        flag = "restricted" if entry.get("restricted") else ""
        state = "ok" if seen != "-" else "no telemetry"
        print(f"{name:<24}{entry['team']:<20}{entry['kind']:<6}{entry.get('sdk','-'):<8}{seen:<22}{state}  {flag}")
    return 0


def cmd_compliance_report(args) -> int:
    """The artefact you hand a compliance reviewer. Everything in it is read from the
    registry and the audit sink - nothing is asserted by this command itself."""
    catalog = config.catalog()
    grants = config.grants()
    dataset_name = args.dataset
    dataset = catalog.datasets.get(dataset_name)
    if dataset is None:
        print(f"no dataset '{dataset_name}'")
        return 1

    print(f"DATASET  {dataset_name}      classification: {dataset.classification}     owner: {dataset.owner}\n")

    print("APPS WITH ACCESS")
    granted = [g for g in grants.grants if g["dataset"] == dataset_name]
    if not granted:
        print("  (none)")
    for grant in granted:
        print(f"  {grant['app']:<28} granted {grant['granted_at'][:10]} by {grant['granted_by']}")

    reads = [r for r in _read_sink("audit") if r.get("dataset") == dataset_name]
    print(f"\nACCESS IN PERIOD{'':38}{len(reads)} reads")
    for record in reads[-10:]:
        print(f"  {record['ts']}  {record.get('app','?'):<22} {record.get('caller','?'):<24} {record.get('rows',0)} rows")

    standing = [g for g in granted if g.get("operator")]
    print(f"\nOPERATOR ACCESS{'':39}{len(standing)} standing · {len(grants.break_glass)} break-glass")
    now = datetime.now(timezone.utc)
    for entry in grants.break_glass:
        if entry["dataset"] != dataset_name:
            continue
        expired = "expired" if not grants._active(entry, now) else "ACTIVE"
        print(f"  {entry['requested_at'][:10]}  {entry['operator']}  approved by {entry['approved_by']}  [{expired}]")
        print(f"              used {entry.get('used', 0)}x · tenant notified: {entry.get('tenant_notified')}")

    violations = [r for r in _read_sink("events") if r.get("event") == "redaction_violation"]
    print(f"\nREDACTION ASSERTIONS{'':34}active, {len(violations)} violations")
    print(f"\nevidence read from {_registry()} and {_sink_dir()}")
    return 0


def cmd_access(args) -> int:
    """Request or approve a grant. The second key for restricted data (ADR-002 s4)."""
    registry_file = _registry() / "grants.yaml"
    import yaml

    body = yaml.safe_load(registry_file.read_text()) if registry_file.is_file() else {"grants": [], "break_glass": []}

    if args.access_command == "request":
        manifest = config.manifest()
        dataset = config.catalog().datasets[args.dataset]
        print(
            f"access request\n"
            f"  dataset : {args.dataset} ({dataset.classification})\n"
            f"  app     : {manifest.app} ({manifest.team})\n"
            f"  approver: {dataset.owner}\n"
            f"  reason  : {args.reason}\n\n"
            f"The platform team cannot approve this. Send it to {dataset.owner}; they run:\n"
            f"  insights access approve --dataset {args.dataset} --app {manifest.app} --as <you>"
        )
        return 0

    if args.access_command == "approve":
        body.setdefault("grants", []).append(
            {
                "dataset": args.dataset,
                "app": args.app,
                "access": "read",
                "granted_by": args.approver,
                "granted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "expires_at": None,
                "reason": args.reason,
            }
        )
        registry_file.write_text(yaml.safe_dump(body, sort_keys=False))
        print(f"granted {args.dataset} to {args.app}, recorded in {registry_file}")
        return 0
    return 1


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

    # 1. seed the stub warehouse
    seed = platform / "runtime" / "fakes" / "warehouse" / "seed.py"
    subprocess.run([sys.executable, str(seed)], check=True)

    # 2. register every app in the workspace. In production this happens in CI, once per
    #    deploy; locally we discover the sibling repos so `up` is a single command.
    registry, port = {}, args.port + 21
    for manifest_path in sorted(workspace.glob("insights-*/app.yaml")):
        config.reset()
        manifest = config.load_manifest(manifest_path)
        restricted = any(
            config.load_catalog(_registry() / "catalog.yaml").datasets[d.dataset].restricted
            for d in manifest.datasets
        )
        registry[manifest.app] = {
            "team": manifest.team, "kind": manifest.kind, "sdk": __version__,
            "path": str(manifest_path.parent), "schedule": manifest.schedule,
            "restricted": restricted,
            "port": port if manifest.kind == "web" else None,
        }
        if manifest.kind == "web":
            port += 1
    _apps_file().write_text(json.dumps(registry, indent=2) + "\n")
    print(f"registered {len(registry)} app(s) -> {_apps_file()}")

    env = dict(os.environ)
    env.update(
        INSIGHTS_ENV="local",
        INSIGHTS_REGISTRY_DIR=str(_registry()),
        INSIGHTS_EDGE_TOKEN=env.get("INSIGHTS_EDGE_TOKEN", "local-edge-token"),
        INSIGHTS_WAREHOUSE_DSN=str(platform / "runtime" / "fakes" / "warehouse" / "warehouse.db"),
        INSIGHTS_DIRECTORY_URL=f"http://127.0.0.1:{directory_port}",
        INSIGHTS_SINK_DIR=str(_sink_dir()),
        PYTHONPATH=_pythonpath(platform),
    )

    procs = [subprocess.Popen(
        [sys.executable, str(platform / "runtime" / "fakes" / "directory" / "serve.py"), str(directory_port)],
        env=env)]

    for name, entry in registry.items():
        if entry["kind"] != "web":
            continue
        app_env = dict(env, INSIGHTS_APP_MANIFEST=str(Path(entry["path"]) / "app.yaml"), INSIGHTS_APP=name)
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--port", str(entry["port"]), "--log-level", "warning"],
            cwd=Path(entry["path"]) / "src", env=app_env))
        print(f"  {name} on :{entry['port']}")

    procs.append(subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "runtime.edge.main:app", "--port", str(edge_port),
         "--log-level", "warning"],
        cwd=platform, env=env))

    print(
        f"\nedge on http://localhost:{edge_port}\n"
        f"  http://localhost:{edge_port}/a/headcount-dashboard/?as=dana@corp.example\n"
        f"  http://localhost:{edge_port}/a/headcount-dashboard/headcount?month=2026-09\n"
        f"\nctrl-c to stop"
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        for proc in procs:
            proc.terminate()
    return 0


def cmd_run(args) -> int:
    """Run the current app the way the platform runs it - same identity, same env."""
    manifest = config.manifest()
    platform = _platform()
    env = dict(os.environ)
    env.update(
        INSIGHTS_ENV="local",
        INSIGHTS_REGISTRY_DIR=str(_registry()),
        INSIGHTS_EDGE_TOKEN=env.get("INSIGHTS_EDGE_TOKEN", "local-edge-token"),
        INSIGHTS_WAREHOUSE_DSN=str(platform / "runtime" / "fakes" / "warehouse" / "warehouse.db"),
        INSIGHTS_DIRECTORY_URL="http://127.0.0.1:8081",
        INSIGHTS_SINK_DIR=str(_sink_dir()),
        INSIGHTS_APP_MANIFEST=str(manifest.path),
        INSIGHTS_APP=manifest.app,
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

    upgrade = sub.add_parser("upgrade-scaffold", help="re-render the platform-owned files in this repo")
    upgrade.add_argument("--check", action="store_true", help="report drift without writing")
    upgrade.set_defaults(func=cmd_upgrade_scaffold)
    sub.add_parser("datasets", help="what this app can read, and what it could ask for").set_defaults(func=cmd_datasets)
    sub.add_parser("status", help="every registered app").set_defaults(func=cmd_status)
    up = sub.add_parser("up", help="start the local platform")
    up.add_argument("--port", type=int, default=8080,
                    help="edge port (the stubs and apps take the next few). Default 8080")
    up.set_defaults(func=cmd_up)
    sub.add_parser("run", help="run this app locally, as the platform would").set_defaults(func=cmd_run)

    report = sub.add_parser("compliance-report", help="evidence for a reviewer")
    report.add_argument("--dataset", required=True)
    report.set_defaults(func=cmd_compliance_report)

    access = sub.add_parser("access", help="request or approve access to a dataset")
    access_sub = access.add_subparsers(dest="access_command", required=True)
    req = access_sub.add_parser("request")
    req.add_argument("--dataset", required=True)
    req.add_argument("--reason", default="(no reason given)")
    app_ = access_sub.add_parser("approve")
    app_.add_argument("--dataset", required=True)
    app_.add_argument("--app", required=True)
    app_.add_argument("--approver", required=True)
    app_.add_argument("--reason", default="")
    access.set_defaults(func=cmd_access)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except InsightsError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
