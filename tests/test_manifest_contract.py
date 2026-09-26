"""Evidence that app.yaml is a contract rather than a document.

Every rule here exists because getting it wrong would be silent: a manifest that
parses but means something different from what the author intended is worse than
one that fails.
"""

import os

import pytest
import yaml

from insights_sdk import config
from insights_sdk.errors import ManifestError

from conftest import FIXTURES


def write(tmp_path, body: dict):
    path = tmp_path / "app.yaml"
    path.write_text(yaml.safe_dump(body, sort_keys=False))
    return path


BASE_WEB = {
    "apiVersion": "v1", "app": "x", "team": "t", "kind": "web",
    "access": {"manage": {"owners": ["MG-T"]}},
    "runtime": {"size": "small"},
    "web": {"route": "/x", "type": "spa"},
}


# --- the control plane / data plane split ------------------------------------

def test_the_access_tiers_nest(platform, as_app):
    """Three tiers, and they nest: an owner satisfies every check a reader does.

    The alternative is every team listing their owners in three places, and
    forgetting once is a lockout that looks like a platform bug.
    """
    as_app("app-web.yaml")
    m = config.manifest()

    assert m.manage.owners == ("MG-PEOPLE-OPS",)
    assert m.manage.contributors == ("MG-PEOPLE-OPS-ENG",)

    assert m.manage.groups_for("owner") == ("MG-PEOPLE-OPS",)
    assert set(m.manage.groups_for("contributor")) == {"MG-PEOPLE-OPS", "MG-PEOPLE-OPS-ENG"}
    assert set(m.manage.groups_for("reader")) >= set(m.manage.groups_for("contributor"))


def test_an_app_defined_role_block_is_refused(platform, tmp_path):
    """Refused rather than ignored. A silently-dropped roles block is a team
    believing an authorization rule is in force when it is not."""
    body = {**BASE_WEB, "access": {"manage": {"owners": ["MG-T"]},
                                   "roles": [{"name": "viewer", "groups": ["MG-T"]}]}}
    with pytest.raises(ManifestError, match="access.roles"):
        config.load_manifest(write(tmp_path, body))


def test_a_fourth_tier_does_not_exist(platform, as_app):
    as_app("app-web.yaml")
    with pytest.raises(Exception, match="three"):
        config.manifest().manage.groups_for("superuser")


def test_an_app_must_have_an_owner_group(platform, tmp_path):
    body = {**BASE_WEB, "access": {"manage": {}}}
    with pytest.raises(ManifestError, match="owners is required"):
        config.load_manifest(write(tmp_path, body))


def test_owners_must_be_groups_not_people(platform, tmp_path):
    """An app owned by someone who left is an orphan."""
    body = {**BASE_WEB, "access": {"manage": {"owners": ["krishna@corp.example"]}, "roles": []}}
    with pytest.raises(ManifestError, match="individual"):
        config.load_manifest(write(tmp_path, body))


# --- kind has to mean something ----------------------------------------------

def test_a_job_must_declare_a_schedule(platform, tmp_path):
    body = {"apiVersion": "v1", "app": "x", "team": "t", "kind": "job",
            "access": {"manage": {"owners": ["MG-T"]}}, "runtime": {"size": "small"}}
    with pytest.raises(ManifestError, match="job.schedule"):
        config.load_manifest(write(tmp_path, body))


def test_a_web_app_cannot_declare_a_job_block(platform, tmp_path):
    body = {**BASE_WEB, "job": {"schedule": "0 6 * * *"}}
    with pytest.raises(ManifestError, match="must not declare a job"):
        config.load_manifest(write(tmp_path, body))


def test_job_defaults_are_the_safe_ones(platform, tmp_path):
    """Unspecified means safe: skip if the last run is still going, and do not
    fire a burst of missed runs after an outage."""
    body = {"apiVersion": "v1", "app": "x", "team": "t", "kind": "job",
            "access": {"manage": {"owners": ["MG-T"]}}, "runtime": {"size": "small"},
            "job": {"schedule": "0 6 * * *"}}
    m = config.load_manifest(write(tmp_path, body))
    assert m.job.concurrency == "forbid"
    assert m.job.catchup is False


# --- the four web shapes ------------------------------------------------------

def test_every_supported_web_shape_parses(platform, tmp_path):
    for shape in ("api", "spa"):
        body = {**BASE_WEB, "web": {"route": "/x", "type": shape}}
        assert config.load_manifest(write(tmp_path, body)).web_type == shape


def test_an_unsupported_web_shape_is_refused(platform, tmp_path):
    """Adding a shape is a platform change - a base image, an identity shim and a
    health contract - not something a tenant can assert into existence."""
    body = {**BASE_WEB, "web": {"route": "/x", "type": "django"}}
    with pytest.raises(ManifestError, match="web.type"):
        config.load_manifest(write(tmp_path, body))


def test_a_shape_the_platform_cannot_deliver_is_refused_early(platform, tmp_path):
    """Streamlit is genuinely wanted and is deliberately NOT accepted.

    It needs an identity shim and a health sidecar that do not exist. Accepting it in
    the manifest would move the failure from `insights doctor` on a laptop to a deploy
    in an environment - the wrong layer (ADR-004). A platform should refuse what it
    cannot deliver, at the cheapest possible moment.
    """
    body = {**BASE_WEB, "web": {"route": "/x", "type": "streamlit"}}
    with pytest.raises(ManifestError, match="web.type"):
        config.load_manifest(write(tmp_path, body))

    body = {**BASE_WEB, "web": {"route": "/x", "type": "streamlit"}}
    with pytest.raises(ManifestError, match="web.type must be one of"):
        config.load_manifest(write(tmp_path, body))


@pytest.mark.parametrize("removed", ["sdk", "base"])
def test_runtime_sdk_and_base_are_refused(platform, tmp_path, removed):
    """Both were second copies of something stated elsewhere - pyproject.toml pins the
    SDK, the Dockerfile's FROM names the base - and a second copy drifts. Refused with
    a message saying where the real one lives, rather than ignored."""
    body = {**BASE_WEB, "runtime": {"size": "small", removed: "anything"}}
    with pytest.raises(ManifestError, match=f"runtime.{removed} was removed"):
        config.load_manifest(write(tmp_path, body))


# --- declare, don't wire ------------------------------------------------------

def test_mechanism_words_are_refused_at_any_depth(platform, tmp_path):
    """A tenant declares intent. Naming a table, an engine or a classification is
    naming mechanism, and the loader refuses the file rather than ignoring the key."""
    for key in ("classification", "connection", "engine", "credential", "dsn", "table"):
        body = {**BASE_WEB, "data": [{"dataset": "hr.headcount", "access": "read", key: "x"}]}
        with pytest.raises(ManifestError, match=key):
            config.load_manifest(write(tmp_path, body))


# --- regressions found by review, locked in -----------------------------------

def test_the_sdk_floor_gate_can_actually_fail(platform, tmp_path, monkeypatch):
    """The reuse/upgrade story's enforcement point.

    This gate once ended in `or True`, so `>=9.9,<10` -- and even `>=banana` -- passed.
    A gate that cannot fail is worse than no gate: it is a control everyone believes in.

    It reads pyproject.toml now. `runtime.sdk` in app.yaml was removed - uv.lock
    already pins the resolved version and uv enforces it on every build, so a range in
    the manifest was a second copy that could disagree with the lockfile unnoticed.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    gates = Path(__file__).resolve().parents[2] / "insights-platform" / "control" / "cli" / "gates.py"
    if not gates.is_file():
        pytest.skip("platform repo not checked out beside the SDK")

    write(tmp_path, {**BASE_WEB, "app": "x"})
    (tmp_path / "Dockerfile").write_text(
        "FROM insights-hub/python-web:0.1\nCOPY src/ /app/src/\n"
    )

    def run(floor: str) -> int:
        (tmp_path / "pyproject.toml").write_text(
            f'[project]\nname = "x"\nversion = "0.1.0"\n'
            f'dependencies = ["insights-sdk{floor}"]\n'
        )
        return subprocess.run(
            [sys.executable, str(gates), "--app", "x", "--manifest", str(tmp_path / "app.yaml")],
            cwd=tmp_path, capture_output=True,
            env={**os.environ, "INSIGHTS_REGISTRY_DIR": str(FIXTURES)},
        ).returncode

    assert run(">=0.1,<1") == 0         # supported
    assert run(">=9.9,<10") != 0        # no supported version satisfies it
    assert run("==0.1.0") != 0          # a pin freezes the app; uv.lock is what pins
