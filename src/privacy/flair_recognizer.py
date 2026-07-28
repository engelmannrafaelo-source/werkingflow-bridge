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
import os

from presidio_analyzer import EntityRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

logger = logging.getLogger(__name__)

# CPU oversubscription guard: PyTorch's default intra-op parallelism tries to
# use EVERY host core for a single predict() call. The Presidio executor (see
# anonymizer.py._get_executor) runs several of these predict() calls
# concurrently — without a cap, concurrent inferences each fighting for all
# cores causes CPU thrashing instead of clean throughput, and wall time under
# concurrent load grows worse than linearly with queue depth (observed
# 2026-07-22: 8 concurrent 10K-char smart-anonymize calls took up to 190s for
# the last one, more than the ~4x a pure FIFO 2-worker queue would predict).
# Bounding threads per inference lets concurrent predict() calls actually
# share the machine instead of contending for it. Applies regardless of
# inference device — it only bounds CPU-side ops (tokenisation etc.).
_TORCH_THREADS = int(os.getenv("SMART_ANONYMIZE_TORCH_THREADS", "2"))


def _resolve_device(torch_module):
    """Resolve which device Flair inference should run on.

    Controlled by PRIVACY_DEVICE:
      - unset / "auto" (default): CUDA if available, else CPU. Matches
        today's CPU-only hosts unchanged; picks up GPU automatically once a
        host has one, provided nvidia-container-toolkit exposes it.
      - "cuda": CUDA is required. If torch.cuda.is_available() is False,
        fail loud instead of silently running on CPU — a quiet CPU fallback
        would bring back the exact throughput bottleneck (~8-11s/analysis,
        2 concurrent slots) this GPU path exists to remove, without anyone
        noticing until performance regressed.
      - "cpu": force CPU even on a host with a visible GPU.
    """
    requested = os.getenv("PRIVACY_DEVICE", "auto").strip().lower()
    if requested == "":
        requested = "auto"
    cuda_available = torch_module.cuda.is_available()

    if requested == "cuda":
        if not cuda_available:
            raise RuntimeError(
                "PRIVACY_DEVICE=cuda was requested but torch.cuda.is_available() "
                "is False on this host. Refusing to silently fall back to CPU. "
                "Check the GPU driver / nvidia-container-toolkit setup, or set "
                "PRIVACY_DEVICE=auto (or unset it) to run on whatever is available."
            )
        device = torch_module.device("cuda")
    elif requested == "cpu":
        device = torch_module.device("cpu")
    elif requested == "auto":
        device = torch_module.device("cuda" if cuda_available else "cpu")
    else:
        raise RuntimeError(
            f"Unknown PRIVACY_DEVICE={requested!r} — expected 'auto', 'cpu' or 'cuda'."
        )

    logger.info(
        "Flair NER device resolved: %s (PRIVACY_DEVICE=%s, cuda_available=%s)",
        device, requested, cuda_available,
    )
    return device


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
            import torch
            import flair
            from flair.models import SequenceTagger
            torch.set_num_threads(_TORCH_THREADS)
            logger.info(
                "torch intra-op threads capped at %d (SMART_ANONYMIZE_TORCH_THREADS)",
                _TORCH_THREADS,
            )
            device = _resolve_device(torch)
            # flair.device is the global SequenceTagger.load() reads to map
            # tensors onto at load time; the explicit .to(device) below is a
            # belt-and-suspenders in case a future flair version stops
            # honouring it.
            flair.device = device
            logger.info("Loading Flair model %s on %s ...", self.model_path, device)
            FlairRecognizer._shared_model = SequenceTagger.load(self.model_path)
            FlairRecognizer._shared_model.to(device)
            logger.info("Flair model loaded")
        return FlairRecognizer._shared_model

    # Memory guard rails (OOM root-cause, 2026-07-03): a single predict() over
    # ALL sentences of a document let peak memory scale with document size —
    # a ~12K-char report spiked one uvicorn worker to >10GB anon-RSS and the
    # 16G container cgroup OOM-killed it mid-request (dmesg 09:39:48Z), which
    # the caller saw as "Server disconnected without sending a response".
    # Two bounds make the peak independent of document size:
    #   - WINDOW_CHARS: the text is pre-split into windows at paragraph/newline
    #     boundaries before sentence splitting, so segtok can never hand the
    #     model one giant pseudo-sentence (markdown tables!) and each predict()
    #     sees a bounded amount of text.
    #   - MINI_BATCH: transformer memory grows with batch_size × seq_len², so
    #     predictions run in small mini-batches instead of Flair's default 32.
    WINDOW_CHARS = 6000
    MINI_BATCH = 4

    @classmethod
    def _split_windows(cls, text: str) -> List[tuple]:
        """Split text into ≤WINDOW_CHARS windows at paragraph/newline boundaries.

        Returns (char_offset, window_text) tuples covering the full input.
        Boundary preference: blank line, then newline, then hard cut — a hard
        cut can split a sentence (worst case: one entity straddling the cut is
        missed), which is acceptable; process death is not.
        """
        windows = []
        pos = 0
        n = len(text)
        while pos < n:
            if n - pos <= cls.WINDOW_CHARS:
                windows.append((pos, text[pos:]))
                break
            cut_zone = text[pos:pos + cls.WINDOW_CHARS]
            half = cls.WINDOW_CHARS // 2
            para = cut_zone.rfind("\n\n")
            line = cut_zone.rfind("\n")
            if para >= half:
                cut = para + 2  # separator stays with the current window
            elif line >= half:
                cut = line + 1
            else:
                cut = cls.WINDOW_CHARS
            windows.append((pos, text[pos:pos + cut]))
            pos += cut
        return windows

    def analyze(
        self, text: str, entities: List[str], nlp_artifacts: NlpArtifacts = None
    ) -> List[RecognizerResult]:
        from flair.splitter import SegtokSentenceSplitter

        model = self._get_model()
        splitter = SegtokSentenceSplitter()

        results: List[RecognizerResult] = []
        for window_offset, window_text in self._split_windows(text):
            sentences = splitter.split(window_text)
            if not sentences:
                continue
            model.predict(sentences, mini_batch_size=self.MINI_BATCH)

            for sentence in sentences:
                # char offset of this sentence in `text` = window offset + offset in window
                base = window_offset + sentence.start_position
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
