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
from .zahlenverweise import zahl_im_zitat, zahlenverweise


def block(text: str, name: str) -> Any:
    matches = re.findall(r"```" + re.escape(name) + r"\s*\n(.*?)\n```", text, re.S)
    if len(matches) != 1:
        raise ValueError(f"Genau ein {name}-JSON-Block erwartet")
    return json.loads(matches[0])


def pruefurteil(text: str) -> list[str]:
    """Blocking findings, read as meant: texts or objects that describe one finding."""
    return _urteil_lesen(text)[0]


def pruefhinweise(text: str) -> list[str]:
    """Non-blocking remarks; shown with the report, never counted as open findings."""
    return _urteil_lesen(text)[1]


def _urteil_lesen(text: str) -> tuple[list[str], list[str]]:
    value = block(text, "erkunder-pruefung")
    if (
        not isinstance(value, dict)
        or "befunde" not in value
        or not set(value) <= {"befunde", "hinweise"}
    ):
        raise ValueError("Prüfurteil benötigt befunde und höchstens noch hinweise")
    lists = []
    for key in ("befunde", "hinweise"):
        items = value.get(key, [])
        if not isinstance(items, list):
            raise ValueError(f"{key} ist keine Liste")
        lists.append(
            [_befundtext(key, index, item) for index, item in enumerate(items, 1)]
        )
    return lists[0], lists[1]


def _befundtext(key: str, index: int, item: Any) -> str:
    if isinstance(item, str) and item.strip():
        return item.strip()
    if isinstance(item, dict):
        parts = [
            f"{field}: {_feldtext(value)}"
            for field, value in item.items()
            if isinstance(field, str) and _feldtext(value)
        ]
        if parts:
            return "; ".join(parts)
    raise ValueError(
        f"{key}[{index}] ist weder ein Text noch ein lesbares Objekt: {item!r:.200}"
    )


def _feldtext(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False) if value else ""


def zahlenpruefung(text: str) -> dict:
    """Form of the reviewer's number list; content is compared in prueferzahlen."""
    data = block(text, "erkunder-zahlenpruefung")
    if not isinstance(data, dict) or not isinstance(data.get("vollstaendig"), bool):
        raise ValueError("Zahlenprüfung benötigt vollstaendig (true/false)")
    if not isinstance(data.get("zahlen"), list):
        raise ValueError("Zahlenprüfung benötigt zahlen als Liste")
    for index, item in enumerate(data["zahlen"], 1):
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str) and item[key].strip()
            for key in ("zitat", "zahl", "id")
        ):
            raise ValueError(
                f"Zahleneintrag {index} braucht zitat, zahl und id als nichtleere Texte"
            )
    return data


def pruefform(text: str) -> list[str]:
    """What keeps the coordinator from reading the review; goes back to the reviewer."""
    problems = []
    for reader in (pruefurteil, zahlenpruefung):
        try:
            reader(text)
        except (ValueError, TypeError) as error:
            problems.append(str(error))
    return problems


def _artifact(directory: Path, relative: str) -> str:
    """Paths in the evidence block are relative to the report folder: `skripte/...`."""
    path = directory / relative
    if not path.resolve().is_relative_to((directory / "skripte").resolve()):
        raise ValueError(
            f"Zahlenbeleg außerhalb skripte/: {relative!r}"
            " (Pfade gelten ab dem Gutachtenordner und beginnen mit skripte/)"
        )
    return read_text(path)


def _zahlendaten(directory: Path, text: str, sources: dict[str, list[str]]) -> tuple[dict, dict, dict[str, str]]:
    evidence = block(text, "erkunder-nachweis")
    channels = evidence["kanaele"]
    if not isinstance(channels, dict) or not channels:
        raise ValueError("Kanal-Kurznamen-Verzeichnis fehlt")
    _kanalverzeichnis(channels, sources)
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


def _kanalverzeichnis(channels: dict, sources: dict[str, list[str]]) -> None:
    known = {channel for names in sources.values() for channel in names}
    for alias, channel in channels.items():
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("Ungültiger Kanal-Kurzname")
        if not isinstance(channel, str) or channel not in known:
            raise ValueError(f"Kanal {alias}: kein echter Messkanal im Quellenmanifest ({channel!r})")


def zahlenbelege(directory: Path, text: str, sources: dict[str, list[str]]) -> tuple[list[str], dict[str, str]]:
    """Compare cited report values to script JSON, preserving the actual artifacts."""
    try:
        channels, values, artifacts = _zahlendaten(directory, text, sources)
        links = zahlenverweise(text)
        findings = [finding for shown, ident in links
                    if (finding := _vergleich(shown, ident, values, channels, sources))]
        if not links:
            findings.append("Gutachten enthält keine mit Skript-Ergebnissen verknüpften Zahlen")
        return findings, artifacts
    except (OSError, ValueError, KeyError, TypeError) as error:
        return [f"Quellnachweis unvollständig: {error}"], {}


def prueferzahlen(directory: Path, report: str, review: str, sources: dict[str, list[str]]) -> list[str]:
    """Reviewer lists numbers without a `zahl:` link; links are compared by machine."""
    try:
        data = zahlenpruefung(review)
    except (ValueError, TypeError) as error:
        return [f"Zahlenprüfung des Prüfers unlesbar: {error}"]
    findings = (
        []
        if data["vollstaendig"]
        else [
            "Zahlenprüfung unvollständig: der Prüfer konnte nicht jede unverknüpfte"
            " berechnete Zahl zuordnen"
        ]
    )
    try:
        channels, values, _ = _zahlendaten(directory, report, sources)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return [*findings, f"Zahlenprüfung unvollständig: {error}"]
    findings.extend(
        finding
        for item in data["zahlen"]
        if (finding := _prueferzahl(item, report, values, channels, sources))
    )
    return findings


def _prueferzahl(item: dict, report: str, values: dict, channels: dict,
                 sources: dict[str, list[str]]) -> str | None:
    quote, shown, ident = item["zitat"], item["zahl"], item["id"]
    line = fundstelle(report, quote)
    if line is None:
        return (
            f"Zahlenprüfung: Zahlenzitat nicht im Gutachten: {quote!r}"
            f" (Zahl {shown!r}, ID {ident})"
        )
    if not zahl_im_zitat(shown, quote):
        return (
            f"Zahlenprüfung: Zahl {shown!r} steht nicht im Zitat {quote!r}"
            f" (Gutachten Zeile {line})"
        )
    finding = _vergleich(shown, ident, values, channels, sources)
    return finding and f"{finding} (Gutachten Zeile {line}: {quote!r})"


def _leerraum_normiert(text: str) -> tuple[str, list[int]]:
    """Whitespace runs become one space, none next to a pipe; keeps char origins."""
    chars: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        if char.isspace():
            if chars and chars[-1] not in " |":
                chars.append(" ")
                origin.append(index)
            continue
        if char == "|" and chars and chars[-1] == " ":
            chars.pop()
            origin.pop()
        chars.append(char)
        origin.append(index)
    if chars and chars[-1] == " ":
        chars.pop()
        origin.pop()
    return "".join(chars), origin


def fundstelle(report: str, quote: str) -> int | None:
    """Report line where the quote starts; line breaks and cell spacing do not count."""
    needle, _ = _leerraum_normiert(quote)
    haystack, origin = _leerraum_normiert(report)
    position = haystack.find(needle) if needle else -1
    if position < 0:
        return None
    return report.count("\n", 0, origin[position]) + 1


def _vergleich(shown: str, ident: str, values: dict, channels: dict,
               sources: dict[str, list[str]]) -> str | None:
    context = _zahlkontext(shown, ident, values)
    try:
        if not ident.strip():
            raise ValueError("Verweis ohne Zahlen-ID")
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
            return f"{context}; Auswahl: {result['auswahl']}"
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        return f"{context}; Beleg fehlt/ungültig ({error})"
    return None


def _zahlkontext(shown: str, ident: str, values: dict) -> str:
    result = values.get(ident)
    value = result.get("wert", "<fehlt>") if isinstance(result, dict) else "<fehlt>"
    return f"Zahl {ident}: Text {shown!r}, Skript {value!r}"



def _belegform(result: dict, channels: dict, sources: dict[str, list[str]]) -> None:
    for key in ("auswahl", "raster", "einheit", "quelle"):
        if not isinstance(result[key], str) or not result[key].strip():
            raise ValueError(f"{key} fehlt")
    if not isinstance(result["kanaele"], list) or not result["kanaele"]:
        raise ValueError("Kanalzuordnung fehlt")
    source = result["quelle"].removeprefix("eingang/")
    if source not in sources:
        raise ValueError("Quelldatei nicht im geprüften Eingangsmanifest")
    _quellkanaele(result["kanaele"], channels, source, sources[source])


def _quellkanaele(aliases: list, channels: dict, source: str, known: list[str]) -> None:
    for alias in aliases:
        if not isinstance(alias, str) or alias not in channels:
            raise ValueError("Kanalzuordnung fehlt")
        if channels[alias] not in known:
            raise ValueError(f"Kanal {alias} nicht in Quelldatei {source}")


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
    if not str(value.get("begruendung") or "").strip():
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
