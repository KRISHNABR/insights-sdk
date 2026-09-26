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
        "config", "connectors", "deprecation", "entrypoints",
        "errors", "identity", "outputs", "secrets", "telemetry",
    ):
        importlib.import_module(f"insights_sdk.{name}")


def test_every_command_is_registered_and_has_a_handler():
    from insights_sdk.cli.main import build_parser

    parser = build_parser()
    subparsers = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    commands = set(subparsers[0].choices)

    expected = {
        "new-app", "doctor", "connections", "status", "up", "serve", "run", "logs",
        "build", "compliance-report", "upgrade-scaffold",
    }
    assert expected <= commands, f"missing commands: {expected - commands}"

    for name, sub in subparsers[0].choices.items():
        assert sub.get_default("func") is not None, f"{name} has no handler"


@pytest.mark.parametrize("args", [["--version"], ["--help"]])
def test_the_cli_runs_without_a_project(args, capsys):
    from insights_sdk.cli.main import main

    with pytest.raises(SystemExit) as exit_info:
        main(args)
    assert exit_info.value.code == 0


def test_no_function_calls_a_name_that_does_not_exist():
    """Catch "the helper was deleted but something still calls it" without running it.

    This is the second time that shape has got through: first a module rename left a
    stale relative import, then a rewrite of `datasets`/`access` deleted the helper
    that `run` and `up` both call. Both were NameErrors that only appear when the
    command is actually executed, and neither had a natural unit test - `run` needs a
    warehouse, `up` starts processes.

    So check it statically instead. Walk every function in the CLI and assert each
    global name it loads is defined somewhere: module scope, an import, or a builtin.
    """
    import importlib

    # EVERY module, not just the CLI. It was cli/main.py only, and a rewrite then left
    # `__version__` unimported in entrypoints.py - a NameError that surfaced as an app
    # failing its health check, three layers from the cause.
    modules = [
        importlib.import_module(f"insights_sdk.{name}")
        for name in ("config", "connectors", "entrypoints", "identity", "outputs",
                     "secrets", "telemetry", "deprecation", "cli.main", "cli.scaffold")
    ]
    missing = []
    for module in modules:
        _check_module(module, missing)
    assert not missing, "\n".join(sorted(set(missing)))


def _check_module(module, missing: list) -> None:
    import ast
    import builtins
    import inspect

    source = inspect.getsource(module)
    tree = ast.parse(source)
    defined = set(dir(builtins)) | set(vars(module))

    # Only analyse TOP-LEVEL functions. A nested def is covered by walking its
    # parent, which is what makes closure variables resolve - checking it separately
    # would report every captured name as undefined.
    nested = {
        inner
        for outer in ast.walk(tree)
        if isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef))
        for inner in ast.walk(outer)
        if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)) and inner is not outer
    }

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node in nested:
            continue
        # Names bound inside the function itself - params, assignments, imports,
        # comprehension targets - are not global lookups.
        local = {a.arg for a in node.args.args + node.args.kwonlyargs}
        if node.args.vararg:
            local.add(node.args.vararg.arg)
        if node.args.kwarg:
            local.add(node.args.kwarg.arg)
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store):
                local.add(inner.id)
            elif isinstance(inner, (ast.Import, ast.ImportFrom)):
                for alias in inner.names:
                    local.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(inner, ast.ExceptHandler) and inner.name:
                local.add(inner.name)
            elif isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local.add(inner.name)
                local.update(a.arg for a in inner.args.args + inner.args.kwonlyargs)
                if inner.args.vararg:
                    local.add(inner.args.vararg.arg)
                if inner.args.kwarg:
                    local.add(inner.args.kwarg.arg)
            elif isinstance(inner, ast.ClassDef):
                local.add(inner.name)
            elif isinstance(inner, ast.Lambda):
                local.update(a.arg for a in inner.args.args + inner.args.kwonlyargs)
                if inner.args.vararg:
                    local.add(inner.args.vararg.arg)
                if inner.args.kwarg:
                    local.add(inner.args.kwarg.arg)

        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load):
                if inner.id not in local and inner.id not in defined:
                    missing.append(f"{node.name}() calls undefined name {inner.id!r}")

    assert not missing, "\n".join(sorted(set(missing)))


def test_new_app_works_outside_the_workspace(tmp_path, monkeypatch):
    """The FIRST command a new team runs, and it runs from an empty directory.

    `_local_defaults()` resolves the platform checkout to point INSIGHTS_* at the local
    fakes. It used to raise outside the workspace, and because it runs for every
    command, `insights new-app` failed with "Run this from inside the insights-hub
    workspace" - telling someone with no repo yet to go and get one.
    """
    from insights_sdk.cli.main import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("INSIGHTS_REGISTRY_DIR", raising=False)

    assert main(["new-app", "forecast", "--kind", "web",
                 "--team", "demo", "--owner", "MG-DEMO"]) == 0
    assert (tmp_path / "insights-forecast" / "app.yaml").is_file()
    assert (tmp_path / "insights-forecast" / "Dockerfile").is_file()
