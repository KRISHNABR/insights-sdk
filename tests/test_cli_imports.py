"""The CLI must at least import, and every command must be wired.

This exists because it was missing. A module rename left `from ..data import` in
cli/main.py, the whole test suite still passed - because nothing in it imported the
CLI - and the break was found by CI running `insights doctor` instead.

Cheap tests that catch whole-module breakage are worth more than their line count.
"""

import importlib

import pytest


def test_the_cli_module_imports():
    importlib.import_module("insights_sdk.cli.main")


def test_the_scaffold_module_imports():
    importlib.import_module("insights_sdk.cli.scaffold")


def test_every_public_module_imports():
    """A rename that misses one relative import should fail here, not in production."""
    for name in (
        "adapters", "broker", "config", "deprecation", "entrypoints",
        "errors", "identity", "outputs", "telemetry",
    ):
        importlib.import_module(f"insights_sdk.{name}")


def test_every_command_is_registered_and_has_a_handler():
    from insights_sdk.cli.main import build_parser

    parser = build_parser()
    subparsers = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    commands = set(subparsers[0].choices)

    expected = {
        "new-app", "doctor", "datasets", "status", "up", "run",
        "build", "compliance-report", "access", "upgrade-scaffold",
    }
    assert expected <= commands, f"missing commands: {expected - commands}"

    for name, sub in subparsers[0].choices.items():
        # `access` dispatches to subcommands; everything else needs its own handler
        assert sub.get_default("func") is not None or name == "access", f"{name} has no handler"


@pytest.mark.parametrize("args", [["--version"], ["--help"]])
def test_the_cli_runs_without_a_project(args, capsys):
    from insights_sdk.cli.main import main

    with pytest.raises(SystemExit) as exit_info:
        main(args)
    assert exit_info.value.code == 0
