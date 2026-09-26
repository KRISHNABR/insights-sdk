"""Evidence for ADR-004: identity is only believed when the platform edge asserts it.

If these tests pass, an app running outside the platform can read nothing - not because
someone remembered to check a flag, but because an untrusted caller has no groups.
"""

import pytest

from insights_sdk import identity
from insights_sdk.broker import query
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
    assert untrusted.has_role("comp-analyst") is False


def test_an_app_run_outside_the_edge_can_read_nothing(platform, as_app):
    as_app("app-web.yaml")
    with pytest.raises(IdentityError):
        query("hr.headcount", "SELECT * FROM hr.headcount")


def test_a_valid_edge_assertion_is_believed(platform, as_app):
    as_app("app-web.yaml")
    with signed_in("krishna@corp.example", "MG-PEOPLE-OPS,headcount-viewer"):
        caller = identity.current_user()
        assert caller.trusted is True
        assert caller.has_role("headcount-viewer")
        assert query("hr.headcount", "SELECT dept FROM hr.headcount")


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

    assert service.subject == "sp-comp-report"
    assert service.trusted is True
    assert "MG-PEOPLE-ANALYTICS" in service.groups
