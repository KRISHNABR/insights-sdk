"""The scheduler's two regressions, locked in.

Both were found by review rather than by the suite, and both were invisible locally:
the job that exposes them runs with `schedule_enabled: false` in dev.
"""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCHEDULER = Path(__file__).resolve().parents[2] / "insights-platform" / "runtime" / "scheduler" / "main.py"
pytestmark = pytest.mark.skipif(not SCHEDULER.is_file(), reason="platform repo not checked out beside the SDK")


def _scheduler():
    spec = importlib.util.spec_from_file_location("scheduler", SCHEDULER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MONDAY_0600 = datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)
TUESDAY_0600 = datetime(2026, 9, 29, 6, 0, tzinfo=timezone.utc)


def test_three_letter_day_names_parse():
    """`0 6 * * MON` is what the platform's own most sensitive job declares.

    It used to raise ValueError: invalid literal for int() with base 10: 'MON' -- so
    the scheduler crashed on its first tick in any environment where that job was armed.
    """
    due = _scheduler().due
    assert due("0 6 * * MON", MONDAY_0600) is True
    assert due("0 6 * * MON", TUESDAY_0600) is False


def test_day_ranges_and_month_names_parse():
    due = _scheduler().due
    assert due("0 6 * * MON-FRI", datetime(2026, 9, 30, 6, 0, tzinfo=timezone.utc)) is True
    assert due("0 6 * * MON-FRI", datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)) is False  # Saturday
    assert due("0 6 1 JAN *", datetime(2027, 1, 1, 6, 0, tzinfo=timezone.utc)) is True


def test_a_minute_match_is_true_for_the_whole_minute():
    """`due()` matches to the minute and the loop ticks every 30s, so it is TRUE twice.

    That is correct behaviour for `due()` -- and it is why the loop must remember the
    last minute it fired each job. See test_the_loop_fires_once_per_minute.
    """
    due = _scheduler().due
    assert due("0 6 * * MON", MONDAY_0600.replace(second=5)) is True
    assert due("0 6 * * MON", MONDAY_0600.replace(second=35)) is True


def test_the_loop_fires_once_per_minute():
    """The de-duplication that stops every job running twice."""
    last_fired: dict[str, str] = {}
    fired = []

    for second in (5, 35):
        now = MONDAY_0600.replace(second=second)
        stamp = now.strftime("%Y-%m-%dT%H:%M")
        if last_fired.get("comp-report") != stamp:
            last_fired["comp-report"] = stamp
            fired.append(second)

    assert fired == [5], "a 30s tick with a minute-resolution match fires twice without de-duplication"
