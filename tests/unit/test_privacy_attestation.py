"""
Tests for the value-free pseudonymization attestation (DSGVO accountability).

The central guarantee: an attestation proves types + counts of what was
pseudonymized but NEVER carries a plaintext PII value — even though the input
detected-entities do carry the original values.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.privacy.attestation import assert_value_free, build_attestation


def _entities():
    # Shape as returned by smart_anonymize: each carries the plaintext `original`.
    return [
        {"placeholder": "ANON_PERSON_001", "type": "PERSON", "original": "Rafael Engelmann"},
        {"placeholder": "ANON_LOCATION_001", "type": "LOCATION", "original": "Pelzgasse 18"},
        {"placeholder": "ANON_LOCATION_002", "type": "LOCATION", "original": "Wien"},
        {"placeholder": "ANON_ORGANIZATION_001", "type": "ORGANIZATION", "original": "Aldebaran-Privatstiftung"},
    ]


def test_build_attestation_counts_types():
    att = build_attestation(_entities(), mode="full")
    assert att["status"] == "success"
    assert att["anonymization_performed"] is True
    assert att["entity_counts_by_type"] == {"LOCATION": 2, "ORGANIZATION": 1, "PERSON": 1}
    assert att["total_entities"] == 4
    assert att["mode"] == "full"


def test_attestation_is_value_free_despite_valued_input():
    """The input entities contain 'Rafael Engelmann' etc.; the attestation must
    contain none of those plaintext values — only types and counts."""
    att = build_attestation(_entities())
    blob = repr(att)
    for value in ("Rafael Engelmann", "Pelzgasse", "Aldebaran", "Wien"):
        assert value not in blob
    # And it passes the structural guarantee.
    assert_value_free(att)


def test_basic_mode_uses_mapping_size_when_untyped():
    att = build_attestation([], mapping_size=7, mode="basic")
    assert att["entity_counts_by_type"] == {}
    assert att["total_entities"] == 7


def test_assert_value_free_rejects_value_field():
    bad = {"status": "success", "leaked": [{"type": "PERSON", "original": "Max"}]}
    with pytest.raises(ValueError):
        assert_value_free(bad)


def test_zero_entities_is_attested_not_empty():
    att = build_attestation([], mapping_size=0)
    assert att["anonymization_performed"] is True
    assert att["total_entities"] == 0
    assert att["entity_counts_by_type"] == {}
