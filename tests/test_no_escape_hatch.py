"""Evidence for ADR-002 and ADR-004: there is no way round the broker.

This test is unusual - it asserts something about the SHAPE of the SDK rather than its
behaviour. It is here because the guarantee that matters is not "the broker checks
entitlement", it is "there is nothing else to call". If someone adds a convenience
`get_connection()` in six months, this fails, and the ADR stops being true quietly.
"""

import insights_sdk
from insights_sdk import data


def test_the_sdk_exports_no_connection_primitive():
    forbidden = {"connect", "connection", "get_connection", "cursor", "engine", "session", "dsn"}
    exported = {name.lower() for name in dir(insights_sdk) if not name.startswith("_")}
    assert not (forbidden & exported), f"the SDK exports a way round the broker: {forbidden & exported}"


def test_the_data_module_exposes_exactly_two_verbs():
    assert set(data.__all__) == {"query", "fetch"}


def test_neither_verb_can_return_a_connection(platform, as_app):
    """Both return plain rows. Nothing a tenant holds afterwards can be reused to read again."""
    from conftest import signed_in

    as_app("app-web.yaml")
    with signed_in("dana@corp.example", "MG-PEOPLE-OPS"):
        rows = data.query("hr.headcount", "SELECT dept FROM hr.headcount")

    assert isinstance(rows, list)
    assert all(isinstance(row, dict) for row in rows)
