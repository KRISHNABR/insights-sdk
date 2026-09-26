"""The Dockerfile belongs to the tenant, so its properties are CHECKED not constructed.

ADR-004 was reversed: the platform used to render the image and refuse a tenant
Dockerfile, which made "built on a supported base" and "runs as non-root" true by
construction. Teams own the file now, so each of those becomes a check - and a check
is weaker than a construction, because it has to be right and it has to run.

These tests are the "has to be right" half.
"""

from __future__ import annotations

from types import SimpleNamespace

from insights_sdk.cli import scaffold


def _view(base="python-data", web=None, system=()):
    return SimpleNamespace(app="demo", base=base, web=web, system_packages=system)


def test_the_generated_dockerfile_pins_a_published_base():
    text = scaffold.render_dockerfile(_view())
    froms = [ln for ln in text.splitlines() if ln.startswith("FROM ")]
    assert froms, "no FROM line"
    image = froms[-1].split()[1]
    assert image.startswith("insights-hub/"), image
    assert not image.endswith(":latest"), "a base you cannot name is one you cannot roll back to"
    assert image.split(":", 1)[1] == scaffold.BASE_VERSIONS["python-data"]


def test_the_generated_dockerfile_never_ends_as_root():
    """The base image sets USER insights, so the tenant file correctly has no USER
    line at all. What must not happen is it ending on root."""
    text = scaffold.render_dockerfile(_view())
    users = [ln for ln in text.splitlines() if ln.strip().upper().startswith("USER ")]
    assert not users or users[-1].split()[1] not in ("root", "0")


def test_an_spa_copies_its_own_bundle_and_a_job_does_not():
    spa = scaffold.render_dockerfile(_view(base="python-web", web=SimpleNamespace(type="spa")))
    job = scaffold.render_dockerfile(_view())
    assert "COPY static/" in spa
    assert "COPY static/" not in job, "a job has no frontend to serve"


def test_declared_system_packages_install_as_root_then_drop_back():
    """The one place the template switches to root. If it ever stops switching back,
    every app built from it ships a root container."""
    text = scaffold.render_dockerfile(_view(system=("libgeos-dev",)))
    assert "libgeos-dev" in text
    users = [ln.strip() for ln in text.splitlines() if ln.strip().upper().startswith("USER ")]
    assert users[0].split()[1] == "root"
    assert users[-1].split()[1] == "insights", "installed as root and never dropped back"


def test_a_generated_app_contains_a_dockerfile(tmp_path):
    """It is generated ONCE. If this stops happening, every new app fails the gate
    on its first push - the worst possible first impression."""
    scaffold.generate(target=tmp_path / "app", name="demo", kind="web",
                      team="demo", owner="MG-DEMO")
    assert (tmp_path / "app" / "Dockerfile").is_file()


def test_the_dockerfile_is_not_platform_owned(tmp_path):
    """`upgrade-scaffold` re-renders PLATFORM_OWNED files. The Dockerfile must not be
    in that set: re-rendering it would silently discard the tenant's edits, which is
    exactly the control we just handed them."""
    assert all(path.name != "Dockerfile" for path in scaffold.PLATFORM_OWNED)
    assert all(path.name != "Dockerfile" for path in scaffold.render_platform_owned("demo"))
