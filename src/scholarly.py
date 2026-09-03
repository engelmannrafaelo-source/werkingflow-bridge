"""OA-Scholarly-Schicht für /v1/research (Weg A: deterministische Pre-Retrieval-Injektion).

Holt LEGALE Open-Access-Fachinhalte (OpenAlex-Abstracts + Volltext aus CORE UND der
OpenAlex-Content-API) zur Recherche-Query und formatiert sie als Kontext-Block, der VOR den
SuperClaude-Research-Prompt gehängt wird. Damit stützt sich der Agent auf peer-reviewte
Primärliteratur (inkl. der darin zitierten Normwerte VDI/DIN/ÖNORM/EN) statt nur auf
Snippets/Shop-Seiten. Kein Sci-Hub, kein Paywall-Bypass.

Design (bewusst dependency-frei + schnell, für die geteilte Bridge):
  - OpenAlex Search: Discovery + `abstract_inverted_index` → rekonstruierter Abstract (immer da, schnell)
  - OpenAlex Content-API (`src/openalex_content.py`): GROBID-XML-Volltext für die relevantesten
    Abstract-only-Treffer, hart credit-gedeckelt (siehe dortiger Modul-Docstring), fremdsprachige
    Volltexte werden erkannt und NICHT verwendet (Rückfall auf den Abstract)
  - CORE:     `fullText` direkt aus dem JSON (57M Repository-/Dissertations-Volltexte), best-effort
              (keyless rate-limitet → 429 wird fail-soft übersprungen); der Sentinel-Platzhalter
              "Not available for public API users." zählt NICHT als Volltext (siehe `_core()`)
  - KEIN PDF-Download/-Extraktion für CORE (Worker haben keine PDF-Lib; Downloads waren der
    Timeout-Treiber) — die Content-API liefert stattdessen bereits geparstes XML, kein PDF nötig
  - Alle Netz-Calls parallel + hart budgetiert; komplett fail-soft → bei jedem Fehler leerer String,
    Research läuft unverändert weiter.

Aktivierung nur wenn BRIDGE_SCHOLARLY_ENABLED=true UND request.research_mode=="academic".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from src import openalex_content

logger = logging.getLogger("scholarly")

MAILTO = os.getenv("SCHOLARLY_MAILTO", "office@werking.tools")
UA = {"User-Agent": f"werkingflow-scholarly (mailto:{MAILTO})"}
_HTTP_TIMEOUT = 15         # OpenAlex: kleine JSON-Antworten, 15s ist reichlich
# CORE liefert `fullText` INLINE: gemessen 1.2-2.3 MB und 7-12s je Query (20.08.2026, 8x parallel
# ab der dev-Bridge), Ausreisser bis 12s. Mit 15s lag der Abbruch mitten in der Normalverteilung —
# jeder CORE-Ausschlag wurde zum ReadTimeout, ohne dass CORE defekt war. Bleibt im 90s-Gesamtbudget.
_CORE_HTTP_TIMEOUT = 30
_MAX_ENTRIES = 12          # so viele Quellen maximal in den Kontext-Block
_EXCERPT_CHARS = 1600      # Zeichen je Quelle (CORE-Volltext gekürzt)

# CORE liefert das Feld `fullText` AUCH ohne Lizenz — gefüllt, aber wertlos: es enthält dann
# woertlich diesen Satz. Gemessen 03.09.2026 an 100 von 100 keyless-Treffern (Median-Textlaenge
# 35 Zeichen). Wer nur `if ft:` prueft, stempelt den Platzhalter als Volltext, meldet
# "12 Quellen, davon 12 Volltext" und schiebt 12 nutzlose Schnipsel in den Prompt — ein still
# gefaelschter Erfolg, der schlimmer ist als der sichtbare Ausfall.
_CORE_NO_LICENCE_SENTINEL = "Not available for public API users."

_PLANNER_SYS = (
    "Du bist ein Recherche-Query-Planner für die wissenschaftliche Literatur-API OpenAlex. "
    "Aus einem Energie-Ingenieur-Research-Prompt erzeugst du kurze SUCHBEGRIFFE (Keyword-Phrasen, "
    "keine Fragen/Sätze). Regeln: (a) mische ENGLISCH (internationale Journals) und DEUTSCH "
    "(Dissertationen/Forschungsberichte, wo ÖNORM/VDI/OIB zitiert werden); "
    "(b) NATIONALE Regelwerke (ÖNORM, TRVB, OIB-Richtlinie, DIN) sind KEINE Fachliteratur und in "
    "OpenAlex/CORE nicht indexiert — suche NIEMALS nach ihrer Bezeichnung. Nimm stattdessen die "
    "EUROPÄISCHE Entsprechung (EN/ISO) als EIGENE, KURZE Query; sie ist der staerkste Treffer-Anker. "
    "Ergänze getrennt davon eine Query auf die PHYSIKALISCHE GRÖSSE bzw. das Verfahren. "
    "(b1) HALTE QUERIES KURZ, zwei bis fünf Begriffe. Mehr Begriffe stapeln heisst NICHT genauer: "
    "gemessen 03.09.2026 lieferte \'EN 12101 smoke control\' 247 thematisch exakte Treffer, waehrend "
    "das ueberladene \'EN 12101-6 pressure differential system\' als Top-Treffer eine Arbeit ueber "
    "Bisphenol A und das Nervensystem zurueckgab. "
    "(b2) Frage NICHT auf Deutsch nach Fachbegriffen — \'Druckbelüftung Sicherheitstreppenraum\' und "
    "\'Druckbelüftungsanlage Treppenraum\' ergaben beide 0 Treffer. Deutsch lohnt nur fuer "
    "Dissertations-/Berichtstitel, nicht fuer Anlagentechnik. "
    "Beispiel fuer TRVB 112 (Druckbelüftung): \'EN 12101 smoke control\' UND "
    "\'stairwell pressurization door opening force\' als ZWEI getrennte kurze Queries; "
    "(c) für jedes technische Thema eine Konzept-Query. "
    "Antworte AUSSCHLIESSLICH als JSON-Array von Strings (8-14 Queries), nichts sonst."
)


def scholarly_profile() -> str:
    """'full' oder 'light' — entscheidet, ob diese Umgebung die externen Kontingente anfassen darf.

    Hintergrund: OpenAlex und CORE sind budgetiert bzw. lizenzpflichtig, und das Kontingent ist
    klein (OpenAlex: 1 USD/Tag mit kostenlosem Key, rund 100 Laeufe). Gemessen am 03.09.2026 wurde
    es ueberwiegend von automatischen Testlaeufen verbraucht: von den Recherchen auf den
    Prod-Workern entfielen 212 von 256 auf werking-report, der haeufigste Anlass war 126x der
    naechtliche Massentest mit absichtlich eingebauten Fehlern, und ALLE Aufrufe stammten von
    Identitaeten mit Heimat-Bridge 'dev' (12.332 Foederierungen, keine einzige nach 'prod').
    Der letzte erfolgreiche Abruf des Tages fiel um 01:02Z — mitten ins Testfenster. Bis tagsueber
    ein Mensch recherchierte, war das Guthaben weg. Ein groesseres Budget allein loest das nicht,
    weil dieselben Testlaeufe auch das neue aufbrauchen.

    Deshalb: NUR Umgebungen, die sich ausdruecklich als 'full' deklarieren, duerfen die echten
    Kontingente ziehen. Staging und lokale Entwicklung laufen light, also ohne jede externe
    Abfrage. Der Default ist bewusst 'light' — die teure Betriebsart muss man einschalten, nicht
    ausschalten. Ist die Schicht aktiv, aber kein Profil gesetzt, wird das einmalig als Warnung
    gemeldet, damit es nicht STILL degradiert; das ist genau der Fehler, den diese Datei sonst
    ueberall behebt.
    """
    raw = (os.getenv("BRIDGE_SCHOLARLY_PROFILE") or "").strip().lower()
    if raw in ("full", "light"):
        return raw
    if raw:
        logger.warning(
            f"research-cloud: BRIDGE_SCHOLARLY_PROFILE={raw!r} ist unbekannt (erlaubt: full|light) "
            f"— behandle die Umgebung als 'light', OA-Schicht bleibt aus"
        )
    return "light"


_profile_warned = False


def scholarly_enabled(research_mode: Optional[str]) -> bool:
    """Master-Kill-Switch (env) UND per-call opt-in UND Umgebungs-Profil. Default: aus."""
    global _profile_warned
    if os.getenv("BRIDGE_SCHOLARLY_ENABLED", "false").lower() != "true":
        return False
    if (research_mode or "standard") != "academic":
        return False
    if scholarly_profile() != "full":
        if not _profile_warned:
            _profile_warned = True
            logger.warning(
                "research-cloud: OA-Schicht ist aktiviert, aber diese Umgebung laeuft im "
                "Light-Profil — es werden KEINE externen Literatur-Kontingente verbraucht. "
                "Fuer den Produktivbetrieb BRIDGE_SCHOLARLY_PROFILE=full setzen."
            )
        return False
    return True


def _get(url: str, **kw) -> requests.Response:
    kw.setdefault("headers", {}).update(UA)
    kw.setdefault("timeout", _HTTP_TIMEOUT)
    return requests.get(url, **kw)


def _reconstruct_abstract(inv: Optional[Dict[str, List[int]]]) -> str:
    """OpenAlex liefert Abstracts als inverted index {wort: [positionen]} → Fließtext."""
    if not inv:
        return ""
    positions: List[tuple] = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)


def _openalex(query: str, n: int) -> List[Dict[str, Any]]:
    # OpenAlex ist seit 2026 budgetiert (search = 1 USD je 1.000 Calls). OHNE Schluessel gibt es
    # 0,10 USD/Tag = 100 Calls; ein Recherche-Lauf verbraucht 8-14 davon, das Kontingent ist also
    # nach rund zehn Laeufen leer. Ein KOSTENLOSER Account-Key verzehnfacht es auf 1 USD/Tag und
    # schaltet zusaetzlich das Volltext-Archiv frei. Der Key darf als Query-Parameter oder als
    # Bearer-Header mitgehen; wir nehmen den Parameter, weil `_get` die Header fuer den
    # User-Agent belegt. Fehlt er, laeuft alles unveraendert weiter — nur eben auf dem
    # Zehntel-Budget, und der 429-Zweig unten macht das sichtbar.
    params = {"search": query, "per-page": n, "mailto": MAILTO,
              # `language` und `authorships` kosten nichts extra (derselbe Such-Call), liefern aber
              # die Herkunft: der Brandschutz-Ingenieur muss einordnen koennen, aus welchem
              # Rechtsraum eine Arbeit stammt. Ein Laenderfilter waere der falsche Weg — gemessen
              # 03.09.2026 zerstoert institutions.country_code:at|de|ch die Relevanz komplett
              # (Top-Treffer wurde eine Arbeit ueber Fettgewebe bei Adipositas). Anzeigen statt filtern.
              # "id" kostet nichts extra (derselbe Such-Call) und ist der Schlüssel für die
              # Content-API-Anreicherung unten (_enrich_openalex_fulltext) — ohne ihn keine Volltextabfrage.
              "select": "id,title,publication_year,doi,open_access,abstract_inverted_index,language,authorships"}
    oa_key = os.getenv("OPENALEX_API_KEY")
    if oa_key:
        params["api_key"] = oa_key
    r = _get("https://api.openalex.org/works", params=params)
    if r.status_code != 200:
        # War bisher stumm — und verdeckte damit den Budget-Ausfall: OpenAlex ist seit 2026
        # kostenpflichtig (search = 1 USD je 1.000 Calls) und antwortet bei erschoepftem
        # Tagesguthaben 429 mit "Insufficient budget". Ohne diese Zeile sieht der Betrieb nur
        # leere Ergebnisse. 429 ist deshalb ein Betriebszustand, kein Zufall.
        logger.warning(
            f"research-cloud: OpenAlex antwortet {r.status_code} — Abstract-Schicht liefert nichts "
            f"({(r.text or '')[:160]})"
        )
        return []
    out = []
    for rang, w in enumerate(r.json().get("results", [])):
        oa = w.get("open_access") or {}
        abstract = _reconstruct_abstract(w.get("abstract_inverted_index"))
        laender = []
        for a in (w.get("authorships") or []):
            for inst in (a.get("institutions") or []):
                cc = inst.get("country_code")
                if cc and cc not in laender:
                    laender.append(cc)
        out.append({
            "id": w.get("id"),
            "title": (w.get("title") or "")[:200],
            "laender": laender[:4],
            "sprache": w.get("language"),
            "year": w.get("publication_year"),
            "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
            "url": oa.get("oa_url"),
            "text": abstract,
            "kind": "abstract",
            "source": "OpenAlex",
            "rang": rang,
        })
    return out


def _core(query: str, n: int) -> List[Dict[str, Any]]:
    # CORE keyless (freier Rate-Limit, 429 → fail-soft []). Trailing-Slash nötig. fullText kommt
    # direkt im JSON — das ist der eigentliche Mehrwert (echter Volltext ohne PDF-Download).
    key = os.getenv("CORE_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        r = _get("https://api.core.ac.uk/v3/search/works/", headers=headers,
                 params={"q": query, "limit": n}, timeout=_CORE_HTTP_TIMEOUT)
        if r.status_code != 200:
            if r.status_code == 429:
                # Keyless-Rate-Limit: Schicht degradiert STILL auf OpenAlex-Abstracts —
                # unter Last sichtbar machen (CORE_API_KEY hebt das Limit).
                logger.warning("research-cloud: CORE rate-limited (429, keyless) — Volltext-Anteil degradiert auf Abstracts")
            else:
                # JEDER andere Fehler war bisher komplett stumm. Genau daran ist der Ausfall vom
                # August/September 2026 wochenlang unbemerkt geblieben: die abgelaufene Trial-Lizenz
                # antwortete 401, `_core` gab still [] zurueck, und niemand sah es.
                logger.warning(
                    f"research-cloud: CORE antwortet {r.status_code} — Volltext-Schicht liefert nichts "
                    f"({(r.text or '')[:160]})"
                )
            return []
        out = []
        placeholders = 0
        for rang, w in enumerate(r.json().get("results", [])):
            ft = w.get("fullText") or ""
            if ft.strip() == _CORE_NO_LICENCE_SENTINEL:
                ft = ""          # Platzhalter ist KEIN Volltext
                placeholders += 1
            out.append({
                "title": (w.get("title") or "")[:200],
                "year": w.get("yearPublished"),
                "doi": w.get("doi"),
                "url": w.get("downloadUrl") or (w.get("links") or [{}])[0].get("url") if w.get("links") else w.get("downloadUrl"),
                "text": ft,
                "kind": "fulltext" if ft else "meta",
                "source": "CORE",
                "rang": rang,
            })
        if placeholders:
            # Sichtbar machen, dass CORE unlizenziert laeuft — sonst sieht der Betrieb nur
            # "weniger Volltext" und sucht die Ursache an der falschen Stelle.
            logger.warning(
                f"research-cloud: CORE ohne gueltige Lizenz — {placeholders}/{len(out)} Treffer "
                f"lieferten statt Volltext nur den Platzhalter; Volltext-Schicht ist wirkungslos"
            )
        return out
    except (requests.RequestException, ValueError) as e:
        # Ebene beachten: _core() laeuft EINMAL PRO GEPLANTER SUCHQUERY (8-14 parallel je Lauf).
        # Eine ausgefallene Teil-Query heisst NICHT, dass der Lauf Volltexte verliert — die
        # anderen Queries liefern weiter. Deshalb INFO und bewusst OHNE Alarm-Marker: gemeldet
        # wird die Lauf-Ebene (siehe _retrieve_and_format), nicht jede einzelne Query. Sichtbar
        # bleibt es trotzdem — nur eben nicht als Alarm (Messung 20.08.2026: 354 solcher Zeilen
        # in 3 Wochen, davon fuehrten die wenigsten zu einem Lauf ohne Volltext).
        logger.info(f"CORE-Teilquery ohne Ergebnis ({type(e).__name__}: {e}) — restliche Queries laufen weiter")
        return []


def _enrich_openalex_fulltext(candidates: List[Dict[str, Any]]) -> None:
    """Reichert die aussichtsreichsten Abstract-only-OpenAlex-Treffer mit echtem Volltext an
    (src/openalex_content.py, GROBID-XML). Mutiert die Paper-Dicts in `candidates` in place.

    Absichtlich AUF EINER VORSORTIERTEN TEILMENGE aufgerufen (siehe Aufrufer): der geteilte
    OPENALEX_API_KEY hat nur ~100 Content-Abrufe/Tag (100 Credits/Abruf, 10000 Credits/Tag —
    siehe Modul-Docstring), ein einzelner Research-Lauf darf das Tagesbudget nicht mit einem
    Abruf pro Treffer leerräumen. `openalex_content.fetch_fulltexts` erzwingt den Deckel.

    Läuft NUR für Treffer, die noch keinen Volltext haben (kind=="abstract") — hat CORE für ein
    Werk bereits Volltext geliefert, sind hier keine Credits nötig. Bewusst sequenziell (der
    Deckel lässt ohnehin nur wenige Abrufe zu, ein Thread-Pool für 2-3 Calls wäre Overhead ohne
    messbaren Zeitgewinn innerhalb des 90s-Gesamtbudgets von `build_oa_context`)."""
    key = os.getenv("OPENALEX_API_KEY")
    if not key:
        return
    by_id = {p["id"]: p for p in candidates
             if p.get("source") == "OpenAlex" and p.get("kind") == "abstract" and p.get("id")}
    if not by_id:
        return
    results = openalex_content.fetch_fulltexts(list(by_id.keys()), key)
    for work_id, res in results.items():
        paper = by_id[work_id]
        paper["text"] = res["text"]
        paper["kind"] = res["kind"]


def _retrieve_and_format(queries: List[str], per_query: int) -> str:
    """Parallel je Query OpenAlex+CORE, dedupe, Kontext-Block bauen. Keine PDF-Downloads."""
    papers: List[Dict[str, Any]] = []
    seen = set()

    def fetch(q: str) -> List[Dict[str, Any]]:
        res = []
        try:
            res += _openalex(q, per_query)
        except requests.RequestException:
            pass
        res += _core(q, per_query)
        return res

    # Alle Query-Fetches parallel (Discovery ist schnell; kein Download)
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(queries)))) as pool:
        futs = [pool.submit(fetch, q) for q in queries]
        for f in as_completed(futs):
            try:
                hits = f.result()
            except Exception:
                continue
            for p in hits:
                key = (p.get("doi") or "").lower() or p.get("title", "").lower()[:60]
                if key and key not in seen and p.get("text"):
                    seen.add(key)
                    papers.append(p)

    if not papers:
        logger.warning("OA-Retrieval ohne verwertbare Quellen (0 Treffer mit Text) — Kontext-Block entfällt")
        return ""

    # Vorläufig nur nach Relevanz sortieren (noch ohne Textlänge/Volltext-Bonus, den die
    # Content-API-Anreicherung gleich selbst verändert) — legt fest, welche Abstract-only-
    # OpenAlex-Treffer die knappen Content-API-Credits bekommen: die relevantesten zuerst,
    # begrenzt auf das, was ohnehin im finalen Kontext-Block landen würde (_MAX_ENTRIES).
    papers.sort(key=lambda p: (p["kind"] != "fulltext", p.get("rang", 999)))
    _enrich_openalex_fulltext(papers[:_MAX_ENTRIES])

    # Volltext zuerst, danach nach RELEVANZ (Rang in der Trefferliste der jeweiligen Query),
    # erst zuletzt nach Textlänge. Vorher wurde ausschliesslich nach Laenge sortiert — damit
    # gewann der laengste Abstract unabhaengig davon, ob er zum Thema gehoert. Messung
    # 03.09.2026 an der Druckbelueftungs-Frage: auf Platz zwei stand ein Aufsatz ueber
    # Insektizid-Formulierungen, nur weil dessen Abstract laenger war. Beide Quellen liefern
    # relevanzsortiert, dieser Rang war bisher weggeworfen worden.
    papers.sort(key=lambda p: (p["kind"] != "fulltext", p.get("rang", 999), -len(p.get("text", ""))))
    fulltext_n = sum(1 for p in papers[:_MAX_ENTRIES] if p["kind"] == "fulltext")
    _msg = f"OA-Kontext: {min(len(papers), _MAX_ENTRIES)} Quellen, davon {fulltext_n} Volltext"
    if fulltext_n == 0:
        # DAS ist die echte Degradierung: der Lauf stuetzt sich nur noch auf Abstracts.
        # Genau diese Zeile ueberwacht research-cloud-monitor.py (Marker "davon 0 Volltext").
        logger.warning(f"research-cloud: {_msg} — Recherche degradiert auf Abstracts")
    else:
        logger.info(_msg)
    blocks = []
    for p in papers[:_MAX_ENTRIES]:
        excerpt = re.sub(r"\s+", " ", p["text"]).strip()[:_EXCERPT_CHARS]
        doi = f" · doi:{p['doi']}" if p.get("doi") else ""
        url = f"\nURL: {p['url']}" if p.get("url") else ""
        tag = "Volltext" if p["kind"] == "fulltext" else "Abstract"
        herkunft = ""
        if p.get("laender"):
            herkunft += ", Institutionen: " + "/".join(p["laender"])
        if p.get("sprache"):
            herkunft += f", Sprache: {p['sprache']}"
        blocks.append(f"### {p['title']} ({p.get('year')}){doi}\nQuelle ({p['source']}, {tag}{herkunft}):{url}\n\n{excerpt}")

    header = (
        "## VERIFIZIERTE OA-LITERATUR (legal via OpenAlex/CORE)\n"
        "Nutze diese peer-reviewten Open-Access-Auszüge für physikalische Zusammenhänge, Verfahren, "
        "Messmethoden und Normkontext — mit Quellenangabe (URL/DOI).\n"
        "**KEINE GRENZWERTE AUS AUFSÄTZEN.** Ein Zahlenwert aus einer wissenschaftlichen Arbeit ist ein "
        "Messergebnis oder eine Empfehlung, NIE eine geltende Anforderung. Verbindliche Grenzwerte "
        "(Druckdifferenzen, Kräfte, U-Werte, Fristen) stammen ausschließlich aus dem einschlägigen "
        "Regelwerk selbst — TRVB, OIB-Richtlinie, ÖNORM, EN/ISO. Steht ein Grenzwert nur in einem "
        "Aufsatz, gib ihn als Literaturangabe mit Quelle aus und kennzeichne ausdrücklich, dass die "
        "verbindliche Fundstelle im Regelwerk noch zu prüfen ist.\n"
        "Die Herkunft (Land der Institutionen, Jahr, Sprache) steht bei jeder Quelle — sie entscheidet "
        "mit, ob eine Arbeit auf den österreichischen Rechtsraum übertragbar ist.\n"
        "Ergänze offene Web-Suche nur für Herstellerdaten/Tarife/Förderungen, die hier fehlen.\n"
    )
    return header + "\n\n".join(blocks)


async def _plan_queries(query: str, attribution: Optional[Dict[str, Any]], model: Optional[str]) -> List[str]:
    """Destilliert die (evtl. sehr lange) Research-Query in Suchbegriffe. Fail-soft → [query[:180]]."""
    fallback = [query.strip()[:180]] if query.strip() else []
    try:
        from src.jobs.executors import _self_post_json
        body = {
            "model": model or "claude-sonnet-4-5-20250929",
            "max_tokens": 1000,
            "messages": [
                {"role": "system", "content": _PLANNER_SYS},
                {"role": "user", "content": query[:9000]},
            ],
        }
        data = await _self_post_json("/v1/chat/completions", body, attribution, 60.0)
        content = data["choices"][0]["message"]["content"]
        m = re.search(r"\[.*\]", content, re.DOTALL)
        queries = [q.strip() for q in json.loads(m.group(0)) if isinstance(q, str) and q.strip()]
        return queries[:14] or fallback
    except Exception as e:
        logger.warning(f"scholarly planner fiel auf raw-query zurück: {type(e).__name__}: {e}")
        return fallback


async def build_oa_context(
    query: str,
    *,
    attribution: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    per_query: int = 12,
    overall_timeout: float = 90.0,
) -> str:
    """Liefert den OA-Literatur-Kontext-Block (Markdown) oder '' (fail-soft/timeout)."""
    try:
        async def _run() -> str:
            queries = await _plan_queries(query, attribution, model)
            if not queries:
                return ""
            return await asyncio.to_thread(_retrieve_and_format, queries, per_query)

        return await asyncio.wait_for(_run(), timeout=overall_timeout)
    except Exception as e:  # inkl. TimeoutError — Research darf NIE an dieser Schicht scheitern
        logger.warning(f"OA-Schicht übersprungen (fail-soft): {type(e).__name__}: {e}")
        return ""
