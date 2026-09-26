"""The generator - the cheapest enforcement layer the platform has.

ADR-004's rule is "enforce at the earliest layer that makes a rule impossible to get
wrong", and generation is the earliest layer there is: a Dockerfile that is generated
correctly cannot be a Dockerfile someone got wrong.

The templates live in the SDK, next to the library they scaffold, so the two versions
can never skew. And they are GENERATED, not cloned from a template repo: an app created
eighteen months ago carries an eighteen-month-old scaffold with nothing to tell anyone,
which is exactly the failure ADR-001 is about.
"""

from __future__ import annotations

from pathlib import Path

MANIFEST = """\
# {name} - the tenant's entire declaration to the platform.
#
# You declare INTENT. The platform decides mechanism: which engine, which credential,
# which physical table, how sensitive the data is. That is why there is nothing in here
# resembling a connection string - and why the loader will reject one if you add it.
apiVersion: v1
app: {name}
team: {team}
kind: {kind}
{schedule}
owners:
  - {owner}                  # corporate group. Becomes {name}-admin at deploy time.

runtime:
  sdk: ">=0.1,<1"            # a FLOOR, not a pin. Patches and minors reach you automatically.

data: []                     # add dataset aliases here; run `insights datasets` to see them

access:
  roles: []                  # who may use this app, beyond platform membership
"""

WEB_MAIN = '''\
"""{name} - a web app on Insights Hub."""

from insights_sdk import current_user, get_logger, query, web_app

app = web_app()
log = get_logger()


@app.get("/")
def index() -> dict:
    caller = current_user()
    return {{"app": "{name}", "you": caller.subject}}


# Add a dataset to app.yaml, then read it like this. No connection, no credential:
#
#     @app.get("/rows")
#     def rows() -> dict:
#         data = query("your.dataset", "SELECT * FROM your.dataset LIMIT 10")
#         log.info("read", rows=len(data))     # log the SHAPE, never the rows
#         return {{"rows": data}}
'''

JOB_MAIN = '''\
"""{name} - a scheduled job on Insights Hub.

You do not run a scheduler. You declared a schedule in app.yaml; the platform runs you.
"""

from insights_sdk import get_logger, run_job

log = get_logger()


def main() -> None:
    log.info("hello", note="replace me")

    # Add a dataset to app.yaml, then read it like this:
    #
    #     rows = query("your.dataset", "SELECT * FROM your.dataset")
    #     log.info("done", rows=len(rows))     # log the SHAPE, never the rows


if __name__ == "__main__":
    raise SystemExit(run_job(main))
'''

DOCKERFILE = """\
# Generated. Three lines, and none of them are yours to maintain.
# The base image carries the runtime, the SDK, the non-root user and the entrypoint -
# so a platform CVE is one base-image rebuild, not {n} pull requests.
FROM insights-hub/base:0.1
COPY src/ /app/src/
COPY app.yaml /app/app.yaml
"""

CI = """\
# Generated. Do not edit.
#
# Four lines of tenant-owned CI. Manifest validation, entitlement checks against the
# catalog, the SDK support-window check, build, registration and deploy all live in the
# platform's reusable workflow - so improving the pipeline is one commit, not {n}.
#
# You own your code. We own the road it travels on.
name: ci
on: [push, pull_request]

jobs:
  deploy:
    uses: insights-hub/insights-platform/.github/workflows/deploy.yml@v1
    with:
      app: {name}
"""

README = """\
# {name}

A {kind} app on Insights Hub, owned by **{team}**.

| | |
|---|---|
| What we wrote | `src/main.py` and `app.yaml` |
| What we inherited | auth, data access, logging, health, deployment, the base image |

## Day one

```bash
insights doctor      # check the manifest the way CI will
insights datasets    # what data you can ask for
insights run         # run it locally, with the platform's identity and env
```

Everything here except `src/` and `app.yaml` was generated and is not yours to maintain.
When the platform improves them, you get the change by upgrading the SDK.
"""

GITIGNORE = "__pycache__/\n*.py[cod]\n.venv/\n.pytest_cache/\n"


#: Files the PLATFORM owns inside a tenant repository. These are re-rendered by
#: `insights upgrade-scaffold`; everything else in a tenant repo is the tenant's.
#: This list is the machine-readable version of "you own your code, we own the road
#: it travels on" - and the reason generated-not-cloned matters: a cloned template can
#: never be refreshed, because nothing knows which files came from it.
PLATFORM_OWNED = (Path("Dockerfile"), Path(".github/workflows/ci.yml"))


def render_platform_owned(name: str) -> dict[Path, str]:
    return {
        Path("Dockerfile"): DOCKERFILE.format(n=12),
        Path(".github/workflows/ci.yml"): CI.format(name=name, n=12),
    }


def generate(*, target: Path, name: str, kind: str, team: str, owner: str) -> list[Path]:
    if target.exists() and any(target.iterdir()):
        raise SystemExit(f"{target} already exists and is not empty")

    schedule = '\nschedule: "0 6 * * MON"        # jobs only. Cron, UTC.\n' if kind == "job" else ""
    files = {
        Path("app.yaml"): MANIFEST.format(name=name, team=team, kind=kind, owner=owner, schedule=schedule),
        Path("src/main.py"): (JOB_MAIN if kind == "job" else WEB_MAIN).format(name=name),
        Path("Dockerfile"): DOCKERFILE.format(n=12),
        Path(".github/workflows/ci.yml"): CI.format(name=name, n=12),
        Path("README.md"): README.format(name=name, kind=kind, team=team),
        Path(".gitignore"): GITIGNORE,
    }
    written = []
    for relative, content in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        written.append(path)
    return written
