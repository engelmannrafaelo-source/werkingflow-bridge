"""Zeitkontext-Hook fuer den Abo-CLI-Weg (docs/contracts/zeitkontext.md).

Die CLI ruft dieses Skript an den Hook-Grenzen UserPromptSubmit und
PostToolUse auf (settings-Datei je Lauf, src/claude_cli.py). Es rechnet aus
der ABSOLUTEN Frist (UTC-Epoch-Sekunden, Argument) die Restzeit und gibt sie
dem Modell als ``additionalContext`` mit. Die Frist kommt von Energy; hier
wird nichts entschieden und nichts abgebrochen.

Nur stdlib, keine Secrets, kein Netz. Jeder Fehler -> leere Ausgabe, Exit 0:
ein Hook darf den Lauf nie brechen.

Aufruf: python3 zeitkontext_hook.py --lauf-ende <epoch> [--phase-ende <epoch>]
        (statt der Argumente: --datei <json mit lauf_ende_epoch/phase_ende_epoch>)
"""
import json
import sys
import time
from datetime import datetime, timezone

ABGELAUFEN = "Frist abgelaufen"


def _iso(epoch: int) -> str:
    utc = timezone.utc  # noqa: UP017 — laeuft auch unter Python 3.10
    return datetime.fromtimestamp(epoch, tz=utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _teil(praefix: str, ende: int, jetzt: float) -> str:
    rest = int(ende - jetzt)
    if rest < 0:
        return f"{praefix}frist_utc={_iso(ende)} {ABGELAUFEN}"
    return f"{praefix}frist_utc={_iso(ende)} {praefix}rest_sekunden={rest}"


def kontext_text(lauf_ende: int, phase_ende=None, jetzt=None) -> str:
    """``ZEITKONTEXT frist_utc=<ISO> rest_sekunden=<N>`` (Lauf), dazu bei
    gesetzter Phase ``phase_frist_utc=… phase_rest_sekunden=…``."""
    jetzt = time.time() if jetzt is None else jetzt
    text = "ZEITKONTEXT " + _teil("", lauf_ende, jetzt)
    if phase_ende is not None:
        text += " " + _teil("phase_", phase_ende, jetzt)
    return text


def _epoch(wert) -> int:
    if isinstance(wert, bool) or not isinstance(wert, (int, str)):
        raise TypeError("kein int")
    return int(wert)


def _fristen(argv):
    lauf = phase = None
    i = 0
    while i < len(argv):
        name = argv[i]
        wert = argv[i + 1]  # IndexError -> Fehlerfall
        if name == "--lauf-ende":
            lauf = _epoch(wert)
        elif name == "--phase-ende":
            phase = _epoch(wert)
        elif name == "--datei":
            with open(wert, encoding="utf-8") as f:
                daten = json.load(f)
            lauf = _epoch(daten["lauf_ende_epoch"])
            if daten.get("phase_ende_epoch") is not None:
                phase = _epoch(daten["phase_ende_epoch"])
        else:
            raise ValueError(f"unbekanntes Argument {name!r}")
        i += 2
    if lauf is None:
        raise ValueError("lauf-ende fehlt")
    return lauf, phase


def main(argv=None) -> int:
    try:
        lauf, phase = _fristen(sys.argv[1:] if argv is None else argv)
        ereignis = json.loads(sys.stdin.read())["hook_event_name"]
        if not isinstance(ereignis, str) or not ereignis:
            raise ValueError("hook_event_name fehlt")
        ausgabe = json.dumps({
            "hookSpecificOutput": {
                "hookEventName": ereignis,
                "additionalContext": kontext_text(lauf, phase),
            }
        })
        sys.stdout.write(ausgabe + "\n")
    except BaseException:  # noqa: BLE001 — der Hook darf den Lauf nie brechen
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
