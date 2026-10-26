"""Deploy-Nachweis fuer GET /health.

``scripts/bridge-deploy.sh`` schreibt am Ende eines Dev-Deploys (dieselbe
Stelle, die ``.bridge-deployed-sha`` bzw. ``.bridge-smoke-unproven`` schreibt)
eine kleine JSON-Datei auf das geteilte Log-Volume. ``/health`` gibt sie als
``deploy: {sha, smoke, am}`` aus, damit ein Prueffer (deploy-production auf
PROD-OPS) den Nachweis per HTTPS lesen kann, ohne SSH auf die Bridge.

Fail-closed: fehlt die Datei oder ist sie unlesbar/ungueltig, steht
``smoke: "unbekannt"`` mit dem Grund da — nie ``proven``.
"""

import json
import os
import re
from typing import Any, Dict

PROOF_FILE = os.getenv("BRIDGE_DEPLOY_PROOF_FILE", "/app/logs/deploy-proof.json")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SMOKE_VALUES = ("proven", "unproven")


def _unknown(grund: str) -> Dict[str, Any]:
    return {"sha": None, "smoke": "unbekannt", "am": None, "grund": grund}


def read_deploy_proof() -> Dict[str, Any]:
    """Liest den Deploy-Nachweis; jeder Mangel ergibt ``smoke: unbekannt``."""
    try:
        with open(PROOF_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return _unknown("proof_file_missing")
    except (OSError, ValueError) as e:
        return _unknown(f"proof_unreadable: {type(e).__name__}")

    if not isinstance(raw, dict):
        return _unknown("proof_malformed")
    sha, smoke, am = raw.get("sha"), raw.get("smoke"), raw.get("am")
    if not (isinstance(sha, str) and _SHA_RE.match(sha)):
        return _unknown("proof_malformed: sha")
    if smoke not in _SMOKE_VALUES:
        return _unknown("proof_malformed: smoke")
    if not (isinstance(am, str) and am):
        return _unknown("proof_malformed: am")
    return {"sha": sha, "smoke": smoke, "am": am}
