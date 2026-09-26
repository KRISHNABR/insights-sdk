"""Evidence for ADR-002: the alias earns its place, and masking follows the caller."""

from insights_sdk import config
from insights_sdk.broker import query

from conftest import signed_in


def test_the_same_sql_reads_a_different_table_in_a_different_environment(platform, as_app, monkeypatch):
    """The point of the alias.

    `hr.headcount` is table `hr_headcount` locally and `hr_headcount_v2` in production.
    The tenant's SQL does not change - and cannot, because it never names a table.
    The two tables hold different data, so this cannot pass by accident.
    """
    as_app("app-web.yaml")
    sql = "SELECT headcount FROM hr.headcount WHERE dept = :dept"

    with signed_in("krishna@corp.example", "MG-PEOPLE-OPS"):
        local = query("hr.headcount", sql, dept="Engineering")

        monkeypatch.setenv("INSIGHTS_ENV", "prod")
        config.reset()
        as_app("app-web.yaml")
        prod = query("hr.headcount", sql, dept="Engineering")

    assert local[0]["headcount"] == 184
    assert prod[0]["headcount"] == 999


def test_a_caller_without_the_role_gets_masked_fields(platform, as_app):
    """Raj is in People Analytics - so he reaches the dataset - but is not a comp-analyst."""
    as_app("app-job.yaml")
    with signed_in("raj@corp.example", "MG-PEOPLE-ANALYTICS"):
        rows = query("hr.compensation", "SELECT employee_name, base_salary, dept FROM hr.compensation")

    assert rows[0]["employee_name"] == "***"
    assert rows[0]["base_salary"] == "***"
    assert rows[0]["dept"] == "People Ops"          # not a masked field, still readable


def test_a_caller_with_the_role_sees_the_values(platform, as_app):
    as_app("app-job.yaml")
    with signed_in("sam@corp.example", "MG-PEOPLE-ANALYTICS,comp-analyst"):
        rows = query("hr.compensation", "SELECT employee_name, base_salary FROM hr.compensation")

    assert rows[0]["employee_name"] == "Krishna Murari"
    assert rows[0]["base_salary"] == 94000


def test_masking_is_recorded_as_a_count_not_a_list(platform, as_app):
    """The audit stream is read by more people than the data is, so it records how many
    fields were masked and never which ones."""
    from insights_sdk import telemetry

    as_app("app-job.yaml")
    with telemetry.capture() as records:
        with signed_in("raj@corp.example", "MG-PEOPLE-ANALYTICS"):
            query("hr.compensation", "SELECT employee_name, base_salary FROM hr.compensation")

    audit = [r for r in records if r["stream"] == "audit"][-1]
    assert audit["masked_fields"] == 2
    assert audit["classification"] == "restricted"
    assert audit["caller"] == "raj@corp.example"
    assert "employee_name" not in str(audit)


def test_a_job_sees_unmasked_fields_only_because_the_owner_granted_them(platform, as_app):
    """The service-identity question, which is easy to get wrong.

    A scheduled job has no human, so "may this caller see salaries?" cannot be answered
    from corporate groups. The tempting answer - read `access.roles` from app.yaml - is
    wrong, because that file is in the tenant's own repo. Instead the roles come from the
    grant, which only the dataset owner can write.
    """
    from insights_sdk import config, identity, telemetry
    as_app("app-job.yaml")
    manifest = config.manifest()
    service = identity.Caller.service(manifest.service_subject, manifest.owners, "run-1")

    assert "comp-analyst" not in service.groups          # not in the app's owner groups

    with telemetry.capture() as records:
        with identity.as_caller(service):
            rows = query("hr.compensation", "SELECT base_salary FROM hr.compensation")

    assert rows[0]["base_salary"] == 94000               # unmasked...
    audit = [r for r in records if r["stream"] == "audit"][-1]
    assert audit["via_grant"] is True                    # ...and the audit says why


def test_a_job_without_a_granted_role_is_still_masked(platform, as_app, monkeypatch):
    """Remove the role from the grant and the same job gets masked data. The tenant's
    own manifest is unchanged throughout - it has no say in this."""
    import yaml
    from insights_sdk import config, identity

    from conftest import FIXTURES

    original = (FIXTURES / "grants.yaml").read_text()
    body = yaml.safe_load(original)
    body["grants"][0].pop("roles")
    (FIXTURES / "grants.yaml").write_text(yaml.safe_dump(body, sort_keys=False))
    try:
        as_app("app-job.yaml")
        manifest = config.manifest()
        service = identity.Caller.service(manifest.service_subject, manifest.owners, "run-2")
        with identity.as_caller(service):
            rows = query("hr.compensation", "SELECT base_salary FROM hr.compensation")
        assert rows[0]["base_salary"] == "***"
    finally:
        (FIXTURES / "grants.yaml").write_text(original)
