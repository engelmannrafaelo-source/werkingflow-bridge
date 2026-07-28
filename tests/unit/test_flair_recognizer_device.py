"""
FlairRecognizer device selection (_resolve_device) — CPU/GPU switch tests.

The GPU rollout (2026-07-28) lets Flair run inference on CUDA instead of the
CPU-only path that has been the throughput bottleneck (~8-11s/analysis, 2
concurrent slots). PRIVACY_DEVICE controls this; the one invariant that must
never regress is the fail-loud contract: if the operator explicitly demands
GPU (PRIVACY_DEVICE=cuda) and none is visible, the service must refuse to
start rather than silently falling back to CPU — a silent fallback would
quietly bring the CPU-era bottleneck back with nothing in the logs to explain
why throughput regressed.

_resolve_device() takes the torch module as a parameter, so these tests use a
minimal fake torch instead of requiring a real (CPU or CUDA) torch install.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("presidio_analyzer")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.privacy.flair_recognizer import _resolve_device  # noqa: E402


def _fake_torch(cuda_available: bool):
    return SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: cuda_available),
        device=lambda name: f"device:{name}",
    )


class TestResolveDevice:
    def test_auto_picks_cuda_when_available(self, monkeypatch):
        monkeypatch.delenv("PRIVACY_DEVICE", raising=False)
        assert _resolve_device(_fake_torch(True)) == "device:cuda"

    def test_auto_picks_cpu_when_unavailable(self, monkeypatch):
        monkeypatch.delenv("PRIVACY_DEVICE", raising=False)
        assert _resolve_device(_fake_torch(False)) == "device:cpu"

    def test_explicit_auto_same_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRIVACY_DEVICE", "auto")
        assert _resolve_device(_fake_torch(True)) == "device:cuda"

    def test_explicit_cuda_with_gpu_present(self, monkeypatch):
        monkeypatch.setenv("PRIVACY_DEVICE", "cuda")
        assert _resolve_device(_fake_torch(True)) == "device:cuda"

    def test_explicit_cuda_without_gpu_fails_loud(self, monkeypatch):
        """The core regression guard: no silent CPU fallback when GPU is demanded."""
        monkeypatch.setenv("PRIVACY_DEVICE", "cuda")
        with pytest.raises(RuntimeError, match="cuda.*is False|refus"):
            _resolve_device(_fake_torch(False))

    def test_explicit_cpu_forces_cpu_even_with_gpu_present(self, monkeypatch):
        monkeypatch.setenv("PRIVACY_DEVICE", "cpu")
        assert _resolve_device(_fake_torch(True)) == "device:cpu"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("PRIVACY_DEVICE", "CUDA")
        assert _resolve_device(_fake_torch(True)) == "device:cuda"

    def test_unknown_value_fails_loud(self, monkeypatch):
        monkeypatch.setenv("PRIVACY_DEVICE", "tpu")
        with pytest.raises(RuntimeError, match="Unknown PRIVACY_DEVICE"):
            _resolve_device(_fake_torch(True))
