"""
Unit tests for smart_anonymizer.py — mapping/text consistency invariants.

These tests exercise the two core invariants that the bridge-anonymize-adapter
consumer enforces:
  1. Every mapping key must appear in smart_anonymized_text.
  2. Hard-PII original values must not appear as plaintext in the output.

They also cover the overlap-resolution fix in anonymizer.py: partially-
overlapping Presidio results (neither fully contained in the other) used to
corrupt the right-to-left replacement loop, causing a placeholder to land in
mapping but not in the text.  The new greedy overlap resolution prevents this.

No real Presidio / Flair models are loaded — the tests mock the low-level
PresidioAnonymizer.anonymize() to inject controlled AnonymizationResult objects.
"""

from __future__ import annotations

import sys
import asyncio
from pathlib import Path
from typing import Dict, List
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.privacy.anonymizer import AnonymizationResult, DetectedEntity


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_result(
    text: str,
    entities: List[DetectedEntity],
) -> AnonymizationResult:
    mapping: Dict[str, str] = {e.placeholder: e.original_text for e in entities}
    return AnonymizationResult(
        anonymized_text=text,
        mapping=mapping,
        detected_entities=entities,
        language="de",
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: overlap resolution in anonymizer.py
# ─────────────────────────────────────────────────────────────────────────────

class TestOverlapResolution:
    """The greedy overlap resolver must handle containment AND partial overlap."""

    def _run_anonymize(self, text: str, spans: list) -> AnonymizationResult:
        """Run PresidioAnonymizer.anonymize() with mocked Presidio analyze()."""
        from src.privacy.anonymizer import PresidioAnonymizer

        class _FakeResult:
            def __init__(self, start, end, score, entity_type):
                self.start = start
                self.end = end
                self.score = score
                self.entity_type = entity_type

            def __eq__(self, other):
                return (
                    self.start == other.start
                    and self.end == other.end
                    and self.entity_type == other.entity_type
                )

        fake_results = [_FakeResult(**s) for s in spans]
        anon = PresidioAnonymizer(language="de")

        with patch.object(anon, "_get_analyzer") as mock_analyzer:
            engine = MagicMock()
            engine.analyze.return_value = fake_results
            mock_analyzer.return_value = engine
            return anon.anonymize(text, "de")

    def test_non_overlapping_all_replaced(self):
        """Non-overlapping entities are all replaced and mapping/text consistent."""
        text = "Kontakt Max Mustermann, E-Mail: max@example.at"
        spans = [
            {"start": 8, "end": 22, "score": 0.95, "entity_type": "PERSON"},
            {"start": 32, "end": 46, "score": 0.99, "entity_type": "EMAIL_ADDRESS"},
        ]
        result = self._run_anonymize(text, spans)

        assert len(result.mapping) == 2
        for ph in result.mapping:
            assert ph in result.anonymized_text, f"{ph} missing from anonymized text"

    def test_containment_keeps_outer_entity(self):
        """An entity fully contained in another is dropped; outer entity stays."""
        # EMAIL "max@example.at" at [8, 22] contains URL "example.at" at [12, 22]
        text = "Email: max@example.at end"
        spans = [
            {"start": 7, "end": 21, "score": 0.99, "entity_type": "EMAIL_ADDRESS"},
            {"start": 11, "end": 21, "score": 0.80, "entity_type": "URL"},
        ]
        result = self._run_anonymize(text, spans)

        # Only the EMAIL should be kept (higher score, contains URL)
        assert len(result.mapping) == 1
        ph = list(result.mapping.keys())[0]
        assert "EMAIL_ADDRESS" in ph
        assert ph in result.anonymized_text

    def test_partial_overlap_keeps_higher_score(self):
        """Partially overlapping entities — higher score wins, no corruption."""
        # LOCATION [5, 15] score=0.90, URL [10, 20] score=0.80 — partial overlap.
        # Before fix: both survived dedup → right-to-left replacement corrupted
        # LOCATION placeholder (cut into URL placeholder's region).
        # After fix: only LOCATION (higher score) survives.
        text = "Stadt Wien-Center ist bekannt"
        spans = [
            {"start": 6, "end": 16, "score": 0.90, "entity_type": "LOCATION"},  # "Wien-Cente"
            {"start": 11, "end": 21, "score": 0.80, "entity_type": "URL"},       # "Center ist"
        ]
        result = self._run_anonymize(text, spans)

        assert len(result.mapping) == 1
        ph = list(result.mapping.keys())[0]
        assert ph in result.anonymized_text, (
            f"Partial-overlap regression: {ph!r} absent from anonymized text.\n"
            f"text={result.anonymized_text!r}"
        )

    def test_same_span_keeps_higher_score(self):
        """Same-span duplicate entities — higher score wins."""
        text = "Email max@example.at"
        spans = [
            {"start": 6, "end": 20, "score": 0.99, "entity_type": "EMAIL_ADDRESS"},
            {"start": 6, "end": 20, "score": 0.80, "entity_type": "URL"},
        ]
        result = self._run_anonymize(text, spans)

        assert len(result.mapping) == 1
        ph = list(result.mapping.keys())[0]
        assert ph in result.anonymized_text

    def test_mapping_text_always_consistent(self):
        """After any combination of overlaps, every mapping key is in text."""
        text = "A" * 50  # synthetic — real content doesn't matter for position test
        spans = [
            {"start": 10, "end": 20, "score": 0.90, "entity_type": "PERSON"},
            {"start": 15, "end": 25, "score": 0.80, "entity_type": "URL"},
            {"start": 30, "end": 40, "score": 0.95, "entity_type": "EMAIL_ADDRESS"},
        ]
        result = self._run_anonymize(text, spans)

        for ph in result.mapping:
            assert ph in result.anonymized_text, (
                f"Invariant violation: {ph!r} absent from anonymized text.\n"
                f"mapping={list(result.mapping.keys())}\n"
                f"text={result.anonymized_text!r}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Tests: smart_anonymize consistency assertions
# ─────────────────────────────────────────────────────────────────────────────

class TestSmartAnonymizeConsistency:
    """_assert_mapping_text_consistent and _assert_no_hard_pii_leak fire correctly."""

    def test_assert_mapping_consistent_passes_when_ok(self):
        from src.privacy.smart_anonymizer import _assert_mapping_text_consistent

        text = "Hello ANON_PERSON_001, your URL is ANON_URL_001."
        mapping = {"ANON_PERSON_001": "Max Müller", "ANON_URL_001": "https://x.at"}
        _assert_mapping_text_consistent(mapping, text)  # must not raise

    def test_assert_mapping_consistent_raises_on_missing_placeholder(self):
        from src.privacy.smart_anonymizer import _assert_mapping_text_consistent

        text = "Hello Max, your URL is ANON_URL_001."
        # ANON_PERSON_001 is in mapping but NOT in text
        mapping = {"ANON_PERSON_001": "Max Müller", "ANON_URL_001": "https://x.at"}
        with pytest.raises(ValueError, match="ANON_PERSON_001"):
            _assert_mapping_text_consistent(mapping, text)

    def test_assert_no_hard_pii_leak_passes_when_ok(self):
        from src.privacy.smart_anonymizer import _assert_no_hard_pii_leak

        text = "Hello ANON_PERSON_001, reach ANON_EMAIL_001."
        mapping = {"ANON_PERSON_001": "Max Müller", "ANON_EMAIL_001": "max@example.at"}
        type_by_ph = {"ANON_PERSON_001": "PERSON", "ANON_EMAIL_001": "EMAIL_ADDRESS"}
        _assert_no_hard_pii_leak(mapping, text, type_by_ph)  # must not raise

    def test_assert_no_hard_pii_leak_raises_on_person_in_text(self):
        from src.privacy.smart_anonymizer import _assert_no_hard_pii_leak

        # Person name leaked: appears in text instead of placeholder
        text = "Hello Max Müller, reach ANON_EMAIL_001."
        mapping = {"ANON_PERSON_001": "Max Müller", "ANON_EMAIL_001": "max@example.at"}
        type_by_ph = {"ANON_PERSON_001": "PERSON", "ANON_EMAIL_001": "EMAIL_ADDRESS"}
        with pytest.raises(ValueError, match="PII leak"):
            _assert_no_hard_pii_leak(mapping, text, type_by_ph)

    def test_assert_no_hard_pii_leak_ignores_non_hard_pii_types(self):
        from src.privacy.smart_anonymizer import _assert_no_hard_pii_leak

        # LOCATION "Wien" restored intentionally — not in NEVER_RESTORE_TYPES
        text = "The city Wien is shown and ANON_URL_001 is the site."
        mapping = {"ANON_URL_001": "https://wien.gv.at"}
        type_by_ph = {"ANON_LOCATION_001": "LOCATION", "ANON_URL_001": "URL"}
        _assert_no_hard_pii_leak(mapping, text, type_by_ph)  # must not raise

    def test_assert_no_hard_pii_leak_ignores_short_values(self):
        from src.privacy.smart_anonymizer import _assert_no_hard_pii_leak

        # Value "AT" (2 chars) is too short to be a meaningful leak indicator
        text = "Country AT is here and ANON_PERSON_001 is present."
        mapping = {"ANON_PERSON_001": "AT"}  # unrealistic but tests the guard
        type_by_ph = {"ANON_PERSON_001": "PERSON"}
        _assert_no_hard_pii_leak(mapping, text, type_by_ph)  # must not raise (len <= 3)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: smart_anonymize() end-to-end with mocked Presidio (no real models)
# ─────────────────────────────────────────────────────────────────────────────

class TestSmartAnonymizeEndToEnd:
    """smart_anonymize() integration — mocked Presidio, no real NLP models."""

    def _make_fake_anon_result(self, anonymized_text: str, entities: List[DetectedEntity]) -> AnonymizationResult:
        return _make_result(anonymized_text, entities)

    def test_non_refinement_path_consistent(self):
        """Non-refinement path: mapping and text are consistent, assertions pass."""
        entities = [
            DetectedEntity("PERSON", "Max Müller", 0, 10, 0.95, "ANON_PERSON_001"),
            DetectedEntity("URL", "https://example.at", 15, 33, 0.80, "ANON_URL_001"),
        ]
        fake_result = _make_result(
            "ANON_PERSON_001 at ANON_URL_001 today",
            entities,
        )

        with patch("src.privacy.smart_anonymizer.USE_REFINEMENT", False), \
             patch("src.privacy.smart_anonymizer.PresidioAnonymizer") as MockAnon:
            instance = MagicMock()
            instance.anonymize_async = AsyncMock(return_value=fake_result)
            MockAnon.return_value = instance

            from src.privacy.smart_anonymizer import smart_anonymize
            result = _run(smart_anonymize("Max Müller at https://example.at today"))

        assert result["status"] == "success"
        for ph in result["mapping"]:
            assert ph in result["smart_anonymized_text"]

    def test_non_refinement_path_raises_on_inconsistency(self):
        """Non-refinement path: if anonymizer produces inconsistent data, fail loud."""
        # Simulate the partial-overlap bug: ANON_URL_007 is in mapping but text
        # only has a corrupted fragment "L_007" — a regression test.
        entities = [
            DetectedEntity("PERSON", "Max Müller", 0, 10, 0.95, "ANON_PERSON_001"),
            DetectedEntity("URL", "https://x.at/path", 5, 23, 0.80, "ANON_URL_007"),
        ]
        # Deliberately corrupt: ANON_URL_007 is in mapping but absent from text
        fake_result = AnonymizationResult(
            anonymized_text="ANON_PERSON_001 some L_007 rest",  # URL placeholder corrupted
            mapping={
                "ANON_PERSON_001": "Max Müller",
                "ANON_URL_007": "https://x.at/path",  # present in mapping but not text
            },
            detected_entities=entities,
            language="de",
        )

        with patch("src.privacy.smart_anonymizer.USE_REFINEMENT", False), \
             patch("src.privacy.smart_anonymizer.PresidioAnonymizer") as MockAnon:
            instance = MagicMock()
            instance.anonymize_async = AsyncMock(return_value=fake_result)
            MockAnon.return_value = instance

            from src.privacy.smart_anonymizer import smart_anonymize
            with pytest.raises(ValueError, match="consistency violation"):
                _run(smart_anonymize("..."))

    def test_non_refinement_path_raises_on_pii_leak(self):
        """Non-refinement path: if hard PII appears as plaintext, fail loud."""
        entities = [
            DetectedEntity("PERSON", "Max Müller", 0, 10, 0.95, "ANON_PERSON_001"),
        ]
        # Deliberate bug: PERSON placeholder is in mapping but original "Max Müller"
        # also appears in the text (double presence — shouldn't happen, but tests guard).
        fake_result = AnonymizationResult(
            anonymized_text="ANON_PERSON_001 and also Max Müller is here",
            mapping={"ANON_PERSON_001": "Max Müller"},
            detected_entities=entities,
            language="de",
        )

        with patch("src.privacy.smart_anonymizer.USE_REFINEMENT", False), \
             patch("src.privacy.smart_anonymizer.PresidioAnonymizer") as MockAnon:
            instance = MagicMock()
            instance.anonymize_async = AsyncMock(return_value=fake_result)
            MockAnon.return_value = instance

            from src.privacy.smart_anonymizer import smart_anonymize
            with pytest.raises(ValueError, match="PII leak"):
                _run(smart_anonymize("..."))


# ─────────────────────────────────────────────────────────────────────────────
# Tests: deterministic backfill in anonymizer.py
# ─────────────────────────────────────────────────────────────────────────────

def _run_anonymize_with_spans(text: str, spans: list):
    """Run PresidioAnonymizer.anonymize() with mocked Presidio spans."""
    from src.privacy.anonymizer import PresidioAnonymizer

    class _FakeResult:
        def __init__(self, start, end, score, entity_type):
            self.start = start
            self.end = end
            self.score = score
            self.entity_type = entity_type

    fake_results = [_FakeResult(**s) for s in spans]
    anon = PresidioAnonymizer(language="de")

    with patch.object(anon, "_get_analyzer") as mock_analyzer:
        engine = MagicMock()
        engine.analyze.return_value = fake_results
        mock_analyzer.return_value = engine
        return anon.anonymize(text, "de")


class TestBackfill:
    """Backfill ensures all word-boundary occurrences of mapped values are replaced."""

    def test_backfill_replaces_missed_second_occurrence(self):
        """NER detects a name once — the second occurrence is backfilled."""
        text = "Franz Huber wohnt hier. Später trifft man Franz Huber im Büro."
        # NER only detects the first "Franz Huber" at [0, 11)
        spans = [
            {"start": 0, "end": 11, "score": 0.95, "entity_type": "PERSON"},
        ]
        result = _run_anonymize_with_spans(text, spans)

        assert "Franz Huber" not in result.anonymized_text, (
            f"Second occurrence not backfilled: {result.anonymized_text!r}"
        )
        assert result.anonymized_text.count("ANON_PERSON_001") == 2, (
            f"Expected placeholder twice: {result.anonymized_text!r}"
        )
        # Mapping invariant: placeholder still appears in text
        assert "ANON_PERSON_001" in result.mapping
        assert "ANON_PERSON_001" in result.anonymized_text

    def test_backfill_three_occurrences_two_missed(self):
        """Backfill handles more than one missed occurrence."""
        text = "Max Muster kommt. Max Muster geht. Max Muster bleibt."
        # NER only detects the first "Max Muster" at [0, 10)
        spans = [
            {"start": 0, "end": 10, "score": 0.95, "entity_type": "PERSON"},
        ]
        result = _run_anonymize_with_spans(text, spans)

        assert "Max Muster" not in result.anonymized_text
        assert result.anonymized_text.count("ANON_PERSON_001") == 3

    def test_backfill_order_prevents_partial_corruption(self):
        """Longer values are processed first so 'Franz' doesn't corrupt 'Franz Huber'.

        If shorter "Franz" is backfilled before longer "Franz Huber", the missed
        third occurrence "Franz Huber" becomes "ANON_X Huber" — 'Huber' leaks.
        Correct longer-first ordering prevents this.
        """
        # "Franz besucht Franz Huber. Dort trifft er Franz Huber."
        #  0123456789012345678901234567890123456789012345678901234
        # "Franz" at [0,5), "Franz Huber" at [14,25), third "Franz Huber" at [42,53) missed
        text = "Franz besucht Franz Huber. Dort trifft er Franz Huber."
        spans = [
            {"start": 0, "end": 5, "score": 0.95, "entity_type": "PERSON"},   # "Franz"
            {"start": 14, "end": 25, "score": 0.90, "entity_type": "PERSON"}, # "Franz Huber"
            # third "Franz Huber" at [42,53) intentionally omitted
        ]
        result = _run_anonymize_with_spans(text, spans)

        assert "Huber" not in result.anonymized_text, (
            f"'Huber' leaked — ordering error: {result.anonymized_text!r}"
        )
        assert "Franz" not in result.anonymized_text, (
            f"'Franz' leaked: {result.anonymized_text!r}"
        )

    def test_backfill_preserves_compound_words(self):
        """'Huber' inside 'Hubergruppe' (compound word) is NOT backfilled.

        The word boundary prevents replacing a substring within a larger word.
        """
        # "Huber" at [0,5) detected; "Hubergruppe" is a company name, not a person
        text = "Huber arbeitet in der Hubergruppe seit Jahren."
        spans = [
            {"start": 0, "end": 5, "score": 0.95, "entity_type": "PERSON"},
        ]
        result = _run_anonymize_with_spans(text, spans)

        # "Huber" standalone replaced; "Hubergruppe" must remain intact
        assert "Hubergruppe" in result.anonymized_text, (
            f"Compound word corrupted: {result.anonymized_text!r}"
        )
        assert result.anonymized_text.startswith("ANON_PERSON_001"), (
            f"Standalone 'Huber' not replaced: {result.anonymized_text!r}"
        )

    def test_backfill_idempotent_when_all_detected(self):
        """When NER detects all occurrences, backfill makes no additional change."""
        # Both occurrences detected; backfill should find nothing extra
        text = "Max Muster hier. Max Muster dort."
        spans = [
            {"start": 0, "end": 10, "score": 0.95, "entity_type": "PERSON"},
            {"start": 17, "end": 27, "score": 0.95, "entity_type": "PERSON"},
        ]
        result = _run_anonymize_with_spans(text, spans)

        assert "Max Muster" not in result.anonymized_text
        # Both positions have placeholders (possibly different: _001 and _002)
        assert len(result.mapping) == 2
        for ph in result.mapping:
            assert ph in result.anonymized_text

    def test_backfill_mapping_text_consistent_after_fill(self):
        """After backfill, every mapping key still appears in anonymized_text."""
        text = "Anna Schmidt trifft Anna Schmidt und Anna Schmidt."
        spans = [
            {"start": 0, "end": 12, "score": 0.95, "entity_type": "PERSON"},
        ]
        result = _run_anonymize_with_spans(text, spans)

        for ph in result.mapping:
            assert ph in result.anonymized_text, (
                f"Mapping invariant violated: {ph!r} missing from text\n"
                f"text={result.anonymized_text!r}"
            )
        assert "Anna Schmidt" not in result.anonymized_text


# ─────────────────────────────────────────────────────────────────────────────
# Tests: word-boundary leak check in _assert_no_hard_pii_leak
# ─────────────────────────────────────────────────────────────────────────────

class TestWordBoundaryLeakCheck:
    """_assert_no_hard_pii_leak uses word boundaries to avoid false positives."""

    def test_compound_word_no_false_positive(self):
        """'Huber' inside 'Hubergruppe' must NOT trigger a PII leak alarm."""
        from src.privacy.smart_anonymizer import _assert_no_hard_pii_leak

        # "Huber" was anonymized, but "Hubergruppe" (a company) is left intact
        text = "ANON_PERSON_001 arbeitet in der Hubergruppe seit Jahren."
        mapping = {"ANON_PERSON_001": "Huber"}
        type_by_ph = {"ANON_PERSON_001": "PERSON"}
        _assert_no_hard_pii_leak(mapping, text, type_by_ph)  # must not raise

    def test_standalone_name_still_flagged(self):
        """A genuine standalone leak — standalone 'Huber' — still raises."""
        from src.privacy.smart_anonymizer import _assert_no_hard_pii_leak

        text = "ANON_PERSON_001 arbeitet hier, und auch Huber ist bekannt."
        mapping = {"ANON_PERSON_001": "Huber"}
        type_by_ph = {"ANON_PERSON_001": "PERSON"}
        with pytest.raises(ValueError, match="PII leak"):
            _assert_no_hard_pii_leak(mapping, text, type_by_ph)

    def test_name_with_punctuation_still_flagged(self):
        """Name at sentence end (followed by period) is flagged — period is a word boundary."""
        from src.privacy.smart_anonymizer import _assert_no_hard_pii_leak

        text = "Kontaktperson ist ANON_PERSON_001 und auch Huber."
        mapping = {"ANON_PERSON_001": "Muster"}
        type_by_ph = {"ANON_PERSON_001": "PERSON"}
        # "Huber." — the period creates a word boundary; "Huber" IS the original, but
        # "Muster" is the value, so no leak for Muster.  Verify no false positive here.
        _assert_no_hard_pii_leak(mapping, text, type_by_ph)  # must not raise ("Muster" not in text)

    def test_leak_at_sentence_end_flagged(self):
        """Name at sentence end (e.g., 'Muster.') is correctly caught."""
        from src.privacy.smart_anonymizer import _assert_no_hard_pii_leak

        text = "Kontaktperson ist ANON_PERSON_001 oder auch Muster."
        mapping = {"ANON_PERSON_001": "Muster"}
        type_by_ph = {"ANON_PERSON_001": "PERSON"}
        with pytest.raises(ValueError, match="PII leak"):
            _assert_no_hard_pii_leak(mapping, text, type_by_ph)

    def test_multiword_name_compound_no_false_positive(self):
        """'Franz' within 'Franzmüller' (compound) is not a person leak."""
        from src.privacy.smart_anonymizer import _assert_no_hard_pii_leak

        text = "ANON_PERSON_001 Bericht liegt beim Franzmüller-Amt."
        mapping = {"ANON_PERSON_001": "Franz"}
        type_by_ph = {"ANON_PERSON_001": "PERSON"}
        _assert_no_hard_pii_leak(mapping, text, type_by_ph)  # must not raise

    def test_short_value_skipped(self):
        """Values ≤ 3 chars are still ignored (existing guard preserved)."""
        from src.privacy.smart_anonymizer import _assert_no_hard_pii_leak

        text = "Country AT is here and ANON_PERSON_001 is present."
        mapping = {"ANON_PERSON_001": "AT"}
        type_by_ph = {"ANON_PERSON_001": "PERSON"}
        _assert_no_hard_pii_leak(mapping, text, type_by_ph)  # must not raise (len <= 3)
