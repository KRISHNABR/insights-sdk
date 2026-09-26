"""Evidence for ADR-002: knowing a dataset's name is not access, and declaring a
restricted dataset is not enough on its own."""

import pytest

from insights_sdk.data import fetch, query
from insights_sdk.errors import EntitlementError, InsightsError, UnknownDatasetError

from conftest import signed_in


def test_an_undeclared_dataset_is_refused(platform, as_app):
    """`sales.pipeline` is a real dataset with real rows. This app did not declare it."""
    as_app("app-web.yaml")
    with signed_in("dana@corp.example", "MG-PEOPLE-OPS"):
        with pytest.raises(EntitlementError, match="not entitled"):
            query("sales.pipeline", "SELECT * FROM sales.pipeline")


def test_a_nonexistent_dataset_is_indistinguishable_from_one_you_may_not_have(platform, as_app):
    """No discovery oracle.

    `hr.secrets` does not exist; `sales.pipeline` exists and is simply not this app's.
    Both raise the SAME error, because the entitlement check runs before the catalog is
    consulted. A differentiated error would let any tenant enumerate the catalog by
    guessing names, which is discovery through the back door (ADR-002 s5).
    """
    as_app("app-web.yaml")
    with signed_in("dana@corp.example", "MG-PEOPLE-OPS"):
        with pytest.raises(EntitlementError) as absent:
            query("hr.secrets", "SELECT * FROM hr.secrets")
        with pytest.raises(EntitlementError) as forbidden:
            query("sales.pipeline", "SELECT * FROM sales.pipeline")

    shape = lambda e: str(e.value).replace("hr.secrets", "X").replace("sales.pipeline", "X")
    assert shape(absent) == shape(forbidden)
    assert "hr.compensation" not in str(absent.value)


def test_unknown_dataset_error_is_for_platform_mistakes_only(platform, as_app):
    """UnknownDatasetError means the CATALOG is wrong, not that a tenant guessed. It can
    only be reached by an app that declared something the platform does not have - which
    CI blocks, so in practice it means someone removed a dataset out from under a tenant."""
    as_app("app-web.yaml")
    from insights_sdk import config

    with pytest.raises(UnknownDatasetError):
        config.catalog().resolve("hr.secrets")


def test_declaring_a_restricted_dataset_is_not_enough(platform, as_app):
    """The second key. `curious-app` declares hr.compensation; nobody granted it."""
    as_app("app-ungranted.yaml")
    with signed_in("someone@corp.example", "MG-SOME-TEAM"):
        with pytest.raises(EntitlementError, match="dataset owner"):
            query("hr.compensation", "SELECT * FROM hr.compensation")


def test_a_granted_restricted_dataset_is_allowed(platform, as_app):
    as_app("app-job.yaml")
    with signed_in("sam@corp.example", "MG-PEOPLE-ANALYTICS,comp-analyst"):
        rows = query("hr.compensation", "SELECT employee_id FROM hr.compensation")
    assert len(rows) == 2


def test_sql_cannot_reach_past_the_declared_dataset(platform, as_app):
    """Declare something harmless, then select from a table you were never granted."""
    as_app("app-web.yaml")
    with signed_in("dana@corp.example", "MG-PEOPLE-OPS"):
        with pytest.raises(EntitlementError, match="belongs to dataset"):
            query(
                "hr.headcount",
                "SELECT h.dept FROM hr.headcount h JOIN hr_compensation c ON 1=1",
            )


def test_sql_must_reference_the_dataset_it_names(platform, as_app):
    as_app("app-web.yaml")
    with signed_in("dana@corp.example", "MG-PEOPLE-OPS"):
        with pytest.raises(EntitlementError, match="does not reference"):
            query("hr.headcount", "SELECT 1")


def test_the_wrong_verb_says_which_one_to_use(platform, as_app):
    as_app("app-web.yaml")
    with signed_in("dana@corp.example", "MG-PEOPLE-OPS"):
        with pytest.raises(InsightsError, match=r"fetch\(\)"):
            query("directory.people", "SELECT * FROM directory.people")
