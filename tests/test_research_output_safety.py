"""
Guards security-audit-live-findings-20260818.md L10c/B.4: POST /v1/research's
optional `output_path` field let a caller write anywhere the Bridge process
could write (arbitrary file write) since both write sites in main.py passed
it straight to the filesystem with no containment check.
"""
from __future__ import annotations

import os

import pytest

from src.research_output_safety import default_output_path, safe_output_path


@pytest.fixture(autouse=True)
def _allowed_dir(tmp_path, monkeypatch):
    """Point the allowed output directory at a throwaway tmp_path per test."""
    monkeypatch.setenv("BRIDGE_RESEARCH_OUTPUT_DIR", str(tmp_path))
    return tmp_path


class TestUnsetOrEmpty:
    def test_none_returns_none(self):
        assert safe_output_path(None) is None

    def test_empty_string_returns_none(self):
        assert safe_output_path("") is None

    def test_whitespace_only_returns_none(self):
        assert safe_output_path("   ") is None


class TestSafePaths:
    def test_relative_filename_is_rooted_at_allowed_dir(self, _allowed_dir):
        result = safe_output_path("report.md")
        assert result == (_allowed_dir / "report.md").resolve()

    def test_absolute_path_inside_allowed_dir_is_accepted(self, _allowed_dir):
        target = _allowed_dir / "subdir" / "report.md"
        result = safe_output_path(str(target))
        assert result == target.resolve()

    def test_default_output_path_is_inside_allowed_dir(self, _allowed_dir):
        result = default_output_path("research-abc123.md")
        assert result == (_allowed_dir / "research-abc123.md").resolve()


class TestPathTraversalRejected:
    def test_absolute_path_outside_allowed_dir_is_rejected(self):
        assert safe_output_path("/etc/cron.d/evil") is None

    def test_relative_traversal_escaping_allowed_dir_is_rejected(self, _allowed_dir):
        assert safe_output_path("../../../etc/passwd") is None

    def test_absolute_traversal_escaping_allowed_dir_is_rejected(self, _allowed_dir):
        assert safe_output_path(str(_allowed_dir / ".." / ".." / "etc" / "passwd")) is None

    def test_traversal_that_looks_contained_as_a_string_is_still_rejected(self, _allowed_dir):
        """Prefix-string tricks (e.g. an allowed dir '/tmp' vs '/tmpfoo') must
        not pass a naive startswith() check — this uses Path.resolve() +
        relative_to(), not string prefixing."""
        sibling = _allowed_dir.parent / (_allowed_dir.name + "-evil-sibling")
        assert safe_output_path(str(sibling / "x.md")) is None

    def test_default_output_path_strips_traversal_from_filename(self, _allowed_dir):
        """default_output_path only ever uses Path(filename).name — a
        traversal string as the 'filename' can't escape via this path either."""
        result = default_output_path("../../etc/passwd")
        assert result == (_allowed_dir / "passwd").resolve()
        assert result.parent == _allowed_dir.resolve()


class TestSymlinkEscape:
    def test_symlink_pointing_outside_allowed_dir_is_rejected(self, _allowed_dir, tmp_path_factory):
        outside = tmp_path_factory.mktemp("outside")
        link = _allowed_dir / "escape.md"
        link.symlink_to(outside / "target.md")
        assert safe_output_path(str(link)) is None


class TestLegitimateResearchClientBehaviourUnaffected:
    """The documented client (orchestrator/bin/bridge-research.py) never
    sends output_path at all — it writes the response's `content` locally
    itself. The one contract this guard must preserve is the pre-existing
    default: omitting output_path still resolves under /tmp (now the
    configurable allowed dir, same value in production)."""

    def test_omitted_output_path_falls_back_to_default_dir(self, _allowed_dir):
        assert safe_output_path(None) is None
        # main.py's CLI-subprocess path does:
        #   output_file = safe_output_path(x) or default_output_path(filename)
        # i.e. None correctly triggers the pre-existing /tmp/<filename> default.
        assert default_output_path("query-result.md").parent == _allowed_dir.resolve()

    def test_default_allowed_dir_is_tmp_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("BRIDGE_RESEARCH_OUTPUT_DIR", raising=False)
        # Matches the pre-existing documented default ("If not provided,
        # saves to /tmp/", src/models.py ResearchRequest.output_path).
        assert str(default_output_path("x.md").parent) == os.path.realpath("/tmp")
