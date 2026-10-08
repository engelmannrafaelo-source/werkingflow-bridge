"""Machine evidence feeds the next engineering review; it never edits prose."""
from __future__ import annotations

import hashlib
import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .dateien import read_text
from .models import Urteil

ZAHL = re.compile(r"\[([-+]?\d+(?:[.,]\d+)?)\]\(zahl:([a-zA-Z0-9_-]+)\)")


def block(text: str, name: str) -> Any:
    matches = re.findall(r"```" + re.escape(name) + r"\s*\n(.*?)\n```", text, re.S)
    if len(matches) != 1:
        raise ValueError(f"Genau ein {name}-JSON-Block erwartet")
    return json.loads(matches[0])


def pruefurteil(text: str) -> list[str]:
    value = block(text, "erkunder-pruefung")
    if not isinstance(value, dict) or set(value) != {"befunde"}:
        raise ValueError("Prüfurteil benötigt befunde")
    findings = value["befunde"]
    if not isinstance(findings, list) or any(not isinstance(x, str) or not x.strip() for x in findings):
        raise ValueError("Prüfbefunde müssen eine Liste konkreter Texte sein")
    return findings


def _artifact(directory: Path, relative: str) -> str:
    path = directory / relative
    if not path.resolve().is_relative_to((directory / "skripte").resolve()):
        raise ValueError("Zahlenbeleg außerhalb skripte/")
    return read_text(path)


def _zahlendaten(directory: Path, text: str) -> tuple[dict, dict, dict[str, str]]:
    evidence = block(text, "erkunder-nachweis")
    channels = evidence["kanaele"]
    if not isinstance(channels, dict) or not channels:
        raise ValueError("Kanal-Kurznamen-Verzeichnis fehlt")
    results = evidence["ergebnisdateien"]
    if not isinstance(results, list) or not results:
        raise ValueError("Skript-Ergebnisdateien fehlen")
    values: dict[str, Any] = {}
    artifacts: dict[str, str] = {}
    for item in results:
        artifacts[item["skript"]] = _artifact(directory, item["skript"])
        output = _artifact(directory, item["ergebnis"])
        artifacts[item["ergebnis"]] = output
        loaded = json.loads(output)
        if not isinstance(loaded, dict) or values.keys() & loaded.keys():
            raise ValueError("Zahlen-IDs fehlen oder sind doppelt")
        values.update(loaded)
    return channels, values, artifacts


def zahlenbelege(directory: Path, text: str, sources: set[str] | None = None) -> tuple[list[str], dict[str, str]]:
    """Compare cited report values to script JSON, preserving the actual artifacts."""
    try:
        channels, values, artifacts = _zahlendaten(directory, text)
        findings = [finding for shown, ident in ZAHL.findall(text)
                    if (finding := _vergleich(shown, ident, values, channels, sources))]
        if not ZAHL.search(text):
            findings.append("Gutachten enthält keine mit Skript-Ergebnissen verknüpften Zahlen")
        return findings, artifacts
    except (OSError, ValueError, KeyError, TypeError) as error:
        return [f"Quellnachweis unvollständig: {error}"], {}


def prueferzahlen(directory: Path, report: str, review: str, sources: set[str]) -> list[str]:
    """The independent reviewer also identifies unlinked numerical statements."""
    try:
        data = block(review, "erkunder-zahlenpruefung")
        if data["vollstaendig"] is not True or not isinstance(data["zahlen"], list):
            raise ValueError("Vollständige Zahlenprüfung nicht bestätigt")
        channels, values, _ = _zahlendaten(directory, report)
        return [finding for item in data["zahlen"]
                if (finding := _prueferzahl(item, report, values, channels, sources))]
    except (OSError, ValueError, KeyError, TypeError) as error:
        return [f"Zahlenprüfung unvollständig: {error}"]


def _prueferzahl(item: dict, report: str, values: dict, channels: dict, sources: set[str]) -> str | None:
    quote, shown = item["zitat"], item["zahl"]
    if not quote or quote not in report or shown not in quote:
        return "Zahlenprüfung: Zahlenzitat nicht im Gutachten"
    return _vergleich(shown, item["id"], values, channels, sources)


def _vergleich(shown: str, ident: str, values: dict, channels: dict, sources: set[str] | None) -> str | None:
    try:
        result = values[ident]
        _belegform(result, channels, sources)
        value = Decimal(str(result["wert"]))
        printed = Decimal(shown.replace(",", "."))
        if not value.is_finite():
            raise ValueError("nicht endlicher Skriptwert")
        exponent = printed.as_tuple().exponent
        if not isinstance(exponent, int):
            raise ValueError("nicht endliche Textzahl")
        expected = value.quantize(Decimal(1).scaleb(exponent), rounding=ROUND_HALF_UP)
        if expected != printed:
            return f"Zahl {ident}: Text {shown}, Skript {value}; Auswahl: {result['auswahl']}"
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        return f"Zahl {ident}: Beleg fehlt/ungültig ({error})"
    return None



def _belegform(result: dict, channels: dict, sources: set[str] | None) -> None:
    for key in ("auswahl", "raster", "einheit", "quelle"):
        if not isinstance(result[key], str) or not result[key].strip():
            raise ValueError(f"{key} fehlt")
    if not result["kanaele"] or any(c not in channels for c in result["kanaele"]):
        raise ValueError("Kanalzuordnung fehlt")
    if sources is not None and result["quelle"].removeprefix("eingang/") not in sources:
        raise ValueError("Quelldatei nicht im geprüften Eingangsmanifest")


def manifest_text(order: Any) -> str:
    """Only paths and checksums; signed download URLs never enter the report."""
    rows = ["## Quelldateien", "", "| Pfad | SHA-256 |", "| --- | --- |"]
    rows.extend(f"| eingang/{f.ziel} | {f.sha256.lower()} |" for f in order.dateien)
    return "\n".join(rows)


def artifact_hashes(artifacts: dict[str, str]) -> dict[str, str]:
    return {name: hashlib.sha256(text.encode()).hexdigest() for name, text in artifacts.items()}


def pruefumfang(text: str, expected: list[Any]) -> list[str]:
    try:
        judgments = block(text, "erkunder-pruefumfang")
        if not isinstance(judgments, list):
            raise ValueError("Prüfumfang ist keine Liste")
        judgments = _urteile_validieren(judgments)
        actual = {(x["anlage"], x["dokument_id"], x["fehlerbild_id"]): x for x in judgments}
        wanted = {(x.anlage, x.dokument_id, x.fehlerbild_id): x for x in expected}
        if actual.keys() != wanted.keys() or len(actual) != len(judgments):
            raise ValueError("Prüfumfang enthält fehlende, unbekannte oder doppelte Punkte")
        return [finding for key, point in wanted.items()
                if (finding := _urteil(actual[key], point.fehlende_kanaele))]
    except (ValueError, KeyError, TypeError) as error:
        return [f"Bibliotheksurteile unvollständig: {error}"]


def _urteil(value: dict, missing: list[str]) -> str | None:
    status = value.get("status")
    if status not in ("bestaetigt", "widerlegt", "teilweise", "nicht_pruefbar"):
        return "Unbekanntes Bibliotheksurteil"
    if not set(missing) <= set(value.get("fehlende_kanaele", [])):
        return "Fehlende Kanäle im Bibliotheksurteil verschwiegen"
    if missing and status not in ("teilweise", "nicht_pruefbar"):
        return "Vollständiges Urteil trotz fehlender Kanäle"
    if status == "bestaetigt" and not value.get("befund_verweis"):
        return "Bestätigtes Fehlerbild ohne Befundverweis"
    if status == "nicht_pruefbar" and not value["fehlende_kanaele"]:
        return "Nicht prüfbar ohne fehlende Messung"
    if not value.get("begruendung"):
        return "Bibliotheksurteil ohne Begründung"
    return None


def _urteile_validieren(values: list) -> list[dict]:
    return [Urteil.model_validate(value).model_dump() for value in values]


def migriere_altauftrag(state: dict) -> dict:
    """Explicitly recognize completed v1 reports without inventing review results."""
    if state.get("zustand") != "fertig" or "gutachten_schritt" in state:
        return state
    if state.get("meta", {}).get("prompt_version") != "erkunder-prompts/1":
        raise ValueError("Abgeschlossener Auftrag ohne finalen Prüfschritt")
    suffix = "-korrektur" if state["korrekturkreis_gelaufen"] else ""
    state.update(gutachten_schritt="harmonisierung" + suffix, pruefung_schritt="pruefung" + suffix,
                 altauftrag_ungeprueft=True)
    state["meta"].update(offene_befunde_anzahl=None, pruefstatus="altauftrag_ungeprueft", korrekturrunden=None)
    return state
