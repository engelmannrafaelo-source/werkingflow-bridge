"""
Flair-based NER recognizer for Presidio (local, German).

Replaces Presidio's default spaCy SpacyRecognizer for PERSON / LOCATION /
ORGANIZATION. `flair/ner-german-large` (XLM-RoBERTa) is markedly stronger on
German names/addresses than `de_core_news_lg` and — crucially — does NOT tag
every capitalised technical noun as an entity. Runs fully LOCAL on the privacy
service: no cleartext ever leaves for a cloud model.

MISC is deliberately NOT mapped: in the CoNLL-03 German tagset `MISC` is the
bucket that swallows technical terms, product names and units — the exact
over-detection we are eliminating. We map only the three hard PII classes.
"""
from typing import List, Optional
import logging

from presidio_analyzer import EntityRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

logger = logging.getLogger(__name__)


class FlairRecognizer(EntityRecognizer):
    ENTITIES = ["PERSON", "LOCATION", "ORGANIZATION"]

    # flair/ner-german-large emits CoNLL-03 tags. Map only hard PII; drop MISC.
    LABEL_TO_ENTITY = {
        "PER": "PERSON",
        "LOC": "LOCATION",
        "ORG": "ORGANIZATION",
    }

    DEFAULT_MODEL = "flair/ner-german-large"

    # Class-level singleton so the heavy SequenceTagger loads once per process,
    # even if the recognizer is re-instantiated.
    _shared_model = None

    def __init__(
        self,
        supported_language: str = "de",
        supported_entities: Optional[List[str]] = None,
        model_path: str = DEFAULT_MODEL,
    ):
        self.model_path = model_path
        super().__init__(
            supported_entities=supported_entities or self.ENTITIES,
            supported_language=supported_language,
            name="FlairRecognizer",
        )

    def load(self) -> None:
        """Presidio lifecycle hook — model is loaded lazily on first analyze."""
        pass

    def _get_model(self):
        if FlairRecognizer._shared_model is None:
            from flair.models import SequenceTagger
            logger.info("Loading Flair model %s ...", self.model_path)
            FlairRecognizer._shared_model = SequenceTagger.load(self.model_path)
            logger.info("Flair model loaded")
        return FlairRecognizer._shared_model

    def analyze(
        self, text: str, entities: List[str], nlp_artifacts: NlpArtifacts = None
    ) -> List[RecognizerResult]:
        from flair.splitter import SegtokSentenceSplitter

        model = self._get_model()
        splitter = SegtokSentenceSplitter()
        sentences = splitter.split(text)
        if not sentences:
            return []
        model.predict(sentences)

        results: List[RecognizerResult] = []
        for sentence in sentences:
            base = sentence.start_position  # char offset of this sentence in `text`
            for span in sentence.get_spans("ner"):
                mapped = self.LABEL_TO_ENTITY.get(span.tag)
                if mapped is None:
                    continue  # MISC and anything else → ignored (no over-detection)
                if entities and mapped not in entities:
                    continue
                results.append(
                    RecognizerResult(
                        entity_type=mapped,
                        start=base + span.start_position,
                        end=base + span.end_position,
                        score=span.score,
                    )
                )
        return results
