"""Evidence for ADR-003: telemetry structurally cannot carry tenant data."""

import pytest

from insights_sdk import telemetry
from insights_sdk.broker import query
from insights_sdk.errors import RedactionError

from conftest import signed_in


def test_a_log_record_cannot_carry_rows(platform):
    log = telemetry.get_logger()
    rows = [{"employee_name": "Krishna Murari", "base_salary": 94000}]
    with pytest.raises(RedactionError, match="scalars"):
        log.info("debug", rows=rows)


def test_a_log_record_cannot_carry_a_payload_disguised_as_a_string(platform):
    log = telemetry.get_logger()
    with pytest.raises(RedactionError, match="payload in string form"):
        log.info("debug", blob="x" * 900)


def test_facts_about_a_run_are_fine(platform):
    log = telemetry.get_logger()
    with telemetry.capture() as records:
        log.info("query_complete", dataset="hr.headcount", rows=412, ms=38)
    assert records[0]["rows"] == 412


def test_reading_restricted_data_arms_the_field_assertion(platform, as_app):
    """Before the read, `base_salary` is just a word. After it, mentioning it raises -
    because the field list came from the catalog when the broker resolved the dataset."""
    as_app("app-job.yaml")
    log = telemetry.get_logger()

    log.info("startup", note="checking base_salary column exists")     # fine: nothing armed yet

    with signed_in("vidya@corp.example", "MG-PEOPLE-ANALYTICS,comp-analyst"):
        query("hr.compensation", "SELECT employee_id FROM hr.compensation")

        with pytest.raises(RedactionError, match="base_salary"):
            log.info("startup", note="checking base_salary column exists")


def test_the_tenant_cannot_shorten_the_sensitive_field_list(platform, as_app):
    """The list lives in the platform catalog. Tenant code has no API to edit it."""
    import insights_sdk

    assert not hasattr(insights_sdk, "register_sensitive_fields")
    assert not hasattr(insights_sdk, "clear_sensitive_fields")
