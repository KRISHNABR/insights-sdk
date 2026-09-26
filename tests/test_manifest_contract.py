"""Evidence that app.yaml is a contract rather than a document.

Every rule here exists because getting it wrong would be silent: a manifest that
parses but means something different from what the author intended is worse than
one that fails.
"""

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
    "access": {"manage": {"owners": ["MG-T"]}, "roles": []},
    "runtime": {"sdk": ">=0.1,<1", "base": "python-web"},
    "web": {"route": "/x", "type": "spa"},
}


# --- the control plane / data plane split ------------------------------------

def test_the_two_access_planes_are_parsed_separately(platform, as_app):
    """Deploying an app and reading its data are different permissions."""
    as_app("app-web.yaml")
    m = config.manifest()

    assert m.manage.owners == ("MG-PEOPLE-OPS",)              # control plane
    assert m.manage.contributors == ("MG-PEOPLE-OPS-ENG",)
    assert m.role_names == ("headcount-viewer",)              # data plane
    # A contributor can deploy to uat. That says nothing about what they may read.
    assert "MG-PEOPLE-OPS-ENG" not in [g for r in m.roles for g in r.groups]


def test_an_app_must_have_an_owner_group(platform, tmp_path):
    body = {**BASE_WEB, "access": {"manage": {}, "roles": []}}
    with pytest.raises(ManifestError, match="owners is required"):
        config.load_manifest(write(tmp_path, body))


def test_owners_must_be_groups_not_people(platform, tmp_path):
    """An app owned by someone who left is an orphan."""
    body = {**BASE_WEB, "access": {"manage": {"owners": ["dana@corp.example"]}, "roles": []}}
    with pytest.raises(ManifestError, match="individual"):
        config.load_manifest(write(tmp_path, body))


# --- kind has to mean something ----------------------------------------------

def test_a_job_must_declare_a_schedule(platform, tmp_path):
    body = {"apiVersion": "v1", "app": "x", "team": "t", "kind": "job",
            "access": {"manage": {"owners": ["MG-T"]}}, "runtime": {"sdk": ">=0.1,<1"}}
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
            "access": {"manage": {"owners": ["MG-T"]}}, "runtime": {"sdk": ">=0.1,<1"},
            "job": {"schedule": "0 6 * * *"}}
    m = config.load_manifest(write(tmp_path, body))
    assert m.job.concurrency == "forbid"
    assert m.job.catchup is False


# --- the four web shapes ------------------------------------------------------

def test_every_supported_web_shape_parses(platform, tmp_path):
    for shape in ("api", "spa", "streamlit"):
        body = {**BASE_WEB, "web": {"route": "/x", "type": shape}}
        assert config.load_manifest(write(tmp_path, body)).web_type == shape


def test_an_unsupported_web_shape_is_refused(platform, tmp_path):
    """Adding a shape is a platform change - a base image, an identity shim and a
    health contract - not something a tenant can assert into existence."""
    body = {**BASE_WEB, "web": {"route": "/x", "type": "django"}}
    with pytest.raises(ManifestError, match="web.type"):
        config.load_manifest(write(tmp_path, body))


def test_streamlit_gets_the_same_data_guarantees(platform, as_app):
    """The point of the SDK being a library rather than a framework integration:
    a Streamlit app's entitlement is enforced by exactly the same code."""
    from insights_sdk.broker import query
    from insights_sdk.errors import EntitlementError
    from conftest import signed_in

    as_app("app-streamlit.yaml")
    with signed_in("sam@corp.example", "MG-PEOPLE-ANALYTICS,comp-analyst"):
        assert query("hr.headcount", "SELECT dept FROM hr.headcount")      # declared
        with pytest.raises(EntitlementError):
            query("hr.compensation", "SELECT * FROM hr.compensation")      # not declared


def test_an_unpublished_base_image_is_refused(platform, tmp_path):
    body = {**BASE_WEB, "runtime": {"sdk": ">=0.1,<1", "base": "my-own-image"}}
    with pytest.raises(ManifestError, match="not published"):
        config.load_manifest(write(tmp_path, body))


# --- declare, don't wire ------------------------------------------------------

def test_mechanism_words_are_refused_at_any_depth(platform, tmp_path):
    """A tenant declares intent. Naming a table, an engine or a classification is
    naming mechanism, and the loader refuses the file rather than ignoring the key."""
    for key in ("classification", "connection", "engine", "credential", "dsn", "table"):
        body = {**BASE_WEB, "data": [{"dataset": "hr.headcount", "access": "read", key: "x"}]}
        with pytest.raises(ManifestError, match=key):
            config.load_manifest(write(tmp_path, body))
