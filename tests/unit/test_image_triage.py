"""
Unit tests for image_triage: the relevance-triage pass on converted-document
images. Covers the split figures/pages logic, the threshold gate, the
describe-all fallback on failure, verdict parsing, and the batch Vision call
(with a mocked provider — no API key needed).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.privacy_service import image_triage as it  # noqa: E402


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_split_figures_and_pages():
    images = {
        "picture-1.png": "AAAA",
        "picture-2.png": "BBBB",
        "page-001.png": "CCCC",
        "page-002.png": "DDDD",
    }
    figures, rendered = it.split_figures_and_pages(images)
    assert set(figures) == {"picture-1.png", "picture-2.png"}
    assert set(rendered) == {"page-001.png", "page-002.png"}


def test_should_triage_threshold_and_killswitch(monkeypatch):
    monkeypatch.setenv("BRIDGE_IMAGE_TRIAGE_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_IMAGE_TRIAGE_THRESHOLD", "3")
    assert it.should_triage({f"f{i}": "x" for i in range(3)}) is False  # == threshold, not >
    assert it.should_triage({f"f{i}": "x" for i in range(4)}) is True
    # Kill-switch overrides even a large figure set.
    monkeypatch.setenv("BRIDGE_IMAGE_TRIAGE_ENABLED", "false")
    assert it.should_triage({f"f{i}": "x" for i in range(50)}) is False


def test_parse_verdict_plain():
    obj = it._parse_verdict('{"1": {"relevant": true, "label": "Schema"}}')
    assert obj["1"]["relevant"] is True
    assert obj["1"]["label"] == "Schema"


def test_parse_verdict_code_fenced_with_prose():
    text = 'Hier das Ergebnis:\n```json\n{"1": {"relevant": false, "label": "Logo"}}\n```\n'
    obj = it._parse_verdict(text)
    assert obj["1"]["relevant"] is False


def test_parse_verdict_no_json_raises():
    with pytest.raises(ValueError):
        it._parse_verdict("Ich kann das nicht beurteilen.")


# ---------------------------------------------------------------------------
# plan_image_descriptions — the orchestration decision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_below_threshold_describes_all(monkeypatch):
    monkeypatch.setenv("BRIDGE_IMAGE_TRIAGE_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_IMAGE_TRIAGE_THRESHOLD", "10")
    images = {"picture-1.png": "AAAA", "picture-2.png": "BBBB"}
    to_describe, skipped, meta = await it.plan_image_descriptions(images)
    assert to_describe == images  # byte-identical behaviour
    assert skipped == {}
    assert meta is None


@pytest.mark.asyncio
async def test_plan_triages_and_keeps_rendered_pages(monkeypatch):
    monkeypatch.setenv("BRIDGE_IMAGE_TRIAGE_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_IMAGE_TRIAGE_THRESHOLD", "2")

    async def fake_select(figures, **kwargs):
        return {
            "picture-1.png": {"relevant": True, "label": "Schema"},
            "picture-2.png": {"relevant": False, "label": "Firmenlogo"},
            "picture-3.png": {"relevant": False, "label": "Zierfoto"},
        }

    monkeypatch.setattr(it, "select_relevant_images", fake_select)

    images = {
        "picture-1.png": "A",
        "picture-2.png": "B",
        "picture-3.png": "C",
        "page-001.png": "P",  # rendered page must survive triage
    }
    to_describe, skipped, meta = await it.plan_image_descriptions(images)

    assert set(to_describe) == {"picture-1.png", "page-001.png"}
    assert skipped == {"picture-2.png": "Firmenlogo", "picture-3.png": "Zierfoto"}
    assert meta["total_figures"] == 3
    assert meta["described_figures"] == 1
    assert meta["rendered_pages"] == 1


@pytest.mark.asyncio
async def test_plan_keeps_unresolved_figures(monkeypatch):
    """A figure the model does not rule on is kept (inclusion bias, no drop)."""
    monkeypatch.setenv("BRIDGE_IMAGE_TRIAGE_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_IMAGE_TRIAGE_THRESHOLD", "1")

    async def fake_select(figures, **kwargs):
        return {"picture-1.png": {"relevant": False, "label": "Logo"}}  # says nothing about #2

    monkeypatch.setattr(it, "select_relevant_images", fake_select)

    images = {"picture-1.png": "A", "picture-2.png": "B"}
    to_describe, skipped, meta = await it.plan_image_descriptions(images)
    assert "picture-2.png" in to_describe  # unresolved → kept
    assert skipped == {"picture-1.png": "Logo"}


@pytest.mark.asyncio
async def test_plan_falls_back_to_describe_all_on_error(monkeypatch):
    monkeypatch.setenv("BRIDGE_IMAGE_TRIAGE_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_IMAGE_TRIAGE_THRESHOLD", "1")

    async def boom(figures, **kwargs):
        raise RuntimeError("vision down")

    monkeypatch.setattr(it, "select_relevant_images", boom)

    images = {"picture-1.png": "A", "picture-2.png": "B"}
    to_describe, skipped, meta = await it.plan_image_descriptions(images)
    assert to_describe == images  # nothing dropped
    assert skipped == {}
    assert meta is None


# ---------------------------------------------------------------------------
# select_relevant_images — batch Vision call with a mocked provider
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, content: str):
        self.content = content


class _FakeProvider:
    def __init__(self, content: str):
        self._content = content
        self.calls = 0

    async def analyze(self, messages, **kwargs):
        self.calls += 1
        return _FakeResp(self._content)


@pytest.mark.asyncio
async def test_select_maps_indices_to_names(monkeypatch):
    monkeypatch.setattr(it, "_downscale_b64_png", lambda b64, max_edge=0: b64)
    provider = _FakeProvider(
        '{"1": {"relevant": true, "label": "Schema"}, '
        '"2": {"relevant": false, "label": "Logo"}}'
    )
    monkeypatch.setattr(it, "get_vision_provider", lambda: provider)

    figures = {"picture-1.png": "A", "picture-2.png": "B"}
    verdict = await it.select_relevant_images(figures, batch_size=20)

    assert verdict["picture-1.png"] == {"relevant": True, "label": "Schema"}
    assert verdict["picture-2.png"] == {"relevant": False, "label": "Logo"}
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_select_batches_split_by_size(monkeypatch):
    monkeypatch.setattr(it, "_downscale_b64_png", lambda b64, max_edge=0: b64)
    provider = _FakeProvider('{"1": {"relevant": true, "label": "x"}}')
    monkeypatch.setattr(it, "get_vision_provider", lambda: provider)

    figures = {f"picture-{i}.png": "X" for i in range(5)}
    await it.select_relevant_images(figures, batch_size=2)
    assert provider.calls == 3  # 2 + 2 + 1


@pytest.mark.asyncio
async def test_select_unparseable_reply_keeps_all_relevant(monkeypatch):
    monkeypatch.setattr(it, "_downscale_b64_png", lambda b64, max_edge=0: b64)
    provider = _FakeProvider("Sorry, kann ich nicht.")
    monkeypatch.setattr(it, "get_vision_provider", lambda: provider)

    figures = {"picture-1.png": "A", "picture-2.png": "B"}
    verdict = await it.select_relevant_images(figures)
    assert all(v["relevant"] for v in verdict.values())  # no drop on parse failure


@pytest.mark.asyncio
async def test_select_empty_returns_empty(monkeypatch):
    called = {"n": 0}

    def _guard():
        called["n"] += 1
        raise AssertionError("provider must not be built for empty figures")

    monkeypatch.setattr(it, "get_vision_provider", _guard)
    assert await it.select_relevant_images({}) == {}
    assert called["n"] == 0
