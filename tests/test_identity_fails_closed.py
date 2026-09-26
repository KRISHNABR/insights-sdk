"""Evidence for ADR-004: identity is only believed when the platform edge asserts it.

If these tests pass, an app running outside the platform can read nothing - not because
someone remembered to check a flag, but because an untrusted caller has no groups.
"""

import pytest

from insights_sdk import config, identity
from insights_sdk import connect
from insights_sdk.errors import AuthzError, IdentityError

from conftest import EDGE_TOKEN, edge_headers, signed_in


def test_a_client_cannot_assert_its_own_identity(platform):
    """The headers the edge owns are worthless coming from a client."""
    forged = {"X-Auth-User": "ceo@corp.example", "X-Auth-Groups": "MG-PEOPLE-ANALYTICS,comp-analyst"}
    caller = identity.from_headers(forged)          # no valid edge token

    assert caller.trusted is False
    assert caller.subject == "anonymous"            # the claimed subject is discarded, not kept
    assert caller.groups == ()


def test_groups_are_empty_without_trust(platform):
    """The structural guarantee: authorization fails closed as a data structure."""
    untrusted = identity.Caller(subject="vidya@corp.example", request_id="r", trusted=False,
                                _groups=("comp-analyst",))
    assert untrusted.groups == ()
    assert untrusted.groups == ()          # fail-closed as a data structure


def test_an_app_run_outside_the_edge_can_read_nothing(platform, as_app):
    as_app("app-web.yaml")
    with pytest.raises(IdentityError):
        connect("team-warehouse").query("SELECT * FROM hr_headcount")


def test_a_valid_edge_assertion_is_believed(platform, as_app):
    as_app("app-web.yaml")
    with signed_in("krishna@corp.example", "MG-PEOPLE-OPS,headcount-viewer"):
        caller = identity.current_user()
        assert caller.trusted is True
        assert caller.groups == ("MG-PEOPLE-OPS", "headcount-viewer")
        assert connect("team-warehouse").query("SELECT dept FROM hr_headcount")


def test_require_role_refuses_a_trusted_caller_outside_every_tier(platform, as_app):
    """Trusted is not the same as authorized. The caller's identity is real and the
    edge vouched for it; they are simply not listed on this app."""
    as_app("app-web.yaml")
    with signed_in("outsider@corp.example", "MG-SOMEWHERE-ELSE"):
        with pytest.raises(AuthzError, match="not a reader"):
            identity.require_role("reader")


def test_an_owner_satisfies_every_tier(platform, as_app):
    """The tiers nest. An owner who had to be listed as a reader too would be listed
    three times, and forgotten once."""
    as_app("app-web.yaml")
    with signed_in("krishna@corp.example", "MG-PEOPLE-OPS"):     # the owner group
        for tier in ("owner", "contributor", "reader"):
            assert identity.require_role(tier).subject == "krishna@corp.example"


def test_a_job_acts_as_itself(platform, as_app):
    """A 06:00 batch run has no interactive user. The scheduler builds a service identity."""
    as_app("app-job.yaml")
    from insights_sdk import config

    manifest = config.manifest()
    service = identity.Caller.service(manifest.service_identity, manifest.owners, "run-1")

    assert service.subject == "sp-demo"
    assert service.trusted is True
    assert "MG-PEOPLE-ANALYTICS" in service.groups


def test_a_tenant_cannot_declare_its_own_service_identity(platform, tmp_path, monkeypatch):
    """Unattended work runs as `sp-<app>`, DERIVED from the registered app name.

    If a team could name their own, they could claim another app's and inherit
    whatever it can read. So there is no manifest field for it, and a manifest that
    invents one is rejected rather than ignored - an ignored key is a team believing
    something is configured.
    """
    from insights_sdk.errors import ManifestError

    body = '''apiVersion: v1
app: demo
team: demo-team
kind: job
access:
  manage:
    owners: [MG-DEMO]
runtime: {size: small}
job:
  schedule: "0 6 * * MON"
'''
    path = tmp_path / "app.yaml"
    path.write_text(body)
    monkeypatch.setenv("INSIGHTS_APP_MANIFEST", str(path))
    config.reset()
    assert config.manifest().service_identity == "sp-demo"

    # ...and it cannot be overridden from the file, at the top level or nested.
    for injected in ("service_identity: sp-someone-else\n",
                     "runtime: {size: small, service_identity: sp-someone-else}\n"):
        path.write_text(body.replace("runtime: {size: small}\n", "") + injected)
        config.reset()
        with pytest.raises(ManifestError):
            config.manifest()
    config.reset()
