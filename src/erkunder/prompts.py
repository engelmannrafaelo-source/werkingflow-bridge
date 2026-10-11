"""Descriptive engineering briefs, versioned with the report metadata."""

from src.erkunder.models import Auftrag

PROMPT_VERSION = "erkunder-prompts/4"


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
    return text + f". Der Bericht dient {a.zweck}. " + _pruefwissen(a)


def _pruefwissen(a: Auftrag) -> str:
    if not any(d.ziel.startswith("pruefwissen/") for d in a.dateien):
        return ""
    return (
        "In `pruefwissen/` liegen ausgewählte Fachdokumente aus der Prüfbibliothek, "
        "je Datei ein Dokument, oben mit Kennung und Titel. Sie sind das Fachwissen "
        "hinter den Prüfpunkten im Vorwissen: Fehlerbilder, ihre Ursachen und wie man "
        "sie in Messdaten erkennt. Die Bibliotheks-Punkte im Vorwissen nennen die "
        "Kennung ihres Dokuments; lies gezielt die Dokumente, die du für einen "
        "Prüfpunkt oder einen eigenen Befund brauchst, und such darin mit Grep "
        "nach dem, was du nachschlagen willst. "
    )


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
        "in diesen Ordner." + NACHWEIS + _vertiefung(a)
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
    return text + NACHWEIS + _vertiefung(a)


def pruefung_prompt(a: Auftrag, rechenstand: bool = False) -> str:
    return (
        "Du bist erfahrener Anlagen-/Energieingenieur und prüfst als unabhängiger "
        "Zweitgutachter ein fremdes Gutachten. "
        + _kontext(a)
        + "In diesem Ordner liegt das Gutachten (`gutachten.md`), das ein anderer "
        "Gutachter dazu erstellt hat. "
        + (RECHENSTAND_PRUEFUNG if rechenstand else "")
        + "Der Betreiber möchte sich darauf verlassen "
        "können. Aufgabe ist eine unabhängige Prüfung jeder inhaltlichen Aussage des "
        "Gutachtens gegen die Rohdaten und den Plan; nachrechnen mit Python ist "
        "ausdrücklich erlaubt. Urteile je Aussage: trägt / trägt teilweise / "
        "trägt nicht, "
        "und zeige jeweils deine Rechnung mit den Belegzahlen. Halte am Ende fest, was "
        "du nicht prüfen konntest und warum. Lege deine Skripte in `skripte/` ab. "
        "Schreibe das Ergebnis als `pruefung.md` in diesen Ordner. "
        f"Steht in `{_eingang(a)}vorwissen.md` ein Zähler, den das Gutachten zugunsten "
        "einer Ersatzgröße übergeht, dann gehört das Urteil darüber zu deiner Prüfung."
        + PRUEFURTEIL + STATUSREGELN + _vertiefung(a)
    )


def korrektur_prompt(a: Auftrag, rechenstand: bool = False) -> str:
    return (
        "Ein unabhängiger Zweitgutachter hat dein Gutachten gegen die Rohdaten "
        "geprüft. "
        "Überarbeite es: Was er belegt widerlegt, korrigierst du; wo du nach eigener "
        "Nachrechnung bei deiner Aussage bleibst, begründest du das mit Zahlen. "
        + _kontext(a)
        + "Dein Gutachten liegt in `gutachten.md`, die Prüfung in "
        "`pruefung.md`. "
        + (RECHENSTAND_KORREKTUR if rechenstand else "")
        + "Lege deine Skripte in `skripte/` ab und schreibe die neue "
        "Fassung als `ergebnis.md` in diesen Ordner. Die Datei `maschinenbefunde.json` "
        "enthält ebenfalls zu klärende Befunde." + NACHWEIS + _vertiefung(a)
    )


STATUSREGELN = """
Die Bibliotheksurteile liest der Leitstand maschinell, mit dieser Bedeutung der
Status. Zu jedem Bibliotheks-Punkt nennt das Vorwissen die Kanäle, die in den Daten
fehlen; sie stehen auch in `fehlende_kanaele` des Urteils, zusammen mit weiteren,
die die Prüfung vermisst. `bestaetigt` und `widerlegt` sind vollständige Urteile aus
den Daten: die Kerngrößen des Fehlerbilds sind gemessen und zeigen es oder schließen
es aus.
Fehlt einem Punkt eine Messung, ist das Urteil `teilweise` oder `nicht_pruefbar`;
Indizien aus Unterlagen, Plan oder Prozesswirkung, die das Fehlerbild wahrscheinlich
oder unwahrscheinlich machen, stehen dann in der Begründung und in `sicherheit`.
`nicht_pruefbar` passt, wann immer die vorliegenden Daten keine Aussage über das
Fehlerbild tragen, ob wegen einer fehlenden Messung oder aus einem anderen Grund,
etwa weil der Zeitraum den nötigen Betriebszustand nicht enthält; die Begründung
sagt, woran es liegt. Ein bestätigtes Fehlerbild verweist in `befund_verweis` auf den
eigenen Befund, und jedes Urteil trägt eine Begründung.
"""

RECHENSTAND_PRUEFUNG = (
    "Unter `rechenstand/` liegt der Rechenstand dieser Fassung, so wie der Gutachter "
    "ihn hinterlassen hat: die Pfade aus seinem Nachweisblock (`skripte/…`) findest "
    "du unter `rechenstand/skripte/…`, samt den Ergebnisdateien mit den Zahlen-IDs. "
    "Damit ordnest du jede verknüpfte Zahl ihrem Skriptwert zu und kannst die "
    "Skripte lesen und ausführen. "
)

RECHENSTAND_KORREKTUR = (
    "In `skripte/` liegt bereits der Rechenstand deiner Vorfassung, Skripte und "
    "Ergebnisdateien, so wie der Prüfer ihn gesehen hat. Rechne darauf weiter; "
    "die Ergebnisdateien gehören danach zur neuen Fassung, also laufen geänderte "
    "Skripte erneut. "
)

NACHWEIS = """
Das Gutachten ist anhand seiner Quellen nachrechenbar. Das Verzeichnis
`eingang/quellen.md` nennt die vom Leitstand geprüften Dateipfade und SHA-256.
Beschreibe im Gutachten die verwendeten Quellen und ihr Raster. Ein Kanalverzeichnis
ordnet deine Kurznamen den vollständigen Messkanälen aus diesem Quellenverzeichnis zu.
Die zu jeder Zahl benannte Quelldatei enthält die angegebenen Messkanäle.
Deine Rechenskripte schreiben ihre Ergebnisse zusätzlich als JSON unter `skripte/`:
Schlüssel ist eine eindeutige Zahlen-ID; der Wert enthält `wert` (Zahl), `einheit`,
`quelle` (Pfad aus dem Quellenverzeichnis), `kanaele` (Kurznamen), `raster` und
`auswahl` (Zeitraum, Filter, Schwellen, Aggregation der tatsächlich ausgeführten Rechnung).
Berechnete Zahlen im Fließtext und Tabellen tragen Markdown-Verweise der Form
`[ZAHL](zahl:ID)`. Der maschinelle Vergleich rundet auf die angezeigten Dezimalstellen.
Ein JSON-Block mit dem Zaunnamen `erkunder-nachweis` enthält `kanaele` (Kurznamen
auf vollständige Kanalnamen) und `ergebnisdateien` (Liste mit `skript` und
`ergebnis`). Beide Pfade gelten ab dem Ordner, in dem `ergebnis.md` liegt, und
beginnen deshalb mit `skripte/`, etwa
`{"skript": "skripte/rechnen.py", "ergebnis": "skripte/ergebnisse.json"}`;
so findet der Leitstand die Dateien. Skripte und Ergebnisdateien gehören zur
aktuellen Gutachtenfassung; der Leitstand sichert sie zusammen und gibt sie mit dem
Gutachten an den Prüfer.
Im Vorwissen ausgewählte Bibliotheks-Punkte beantwortest du vollständig. Ein
JSON-Block `erkunder-pruefumfang` enthält eine Liste: `anlage`, `fehlerbild_id`,
`dokument_id`, `status` (bestaetigt/widerlegt/teilweise/nicht_pruefbar),
`fehlende_kanaele`, `befund_verweis` (bei bestaetigt Verweis auf den eigenen Befund),
`sicherheit` (0 bis 1) und `begruendung`. Auch ein leerer Prüfumfang steht als leere
Liste in diesem Block.
Die Bibliothek ist fachlicher Kontext; über das Ergebnis urteilst du
anhand der Anlage und Daten.
""" + STATUSREGELN

PRUEFURTEIL = """
Die Prüfung endet mit genau einem JSON-Block `erkunder-pruefung` mit `befunde`:
einer Liste konkreter offener Fehler und Widersprüche der vorliegenden Fassung.
Eine leere Liste bedeutet, dass diese Fassung trägt. Auch fehlende Zahlenbelege,
unbelegte Auswahlkriterien, Zahlen ohne Skript-Verknüpfung und fehlende oder
widersprüchliche Bibliotheksurteile sind Befunde. Dokumentierte Grenzen einer
korrekten Aussage sind keine Fehler. `maschinenbefunde.json` enthält ergänzende
rechnerische Befunde des Leitstands, die mitzuprüfen sind.
In einem zusätzlichen JSON-Block `erkunder-zahlenpruefung` steht `vollstaendig`
(true, sobald jede berechnete Zahl im Gutachten gegen einen Skriptbeleg zugeordnet
ist) und `zahlen`: alle berechneten Zahlen einschließlich Zahlen ohne Markdown-
Verknüpfung, jeweils mit `zitat` (wörtlicher Textausschnitt), `zahl` (exakte
Zahlenschreibweise im Zitat) und `id` (Schlüssel im Skript-JSON). Jeder ausdrücklich
gesetzte `zahl:`-Link wird unabhängig von seiner Zahlenschreibweise erfasst, auch
Referenzlinks. Die Liste enthält je Auftreten einen Eintrag mit dem lesbaren
Linktext (ohne Markdown-Hervorhebung) und der Zahlen-ID. Unlesbarer Linktext oder
ein fehlender Skriptwert erscheint als konkreter Befund mit beiden Seiten.
Der Leitstand gleicht die Liste mit allen Verweisen nach Linktext und ID ab und
vergleicht auch die unabhängig erfassten Textzahlen maschinell. Zahlen ohne
Beleg erzeugen einen Befund; Datumsangaben, Gliederungsnummern und Kanalnamen
sind Quellen-/Strukturangaben und keine berechneten Zahlen.
"""
