"""Explizite Versionswahl (Rafael-Auftrag 17.11.2026, Bridge-Owner 51c8185c).

Nur auf ausdrueckliche Bestellung je Aufruf (Body-Feld ``modell_exakt: true``):
die angefragte, exakt registrierte Modell-ID wird unveraendert bedient. Kein
Alias, kein Fuzzy, kein Upgrade (always-latest) und kein Downgrade. Nur der
Direktweg (Anthropic Messages ueber den Pool), weil nur dort die
Anbieterverfuegbarkeit belegt ist (Models API, ``fallback_faehigkeit.ketten``).

Ohne das Feld bleibt alles wie bisher (``model_registry.resolve_model``).
Jede Unvertraeglichkeit ist ein lauter, nicht wiederholbarer 4xx mit Code;
nie ein stiller Aufruf mit einem anderen Modell.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException

UNBEKANNT = "unbekannt"


class ModellExaktNichtMoeglich(HTTPException):
    def __init__(self, code: str, grund: str, status: int = 400):
        self.code = code
        self.grund = grund
        super().__init__(status_code=status, detail={
            "message": f"modell_exakt: {code} ({grund})",
            "fehler_code": code,
            "grund": grund,
            "retryable": False,
        })


def bestellt(request: Any) -> bool:
    return getattr(request, "modell_exakt", None) is True


def aufloesen(model_input: Optional[str]) -> str:
    """Exakt registrierte ID -> dieselbe ID. Sonst laut ablehnen."""
    from src.model_registry import _MODEL_BY_ID
    if not isinstance(model_input, str) or model_input not in _MODEL_BY_ID:
        raise ModellExaktNichtMoeglich("modell_exakt_unbekannt", f"keine_registrierte_id:{model_input!r}")
    return model_input


async def verfuegbar_pruefen(model: str) -> None:
    """Gegen die Models-API-Liste des Pools (fail-closed, TTL wie der Fallback)."""
    from src.providers import fallback_faehigkeit as ff
    liste = await ff.ketten()
    if liste is None:
        raise ModellExaktNichtMoeglich("modell_verfuegbarkeit_unbekannt", "models_api_nicht_frisch", status=409)
    if ff._norm(model) not in liste:
        raise ModellExaktNichtMoeglich("modell_nicht_verfuegbar", f"nicht_gelistet:{model}")


def wahl(angefragt: Optional[str], aufgeloest: Optional[str], antwortend: Optional[str]) -> Dict[str, Any]:
    return {"exakt": True, "angefragt": angefragt, "aufgeloest": aufgeloest,
            "antwortend": antwortend if isinstance(antwortend, str) and antwortend else UNBEKANNT}
