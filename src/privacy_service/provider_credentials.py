"""Fail before application imports if a direct provider credential is present.

The packaged contract is generated from packages/config/env-contracts.json.
Report names only: never log credential values, including in exceptions.
"""
import json
import os
from collections.abc import Mapping
from pathlib import Path

CONTRACT = json.loads(Path(__file__).with_name("provider-credentials.json").read_text())


def require_keyless_runtime(environ: Mapping[str, str] | None = None) -> None:
    source = os.environ if environ is None else environ
    present = sorted(name for name in CONTRACT["forbidden"] if name in source)
    if present:
        raise RuntimeError(
            "Energy provider credential boundary: forbidden names: "
            + ", ".join(present)
            + ". Remove before deploy with operator approval; use Bridge."
        )
