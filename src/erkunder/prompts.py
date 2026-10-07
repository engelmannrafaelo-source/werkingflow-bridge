"""Descriptive engineering briefs, versioned with the report metadata."""

from src.erkunder.models import Auftrag

PROMPT_VERSION = "erkunder-prompts/1"


def _eingang(a: Auftrag) -> str:
    return f"/arbeit/{a.bericht_id}/eingang/"


def _kontext(a: Auftrag) -> str:
    text = (
        f"In `{_eingang(a)}` liegen die Betriebsdaten ({a.gegenstand}) als Messreihen "
        "(`messdaten/`), die Kundenunterlagen als Text (`unterlagen/`), gegebenenfalls "
        "der Anlagenplan (`plan/`) und in `vorwissen.md` das, was Kollegen bereits "
        "aus den Unterlagen gelesen und berechnet haben. "
        f"Die Daten reichen von {a.datenstand.von} bis {a.datenstand.bis}; "
        f"heute ist {a.datenstand.heute}. Der Betreiber möchte wissen, ob die Anlage "
        "einwandfrei läuft"
    )
    if a.auftrag:
        text += f"; zusätzlich fragt er: {a.auftrag}"
    return text + f". Der Bericht dient {a.zweck}. "


def _vertiefung(a: Auftrag) -> str:
    if a.vertiefung_md is not None:
        return (
            " Zusätzlich bittet der Betreiber, Folgendes zu vertiefen: "
            f"siehe `{_eingang(a)}vertiefung.md`."
        )
    return ""


def erkunder_prompt(a: Auftrag) -> str:
    return (
        "Du bist erfahrener Anlagen- und Energieingenieur. "
        + _kontext(a)
        + "Untersuche die Daten so gründlich wie ein Gutachter: Auffälligkeiten, "
        "mögliche "
        "Ursachen mit Belegzahlen, Gegenproben, was vor Ort gemessen werden müsste. "
        "Was du aus Kanalnamen oder Plan annimmst, statt es zu messen, sagt der Leser "
        "gern einmal ausdrücklich. Rechne mit Python nach (`python` aus "
        "`/opt/rechnen`), "
        "lege deine Skripte in `skripte/` ab und schreibe das Ergebnis als "
        "`ergebnis.md` "
        "in diesen Ordner." + _vertiefung(a)
    )


def harmonisierung_prompt(a: Auftrag, ausgefallen: list[dict]) -> str:
    text = (
        "Du bist erfahrener Anlagen-/Energieingenieur und führst als Obergutachter "
        "unabhängige Gutachten zusammen. "
        + _kontext(a)
        + "In diesem Ordner liegen die Gutachten (`gutachten-N.md`), die Gutachter "
        "unabhängig voneinander zu denselben Daten erstellt haben. Aufgabe ist ein "
        "gemeinsames Gutachten: Prüfe die Befunde der Gutachten gegeneinander "
        "und gegen "
        "die Rohdaten (nachrechnen mit Python ist erlaubt), löse Widersprüche auf, "
        "übernimm nur, was belegt ist, und sieh nach, was fehlt. Nenne Belegzahlen, "
        "Gegenproben und was vor Ort gemessen werden müsste. Lege deine Skripte in "
        "`skripte/` ab. Schreibe das Ergebnis als `ergebnis.md` in diesen Ordner."
    )
    if ausgefallen:
        text += (
            f" Einer der drei Erkunder ist ausgefallen ({ausgefallen[0]['grund']}); "
            "dir liegen zwei Gutachten vor. Vermerke das im Gutachten."
        )
    return text + _vertiefung(a)


def pruefung_prompt(a: Auftrag) -> str:
    return (
        "Du bist erfahrener Anlagen-/Energieingenieur und prüfst als unabhängiger "
        "Zweitgutachter ein fremdes Gutachten. "
        + _kontext(a)
        + "In diesem Ordner liegt das Gutachten (`gutachten.md`), das ein anderer "
        "Gutachter dazu erstellt hat. Der Betreiber möchte sich darauf verlassen "
        "können. Aufgabe ist eine unabhängige Prüfung jeder inhaltlichen Aussage des "
        "Gutachtens gegen die Rohdaten und den Plan; nachrechnen mit Python ist "
        "ausdrücklich erlaubt. Urteile je Aussage: trägt / trägt teilweise / "
        "trägt nicht, "
        "und zeige jeweils deine Rechnung mit den Belegzahlen. Halte am Ende fest, was "
        "du nicht prüfen konntest und warum. Lege deine Skripte in `skripte/` ab. "
        "Schreibe das Ergebnis als `pruefung.md` in diesen Ordner. "
        f"Steht in `{_eingang(a)}vorwissen.md` ein Zähler, den das Gutachten zugunsten "
        "einer Ersatzgröße übergeht, dann gehört das Urteil darüber zu deiner Prüfung."
        + _vertiefung(a)
    )


def korrektur_prompt(a: Auftrag) -> str:
    return (
        "Ein unabhängiger Zweitgutachter hat dein Gutachten gegen die Rohdaten "
        "geprüft. "
        "Überarbeite es: Was er belegt widerlegt, korrigierst du; wo du nach eigener "
        "Nachrechnung bei deiner Aussage bleibst, begründest du das mit Zahlen. "
        + _kontext(a)
        + "Dein Gutachten liegt in `gutachten.md`, die Prüfung in "
        "`pruefung.md`. Lege deine Skripte in `skripte/` ab und schreibe die neue "
        "Fassung als `ergebnis.md` in diesen Ordner." + _vertiefung(a)
    )
