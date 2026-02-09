from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _clear_build_info_caches(monkeypatch: pytest.MonkeyPatch):
    """Ensure build_info cache + env are clean for each test."""

    # Delay import until inside fixture so tests can adjust env/module state.
    from roi import build_info

    # Clear revision-related env vars so tests are isolated.
    for k in (
        "ROI_REVISION",
        "ROI_GIT_COMMIT",
        "GIT_COMMIT",
        "GITHUB_SHA",
        "CI_COMMIT_SHA",
        "SOURCE_VERSION",
    ):
        monkeypatch.delenv(k, raising=False)

    # Remove any injected generated revision module.
    sys.modules.pop("roi._revision", None)

    # Clear memoized results.
    build_info.get_revision_full.cache_clear()
    build_info.get_revision.cache_clear()
    build_info.get_version.cache_clear()

    yield

    # Also clear after, in case a test mutated state.
    sys.modules.pop("roi._revision", None)
    build_info.get_revision_full.cache_clear()
    build_info.get_revision.cache_clear()
    build_info.get_version.cache_clear()


def test_get_revision_unknown_without_sources(monkeypatch: pytest.MonkeyPatch):
    """If env + generated module are absent AND we're not in a git checkout, we report unknown.

    This test must be deterministic: it should pass whether the working tree happens to be a
    real git repo or not. So we explicitly pretend there is no git root for this test.
    """

    from roi import build_info

    # Ensure the git-discovery branch can't find a .git directory/file from either
    # the module path or the current working directory.
    monkeypatch.setattr(build_info, "_find_git_root", lambda *_a, **_k: None)
    build_info.get_revision_full.cache_clear()
    build_info.get_revision.cache_clear()

    assert build_info.get_revision() == "unknown"


def test_get_revision_env_override(monkeypatch: pytest.MonkeyPatch):
    from roi import build_info

    monkeypatch.setenv("ROI_REVISION", "1234567890abcdef")
    build_info.get_revision_full.cache_clear()
    build_info.get_revision.cache_clear()

    assert build_info.get_revision(short=True) == "1234567"
    assert build_info.get_revision(short=False) == "1234567890abcdef"


def test_get_revision_generated_module_override():
    from roi import build_info

    mod = types.ModuleType("roi._revision")
    mod.GIT_COMMIT = "deadbeefcafebabe"
    sys.modules["roi._revision"] = mod

    build_info.get_revision_full.cache_clear()
    build_info.get_revision.cache_clear()

    assert build_info.get_revision(short=True) == "deadbee"
    assert build_info.get_revision(short=False) == "deadbeefcafebabe"


def test_build_banner_includes_rev_and_tag(monkeypatch: pytest.MonkeyPatch):
    from roi import build_info

    monkeypatch.setenv("ROI_REVISION", "abcdef123456")
    build_info.get_revision_full.cache_clear()
    build_info.get_revision.cache_clear()

    banner = build_info.build_banner(build_tag="lab-pi-01")
    assert "ROI" in banner
    assert "rev abcdef1" in banner
    assert "tag lab-pi-01" in banner
