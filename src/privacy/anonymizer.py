"""
Presidio-based PII Anonymization Engine

DSGVO-compliant PII detection and pseudonymization using Microsoft Presidio.
Supports German and English languages with custom patterns for Austrian/German phone numbers.

Features:
- Async support for non-blocking FastAPI integration
- Thread pool execution for CPU-bound NLP operations
- Lazy loading of spaCy models

Based on: bacher-zt-ai-hub/src/services/presidio/anonymizer.py
"""

import os
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Lazy load Presidio to avoid import errors when disabled
_presidio_available: Optional[bool] = None
_analyzer_engine = None
_anonymizer_engine = None


def _check_presidio_available() -> bool:
    """Check if Presidio is installed and available."""
    global _presidio_available
    if _presidio_available is not None:
        return _presidio_available

    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_anonymizer import AnonymizerEngine
        _presidio_available = True
        logger.info("Presidio is available")
    except ImportError as e:
        _presidio_available = False
        logger.warning(f"Presidio not available: {e}")
        logger.warning("Install with: poetry add presidio-analyzer presidio-anonymizer")

    return _presidio_available


@dataclass
class DetectedEntity:
    """Represents a detected PII entity."""
    entity_type: str
    original_text: str
    start: int
    end: int
    confidence: float
    placeholder: str


@dataclass
class AnonymizationResult:
    """Result of anonymization operation."""
    anonymized_text: str
    mapping: Dict[str, str]  # placeholder -> original
    detected_entities: List[DetectedEntity]
    language: str
    entity_count: int = field(init=False)

    def __post_init__(self):
        self.entity_count = len(self.detected_entities)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'anonymizedText': self.anonymized_text,
            'mapping': self.mapping,
            'detectedEntities': [
                {
                    'type': e.entity_type,
                    'text': e.original_text,
                    'start': e.start,
                    'end': e.end,
                    'confidence': e.confidence,
                    'placeholder': e.placeholder
                }
                for e in self.detected_entities
            ],
            'language': self.language,
            'entityCount': self.entity_count
        }


# Thread pool for async operations (shared across instances)
_executor: Optional[ThreadPoolExecutor] = None


def _get_executor() -> ThreadPoolExecutor:
    """Get or create the shared thread pool executor."""
    global _executor
    if _executor is None:
        # Use 2 workers - Presidio is CPU-bound, more workers don't help
        _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="presidio")
        logger.info("Presidio thread pool initialized (2 workers)")
    return _executor


class PresidioAnonymizer:
    """
    DSGVO-compliant PII anonymization using Microsoft Presidio.

    Features:
    - German and English language support
    - Custom Austrian/German phone number patterns
    - Reversible pseudonymization (mapping preserved)
    - Thread-safe singleton engines
    - Async methods for non-blocking FastAPI integration
    """

    # Supported entity types for detection
    SUPPORTED_ENTITIES = [
        'PERSON',
        'EMAIL_ADDRESS',
        'PHONE_NUMBER',
        'LOCATION',
        'ORGANIZATION',
        'DATE_TIME',
        'IBAN_CODE',
        'CREDIT_CARD',
        'IP_ADDRESS',
        'URL'
    ]

    def __init__(self, language: str = 'de'):
        """
        Initialize the anonymizer.

        Args:
            language: Default language for analysis ('de' or 'en')
        """
        self.language = language
        self._analyzer = None
        self._anonymizer = None

    @property
    def is_available(self) -> bool:
        """Check if Presidio is available."""
        return _check_presidio_available()

    def _get_analyzer(self):
        """Get or create the analyzer engine (lazy initialization)."""
        global _analyzer_engine

        if not self.is_available:
            raise RuntimeError("Presidio is not installed. Install with: poetry add presidio-analyzer presidio-anonymizer")

        if _analyzer_engine is None:
            logger.info("Initializing Presidio Analyzer Engine...")
            _analyzer_engine = self._create_analyzer()
            logger.info("Presidio Analyzer Engine initialized")

        return _analyzer_engine

    def _get_anonymizer(self):
        """Get or create the anonymizer engine (lazy initialization)."""
        global _anonymizer_engine

        if not self.is_available:
            raise RuntimeError("Presidio is not installed")

        if _anonymizer_engine is None:
            from presidio_anonymizer import AnonymizerEngine
            _anonymizer_engine = AnonymizerEngine()
            logger.info("Presidio Anonymizer Engine initialized")

        return _anonymizer_engine

    def _create_analyzer(self):
        """Create Presidio Analyzer with German language support + custom patterns."""
        from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        # Configuration for spaCy NLP models
        configuration = {
            'nlp_engine_name': 'spacy',
            'models': [
                {'lang_code': 'de', 'model_name': 'de_core_news_lg'},
                {'lang_code': 'en', 'model_name': 'en_core_web_lg'}
            ]
        }

        try:
            provider = NlpEngineProvider(nlp_configuration=configuration)
            nlp_engine = provider.create_engine()
        except OSError as e:
            # spaCy model not installed - provide helpful error
            logger.error(f"spaCy model not found: {e}")
            logger.error("Install with: python -m spacy download de_core_news_lg && python -m spacy download en_core_web_lg")
            raise RuntimeError(
                "spaCy language models not installed. Run: "
                "python -m spacy download de_core_news_lg && "
                "python -m spacy download en_core_web_lg"
            ) from e

        analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine,
            supported_languages=['de', 'en']
        )

        # Add custom phone number recognizer for Austrian/German numbers
        phone_patterns = [
            # Austrian international format: +43 XXX XXX XX XX
            Pattern(
                name='austrian_phone_international',
                regex=r'\+43[\s\-]?\d{1,4}(?:[\s\-]?\d{2,4}){2,4}',
                score=0.9
            ),
            # German international format: +49 XXX XXX XX XX
            Pattern(
                name='german_phone_international',
                regex=r'\+49[\s\-]?\d{1,4}(?:[\s\-]?\d{2,4}){2,4}',
                score=0.9
            ),
            # Local format: 0XXX XXXXXX (only spaces/hyphens, NO slashes to avoid GZ conflicts)
            Pattern(
                name='local_phone',
                regex=r'\b0\d{2,4}[\s\-]\d{3,}\b',
                score=0.8
            ),
        ]

        phone_recognizer = PatternRecognizer(
            supported_entity='PHONE_NUMBER',
            name='phone_number_recognizer_de',
            patterns=phone_patterns,
            context=['Telefon', 'Tel', 'Phone', 'Kontakt', 'Mobil', 'Fax'],
            supported_language='de'
        )

        analyzer.registry.add_recognizer(phone_recognizer)

        return analyzer

    def anonymize(self, text: str, language: Optional[str] = None) -> AnonymizationResult:
        """
        Anonymize PII in text and return structured mapping.

        Args:
            text: Input text containing PII
            language: Language code ('de' or 'en'), defaults to instance language

        Returns:
            AnonymizationResult with anonymized text, mapping, and detected entities
        """
        if not text or not text.strip():
            return AnonymizationResult(
                anonymized_text=text,
                mapping={},
                detected_entities=[],
                language=language or self.language
            )

        lang = language or self.language
        analyzer = self._get_analyzer()

        # Analyze text for PII entities
        results = analyzer.analyze(
            text=text,
            language=lang,
            entities=self.SUPPORTED_ENTITIES
        )

        # Remove overlapping entities (keep longer one)
        # Example: EMAIL_ADDRESS "p.pichlbauer@getec.at" contains URL "getec.at"
        results_filtered = []
        for result in results:
            is_contained = False
            for other in results:
                if result == other:
                    continue
                # Check if this result is completely contained in another result
                if (result.start >= other.start and result.end <= other.end and
                    (result.start != other.start or result.end != other.end)):
                    is_contained = True
                    break
            if not is_contained:
                results_filtered.append(result)

        # Sort by position (reverse) to avoid offset issues during replacement
        results_sorted = sorted(results_filtered, key=lambda x: x.start, reverse=True)

        # Create structured placeholders and mapping
        entity_counters: Dict[str, int] = {}
        mapping: Dict[str, str] = {}
        detected_entities: List[DetectedEntity] = []
        anonymized_text = text

        for result in results_sorted:
            entity_type = result.entity_type
            original_text = text[result.start:result.end]

            # Increment counter for this entity type
            if entity_type not in entity_counters:
                entity_counters[entity_type] = 0
            entity_counters[entity_type] += 1

            # Create structured placeholder: ANON_PERSON_001, ANON_ORG_001, etc.
            placeholder = f"ANON_{entity_type}_{entity_counters[entity_type]:03d}"

            # Replace in text
            anonymized_text = (
                anonymized_text[:result.start] +
                placeholder +
                anonymized_text[result.end:]
            )

            # Store mapping (placeholder -> original)
            mapping[placeholder] = original_text

            # Track detected entity
            detected_entities.append(DetectedEntity(
                entity_type=entity_type,
                original_text=original_text,
                start=result.start,
                end=result.end,
                confidence=result.score,
                placeholder=placeholder
            ))

        logger.debug(f"Anonymized {len(detected_entities)} entities in text")

        return AnonymizationResult(
            anonymized_text=anonymized_text,
            mapping=mapping,
            detected_entities=detected_entities,
            language=lang
        )

    def deanonymize(self, anonymized_text: str, mapping: Dict[str, str]) -> str:
        """
        Restore original text from anonymized version using mapping.

        Args:
            anonymized_text: Text with ANON_XXX placeholders
            mapping: { 'ANON_PERSON_001': 'Patrick Pichlbauer', ... }

        Returns:
            Original text with PII restored
        """
        if not anonymized_text or not mapping:
            return anonymized_text

        result = anonymized_text

        # Sort by placeholder length (longest first) to avoid partial replacements
        sorted_placeholders = sorted(mapping.keys(), key=len, reverse=True)

        for placeholder in sorted_placeholders:
            original = mapping[placeholder]
            result = result.replace(placeholder, original)

        return result

    # =========================================================================
    # ASYNC METHODS (Non-blocking for FastAPI)
    # =========================================================================

    async def anonymize_async(self, text: str, language: Optional[str] = None) -> AnonymizationResult:
        """
        Async version of anonymize() - runs NLP in thread pool.

        This prevents blocking the FastAPI event loop during CPU-intensive
        Presidio/spaCy analysis.

        Args:
            text: Input text containing PII
            language: Language code ('de' or 'en'), defaults to instance language

        Returns:
            AnonymizationResult with anonymized text, mapping, and detected entities
        """
        if not text or not text.strip():
            return AnonymizationResult(
                anonymized_text=text,
                mapping={},
                detected_entities=[],
                language=language or self.language
            )

        # Run CPU-bound Presidio analysis in thread pool
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _get_executor(),
            self.anonymize,
            text,
            language
        )

    async def deanonymize_async(self, anonymized_text: str, mapping: Dict[str, str]) -> str:
        """
        Async version of deanonymize() - runs in thread pool.

        De-anonymization is fast (simple string replacement), but we provide
        an async version for API consistency.

        Args:
            anonymized_text: Text with ANON_XXX placeholders
            mapping: { 'ANON_PERSON_001': 'Patrick Pichlbauer', ... }

        Returns:
            Original text with PII restored
        """
        if not anonymized_text or not mapping:
            return anonymized_text

        # De-anonymization is fast, but run in executor for consistency
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _get_executor(),
            self.deanonymize,
            anonymized_text,
            mapping
        )
