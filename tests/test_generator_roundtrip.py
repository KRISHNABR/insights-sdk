"""Evidence for ADR-001 and ADR-004: the generator is the cheapest enforcement layer.

That claim is only true if what it generates is actually valid. A generator that emits
a manifest the loader rejects is worse than no generator - it teaches a new team that
the platform is broken on their first command.
"""

import pytest

from insights_sdk import config
from insights_sdk.cli import scaffold
from insights_sdk.errors import ManifestError


@pytest.mark.parametrize("kind", ["web", "job"])
def test_a_generated_app_passes_the_loader(platform, tmp_path, kind):
    """new-app -> a manifest that loads. The round trip, for both archetypes."""
    target = tmp_path / f"insights-new-{kind}"
    scaffold.generate(target=target, name=f"new-{kind}", kind=kind,
                      team="some-team", owner="MG-SOME-TEAM")

    manifest = config.load_manifest(target / "app.yaml")
    assert manifest.app == f"new-{kind}"
    assert manifest.kind == kind
    assert manifest.manage.owners == ("MG-SOME-TEAM",)
    # `kind` is not decoration: each shape gets its own block and only its own.
    assert (manifest.job is not None) is (kind == "job")
    assert (manifest.web is not None) is (kind == "web")


def test_a_generated_app_ships_with_a_dockerfile(platform, tmp_path):
    """Inverted when ADR-004 was reversed and teams took ownership of the image.

    It is generated ONCE, here. If that stops, every new app fails the deploy gate on
    its first push, which is the worst possible introduction to a platform."""
    target = tmp_path / "insights-withdocker"
    written = scaffold.generate(target=target, name="withdocker", kind="web",
                                team="t", owner="MG-T")

    assert (target / "Dockerfile").is_file()
    assert "Dockerfile" in [p.name for p in written]
    # ...and it must not be re-rendered afterwards, or we silently discard their edits.
    assert "Dockerfile" not in [p.name for p in scaffold.PLATFORM_OWNED]


def test_the_image_is_rendered_from_the_manifest(platform, as_app):
    """Platform-owned must not mean hidden: `insights build --show` prints exactly
    what would be built, derived from what the team declared."""
    as_app("app-web.yaml")
    rendered = scaffold.render_dockerfile(config.manifest())

    assert "FROM insights-hub/python-web:0.1" in rendered   # pinned, never :latest
    # Check the FROM LINES, not the whole file: the template's own comment explains
    # why :latest is refused, and a naive substring search matched that comment.
    froms = [ln for ln in rendered.splitlines() if ln.startswith("FROM ")]
    assert froms and not any(ln.split()[1].endswith(":latest") for ln in froms)
    assert "COPY static/" in rendered                        # because web.type is spa


def test_the_image_installs_the_tenants_own_dependencies(platform, as_app):
    """"Bring your code" has to include bringing your dependencies.

    An earlier version copied src/ and app.yaml and nothing else, so a team adding
    pandas to pyproject.toml had it locally and NOT in their image - the worst kind of
    gap, because everything passes until it runs.
    """
    as_app("app-web.yaml")
    rendered = scaffold.render_dockerfile(config.manifest())

    assert "COPY pyproject.toml uv.lock" in rendered
    assert "uv sync --frozen" in rendered            # the committed lock, or fail
    # dependencies before source, so a code change does not reinstall everything
    assert rendered.index("uv sync") < rendered.index("COPY src/")


def test_the_job_image_has_no_web_server(platform, as_app):
    as_app("app-job.yaml")
    rendered = scaffold.render_dockerfile(config.manifest())

    assert "python-data" in rendered
    assert "COPY static/" not in rendered


def test_every_platform_owned_file_can_be_re_rendered(platform):
    """`insights upgrade-scaffold` only works because the SDK knows which files it
    owns. If a file is generated but not listed here, it silently rots."""
    rendered = scaffold.render_platform_owned("x")
    assert set(rendered) == set(scaffold.PLATFORM_OWNED)
    assert all(content.strip() for content in rendered.values())
