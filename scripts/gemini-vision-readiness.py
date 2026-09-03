#!/usr/bin/env python3
"""gemini-vision-readiness.py — ist der Gemini-Bildweg auf DIESER Bridge scharf?

WOFUER
------
Einmal auszufuehren, NACHDEM der Key hinterlegt und der Schalter umgelegt wurde
(Reihenfolge siehe docker/docker-compose.yml). Beantwortet die eine Frage, die
sonst erst der erste echte Messlauf beantwortet — und dann als unklarer
Fehlschlag: **kommt ein Bild wirklich bei Gemini an, und wird es richtig
abgerechnet?**

BEWUSST NICHT in ``scripts/bridge_smoke.py`` eingebaut. Der Smoke laeuft bei
JEDEM Deploy, auch auf prod — und dort fehlt der Gemini-Key ABSICHTLICH. Eine
Probe dort waere entweder ein Dauer-Rotlicht oder muesste "kein Key" als Erfolg
werten; beides macht den Deploy-Smoke unglaubwuerdiger, statt etwas zu sichern.

WARUM DAS TESTBILD NICHT WINZIG IST
-----------------------------------
Ein 1x1-Pixel-PNG haette denselben Nachweiswert wie ein Ping: es sagt "der
Endpunkt antwortet", nicht "der Weg traegt echte Arbeit". Genau diese
Verwechslung hat am 2026-09-03 an anderer Stelle eine Minimal-Probe gruen
melden lassen, waehrend echte Last in ein Kontingent-402 lief. Deshalb erzeugt
dieses Skript ein planaehnliches Bild in realistischer Groesse (1600x1100) und
prueft die Token-Zahl gegen eine Untergrenze: wer nur 258 Input-Tokens sieht,
hat ein Icon geschickt, keinen Plan.

Das Bild ist synthetisch (hier erzeugt, keine Kundendaten) — passend, weil
diese Probe auch auf einer frisch aufgesetzten Umgebung laufen koennen soll.

USAGE
-----
    python3 scripts/gemini-vision-readiness.py --base-url http://49.12.72.66:8000
    python3 scripts/gemini-vision-readiness.py --base-url ... --app-env development

Auth: AI_BRIDGE_API_KEY aus der Umgebung.
Exit 0 = der Weg traegt. Nicht-0 = er traegt nicht, mit Begruendung.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import urllib.error
import urllib.request

# Untergrenze fuer die Input-Tokens. Ein einzelnes 768x768-Kachelbild kostet
# 258 Tokens; ein Plan in der Groesse, die dieses Skript schickt, muss deutlich
# darueber liegen. Der Wert prueft "es wurde ein echtes Bild verarbeitet", nicht
# eine exakte Kachelrechnung — die ist Googles Sache und darf sich aendern.
MIN_PROMPT_TOKENS = 400


def build_planlike_png() -> bytes:
    """Ein synthetisches, planaehnliches Bild in realistischer Groesse."""
    from PIL import Image, ImageDraw

    w, h = 1600, 1100
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)

    # Grundriss-artige Rechtecke + Bemassungslinien: genug Struktur, dass eine
    # Antwort inhaltlich etwas sagen KANN (leere Flaeche liesse offen, ob das
    # Modell das Bild ueberhaupt gesehen hat).
    d.rectangle([80, 80, w - 80, h - 80], outline="black", width=4)
    d.rectangle([140, 150, 700, 560], outline="black", width=3)
    d.rectangle([760, 150, 1450, 560], outline="black", width=3)
    d.rectangle([140, 620, 1450, 1000], outline="black", width=3)
    for x in range(200, 1400, 120):
        d.line([x, 620, x, 1000], fill="black", width=1)
    d.line([140, 1040, 1450, 1040], fill="black", width=2)
    d.text((700, 1050), "13.10 m", fill="black")
    d.text((300, 340), "Raum A", fill="black")
    d.text((1000, 340), "Raum B", fill="black")
    d.text((700, 800), "Halle", fill="black")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def probe(base_url: str, api_key: str, app_env: str) -> dict:
    png_b64 = base64.b64encode(build_planlike_png()).decode()

    body = {
        "model": "claude-sonnet-5",          # bleibt Claude — Umschalter ist der Tier
        "provider_tier": "gemini-vision",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text":
                 "Beschreibe in zwei Saetzen, was auf diesem Grundriss zu sehen ist."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{png_b64}"}},
            ],
        }],
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-App-ID": "dev-tooling",
            "X-App-Env": app_env,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return {"status": r.status, "json": json.loads(r.read())}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "text": e.read().decode()[:1200]}
    except urllib.error.URLError as e:
        # Erreichbarkeit ist eine andere Aussage als "der Weg traegt nicht" —
        # sonst liest sich ein Tippfehler in --base-url wie ein kaputter
        # Gemini-Weg.
        return {"status": 0, "text": f"Bridge unter {base_url!r} nicht erreichbar: {e.reason}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    # Bewusst KEIN --model: welches Gemini-Modell bedient, entscheidet
    # GEMINI_VISION_MODEL auf der Bridge. Ein Schalter hier haette so getan, als
    # koennte der Aufrufer das steuern — er kann es nicht, und ein Feld, das
    # nichts tut, ist schlimmer als keins.
    ap.add_argument("--app-env", default="preview",
                    help="preview|development. 'production' MUSS abgewiesen werden.")
    args = ap.parse_args()

    api_key = os.getenv("AI_BRIDGE_API_KEY")
    if not api_key:
        print("FAIL: AI_BRIDGE_API_KEY nicht gesetzt.")
        return 2

    res = probe(args.base_url, api_key, args.app_env)

    if res["status"] == 0:
        print(f"FAIL: {res['text']}")
        print("      Das ist ein Erreichbarkeitsproblem, keine Aussage ueber den Gemini-Weg.")
        return 2
    if res["status"] == 403:
        print("FAIL: die Sperre hat abgewiesen (403). Das ist auf prod RICHTIG.")
        print("      Auf dev heisst es: Key oder BRIDGE_GEMINI_VISION_ENABLED fehlt,")
        print("      oder X-App-Env kam nicht als Nicht-Prod an.")
        print(res.get("text", ""))
        return 1
    if res["status"] != 200:
        print(f"FAIL: HTTP {res['status']}")
        print(res.get("text", ""))
        return 1

    data = res["json"]
    served = data.get("model", "")
    usage = data.get("usage") or {}
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")

    problems = []
    if not served.startswith("gemini-"):
        problems.append(
            f"served model ist {served!r} — der Aufruf wurde NICHT von Gemini beantwortet. "
            "Wahrscheinlich hat ein Pin/eine Regel den Tier ueberschrieben."
        )
    if not content.strip():
        problems.append("leere Antwort — Gemini hat nichts Verwertbares geliefert.")
    if usage.get("prompt_tokens", 0) < MIN_PROMPT_TOKENS:
        problems.append(
            f"nur {usage.get('prompt_tokens')} Input-Tokens (< {MIN_PROMPT_TOKENS}). "
            "Das Bild kam offenbar nicht als Bild an — ein durchgereichter "
            "Text-Platzhalter kostet fast nichts und sieht sonst wie Erfolg aus."
        )

    print(f"served_model : {served}")
    print(f"usage        : {usage}")
    print(f"antwort      : {content.strip()[:200]}")

    if problems:
        print("\nFAIL:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\nOK: Bild ist bei Gemini angekommen, Antwort verwertbar, Abrechnung plausibel.")
    print("Gegenprobe im Ledger (die Fahrspur ist die Grundlage jeder Auswertung):")
    print("  SELECT model, prompt_tokens, hypothetical_cost_eur FROM usage_events")
    print("   WHERE provider_metadata->>'api_key_lane' = 'vision_gemini'")
    print("   ORDER BY recorded_at DESC LIMIT 5;")
    return 0


if __name__ == "__main__":
    sys.exit(main())
