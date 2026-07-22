"""
FlairRecognizer._split_windows — memory-guard regression tests.

The windowing exists so one predict() never sees unbounded text (OOM
root-cause 2026-07-03: a ~12K-char document spiked a worker >10GB RSS and the
16G cgroup killed it mid-request). These tests pin the invariants the
recognizer's offset arithmetic relies on: full coverage, correct offsets,
bounded window size.
"""
import pytest

pytest.importorskip("presidio_analyzer")

from src.privacy.flair_recognizer import FlairRecognizer  # noqa: E402


def _reassemble(windows):
    return "".join(w for _, w in windows)


class TestSplitWindows:
    def test_short_text_single_window(self):
        text = "Max Mustermann wohnt in Wien."
        windows = FlairRecognizer._split_windows(text)
        assert windows == [(0, text)]

    def test_full_coverage_and_offsets(self):
        # Paragraph-structured text well above one window
        para = "Der Prüfbericht dokumentiert die Messung an sechs Messpunkten.\n\n"
        text = para * 300  # ~19K chars
        windows = FlairRecognizer._split_windows(text)
        assert len(windows) > 1
        assert _reassemble(windows) == text
        # Every offset must point at exactly its window's text
        for offset, wtext in windows:
            assert text[offset:offset + len(wtext)] == wtext

    def test_window_size_bounded(self):
        text = ("Zeile mit Inhalt.\n" * 2000)  # ~36K chars, newline boundaries
        for _, wtext in FlairRecognizer._split_windows(text):
            assert len(wtext) <= FlairRecognizer.WINDOW_CHARS

    def test_pathological_no_boundaries_hard_cut(self):
        # A single giant "sentence" without any newline (markdown table row
        # worst case) must still be cut — process death is worse than a
        # split sentence.
        text = "x" * (FlairRecognizer.WINDOW_CHARS * 3 + 17)
        windows = FlairRecognizer._split_windows(text)
        assert _reassemble(windows) == text
        assert len(windows) == 4
        for _, wtext in windows:
            assert len(wtext) <= FlairRecognizer.WINDOW_CHARS

    def test_prefers_paragraph_boundary(self):
        # Blank line sits past the half-window mark → cut lands exactly there
        first = "A" * 4000 + "\n\n"
        text = first + "B" * 5000
        windows = FlairRecognizer._split_windows(text)
        assert windows[0][1] == first
        assert windows[1] == (len(first), "B" * 5000)

    def test_empty_text(self):
        assert FlairRecognizer._split_windows("") == []


class TestTorchThreadCap:
    """_get_model() must bound torch's intra-op thread count.

    Without this, each of the executor's 2 concurrent predict() calls (see
    anonymizer.py._get_executor) defaults to using every host core, so 2
    concurrent large-text analyses fight for the same cores instead of
    getting clean throughput (observed 2026-07-22: concurrent smart-anonymize
    calls slowed down worse than a simple FIFO queue would predict).
    """

    def test_get_model_caps_torch_threads(self, monkeypatch):
        pytest.importorskip("torch")
        import torch
        from flair.models import SequenceTagger

        monkeypatch.setattr(FlairRecognizer, "_shared_model", None)
        monkeypatch.setattr(SequenceTagger, "load", staticmethod(lambda path: object()))
        monkeypatch.setattr(
            "src.privacy.flair_recognizer._TORCH_THREADS", 2, raising=True
        )

        recognizer = FlairRecognizer()
        recognizer._get_model()

        assert torch.get_num_threads() == 2
