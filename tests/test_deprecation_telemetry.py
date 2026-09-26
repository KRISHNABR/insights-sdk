"""Evidence for ADR-001: the upgrade story is driven by data, not by email."""

import warnings

from insights_sdk import telemetry
from insights_sdk.deprecation import deprecated


def test_a_deprecated_call_tells_the_platform_who_is_still_using_it(platform):
    @deprecated(since="0.2", removed_in="1.0", instead="query()")
    def run_sql(x):
        return x

    with telemetry.capture() as records:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            run_sql(1)

    event = [r for r in records if r["event"] == "sdk_deprecated_use"][0]
    assert event["removed_in"] == "1.0"
    assert event["instead"] == "query()"
    assert "app" in event and "sdk" in event      # who, and on what version


def test_it_emits_once_per_symbol_not_once_per_call(platform):
    """A job in a loop must produce one signal, or the telemetry that was meant to make
    upgrades tractable becomes the reason nobody reads the logs."""

    @deprecated(since="0.2", removed_in="1.0", instead="fetch()")
    def old_fetch():
        return None

    with telemetry.capture() as records:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            for _ in range(100):
                old_fetch()

    assert len([r for r in records if r["event"] == "sdk_deprecated_use"]) == 1


def test_the_signature_survives_the_wrapper(platform):
    import inspect

    @deprecated(since="0.2", removed_in="1.0", instead="query()")
    def takes_two(a, b=3):
        return a

    assert list(inspect.signature(takes_two).parameters) == ["a", "b"]
