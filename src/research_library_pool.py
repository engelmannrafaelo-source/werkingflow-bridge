"""Die kuratierte Normenbibliothek auf dem Abo-Pool-Weg (Claude-Code-CLI).

Der Cloud-Weg (``src/research_cloud/executor.py``) reicht dem Modell zwei
Client-Werkzeuge (``library_index``/``library_get``) und beantwortet sie selbst
aus S3. Auf dem Pool-Weg gibt es keinen solchen Werkzeug-Loop: dort laeuft die
Claude-Code-CLI in einem Arbeitsverzeichnis und liest mit Read/Grep/Glob vom
Dateisystem. Dieses Modul uebersetzt die Bibliothek in genau diese Welt —
Volltexte als Dateien, Verzeichnis als Prompt-Block (``prompt.py``).

Warum ein Spiegel auf der Platte und keine 74 S3-Downloads je Lauf
(Abwaegung, Entscheidung Rafael 07.09.2026, Karte k20, Weg a):

* Ein Vorfiltern nach Thema/Stichwort ist ausgeschlossen — die Leitplanke vom
  31.07. verbietet Keyword-Routing; das Modell soll den Bestand sehen und
  selbst waehlen. Also braucht der Lauf grundsaetzlich ALLE Volltexte.
* Sie je Lauf frisch aus S3 zu ziehen, kostet ~5 MB und 74 GETs pro Recherche,
  auch bei fachfremden Fragen, und legt in jedem Sitzungsordner eine eigene
  Kopie ab (Sitzungen liegen 7 Tage) — bei hundert Recherchen am Tag ein
  halbes Gigabyte taeglich fuer immer dieselben Dokumente.
* Der Spiegel haelt EINE Kopie je Worker. Ein Lauf prueft nur noch den Bestand
  (ein LIST + ein GET auf index.json) und verlinkt die Dateien per Hardlink in
  sein Arbeitsverzeichnis: kein Datenverkehr, kein zweites Byte auf der Platte,
  und fuer jedes Werkzeug (Read, Grep, Glob) sind es echte Dateien — anders als
  bei einem Symlink, dem ripgrep und Glob nicht zuverlaessig folgen.

Fail-loud: Der Pool-Weg meldete stille Fehlschlaege bisher als Erfolg (Befund
04.09.). Eine eingeschaltete, aber nicht benutzbare Bibliothek bricht den Lauf
deshalb VOR dem ersten Token ab — dieselbe Regel wie auf dem Cloud-Weg
(``library.load_library_for_run``), und aus demselben Grund: lieber ein
lauter Fehler als eine Antwort, die heimlich aus dem offenen Netz kommt.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel

from src.research_cloud.library import (
    LibraryConfig,
    LibraryUnavailableError,
    entry_has_fulltext,
)

logger = logging.getLogger(__name__)

# Name des Unterordners im Sitzungsverzeichnis, unter dem die Volltexte liegen.
# Steht so auch im Prompt-Block (prompt.py) und ist der Marker, an dem
# count_library_reads die Bibliotheksaufrufe des Modells wiedererkennt.
LIBRARY_WORKDIR_NAME = "bibliothek"

_MANIFEST_NAME = "mirror-manifest.json"

# Ein Prozess laedt den Spiegel nicht mehrfach parallel. Zwischen mehreren
# Worker-Prozessen genuegt das nicht — dagegen schuetzt das atomare
# Schreiben (Temp-Datei + os.replace), nicht dieses Lock.
_sync_lock = asyncio.Lock()


class LibraryMirror(BaseModel):
    """Ergebnis eines Spiegel-Abgleichs: was liegt jetzt lokal, und was hat
    der Abgleich gekostet."""

    docs_dir: Path
    available_ids: List[str]
    # Eintraege, die laut Index einen Volltext haben, zu denen im Bucket aber
    # kein Objekt liegt. Sie bleiben im Katalog (das Verzeichnis wird nie
    # beschnitten), werden dort aber als "KEIN VOLLTEXT" gefuehrt — sonst
    # schickt der Prompt das Modell auf eine Datei, die es nicht gibt.
    missing_ids: List[str] = []
    downloaded: int = 0
    reused: int = 0
    bytes_downloaded: int = 0

    model_config = {"arbitrary_types_allowed": True}


def mirror_root(config: LibraryConfig) -> Path:
    """Verzeichnis des Worker-lokalen Spiegels.

    Default ist ein Punkt-Ordner NEBEN den Sitzungsverzeichnissen (CLAUDE_CWD),
    damit Hardlinks ins Sitzungsverzeichnis auf demselben Dateisystem liegen —
    ein Spiegel auf einem anderen Mount koennte nicht verlinkt werden und
    fiele auf Kopien zurueck.
    """
    configured = os.environ.get("RESEARCH_LIBRARY_MIRROR_DIR", "").strip()
    if configured:
        return Path(configured)
    base = os.environ.get("CLAUDE_CWD", "").strip() or "/app/instances"
    # Der Bucket-Prefix gehoert in den Pfad: zwei Bibliotheken (anderer Prefix)
    # duerfen sich nicht denselben Spiegel teilen.
    slug = config.prefix.strip("/").replace("/", "_") or "research-library"
    return Path(base) / ".research-library" / slug


def _list_doc_objects_sync(config: LibraryConfig) -> Dict[str, Dict[str, Any]]:
    """{doc_id: {"key", "etag", "size"}} fuer alle Objekte unter docs/.

    Ein einziger (paginierter) LIST statt 74 HEADs — das ist der Grund, warum
    ein Abgleich im Normalfall nichts kostet.
    """
    from src.research_cloud.library import _get_s3_client

    client = _get_s3_client(config)
    prefix = f"{config.prefix}docs/"
    found: Dict[str, Dict[str, Any]] = {}
    token: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"Bucket": config.bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            name = key[len(prefix):]
            if not name.endswith(".md") or "/" in name:
                continue
            found[name[: -len(".md")]] = {
                "key": key,
                "etag": (obj.get("ETag") or "").strip('"'),
                "size": int(obj.get("Size") or 0),
            }
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:
            break
    return found


def _download_doc_sync(config: LibraryConfig, key: str, target: Path) -> int:
    """Ein Dokument atomar in den Spiegel legen. Rueckgabe: Byte-Zahl.

    Erst in eine Temp-Datei daneben, dann os.replace — ein zweiter Prozess,
    der dieselbe Datei gleichzeitig zieht, sieht so nie einen halben Text.
    """
    from src.research_cloud.library import _get_s3_client

    client = _get_s3_client(config)
    obj = client.get_object(Bucket=config.bucket, Key=key)
    body = obj["Body"].read()
    tmp = target.with_suffix(target.suffix + f".tmp-{os.getpid()}")
    tmp.write_bytes(body)
    os.replace(tmp, target)
    return len(body)


def _manifest_path(root: Path) -> Path:
    return root / _MANIFEST_NAME


def _load_manifest(root: Path) -> Dict[str, Dict[str, Any]]:
    path = _manifest_path(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        # Kein Grund zum Abbruch: das Manifest ist eine Abkuerzung, kein
        # Bestandsnachweis. Ohne Manifest wird alles neu gezogen.
        logger.warning(f"research-library mirror: Manifest unlesbar, wird neu aufgebaut: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def _store_manifest(root: Path, manifest: Dict[str, Dict[str, Any]]) -> None:
    path = _manifest_path(root)
    tmp = path.with_suffix(f".tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"research-library mirror: Manifest nicht schreibbar ({e}) — naechster Lauf gleicht neu ab")


def _sync_sync(config: LibraryConfig, index: Dict[str, Any], root: Path) -> LibraryMirror:
    """Der eigentliche Abgleich (blockierend; Aufrufer nutzt asyncio.to_thread)."""
    docs_dir = root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    remote = _list_doc_objects_sync(config)
    manifest = _load_manifest(root)

    wanted = [
        str(entry.get("id"))
        for entry in (index.get("documents") or [])
        if entry.get("id") and entry_has_fulltext(entry)
    ]

    available: List[str] = []
    missing: List[str] = []
    downloaded = reused = 0
    bytes_downloaded = 0

    for doc_id in wanted:
        info = remote.get(doc_id)
        if info is None:
            missing.append(doc_id)
            continue
        target = docs_dir / f"{doc_id}.md"
        known = manifest.get(doc_id) or {}
        # Die Platte entscheidet, nicht das Manifest: der Aufraeum-Cron darf
        # den Spiegel jederzeit loeschen, ohne dass wir Dateien annehmen,
        # die es nicht mehr gibt.
        fresh = (
            target.exists()
            and known.get("etag") == info["etag"]
            and target.stat().st_size == info["size"]
        )
        if fresh:
            reused += 1
        else:
            bytes_downloaded += _download_doc_sync(config, info["key"], target)
            downloaded += 1
            manifest[doc_id] = {"etag": info["etag"], "size": info["size"]}
        available.append(doc_id)

    # Dokumente, die der Index nicht mehr fuehrt, verschwinden aus dem Spiegel:
    # eine zurueckgezogene Fassung darf nicht als Datei liegenbleiben und
    # weiter zitiert werden.
    wanted_set = set(wanted)
    for stale in sorted(set(manifest) - wanted_set):
        manifest.pop(stale, None)
    for path in docs_dir.glob("*.md"):
        if path.stem not in wanted_set:
            try:
                path.unlink()
            except OSError as e:
                logger.warning(f"research-library mirror: {path.name} nicht loeschbar: {e}")

    _store_manifest(root, manifest)

    return LibraryMirror(
        docs_dir=docs_dir,
        available_ids=available,
        missing_ids=missing,
        downloaded=downloaded,
        reused=reused,
        bytes_downloaded=bytes_downloaded,
    )


async def sync_library_mirror(config: LibraryConfig, index: Dict[str, Any]) -> LibraryMirror:
    """Spiegel gegen den Bucket abgleichen. Laut, wenn er nicht benutzbar ist.

    Aufrufer ist der Pool-Recherche-Pfad, NACH library.load_library_for_run
    (das den Index laedt und die Bibliothek als Ganzes prueft) und VOR dem
    ersten Modell-Token.
    """
    root = mirror_root(config)
    async with _sync_lock:
        try:
            mirror = await asyncio.to_thread(_sync_sync, config, index, root)
        except LibraryUnavailableError:
            raise
        except Exception as e:
            raise LibraryUnavailableError(
                f"Der lokale Bibliotheks-Spiegel unter {root} laesst sich nicht abgleichen: {e}"
            ) from e

    if not mirror.available_ids:
        raise LibraryUnavailableError(
            "Der Bibliotheks-Index fuehrt Volltexte, aber im Bucket liegt unter "
            f"{config.prefix}docs/ kein einziges davon — der Spiegel waere leer"
        )
    if mirror.missing_ids:
        # Einzelne Luecken sind ein Kuratierungsfehler, kein Infrastrukturfehler:
        # der Lauf geht weiter, die betroffenen Eintraege werden im Katalog als
        # "KEIN VOLLTEXT" gefuehrt (build_pool_library_catalogue).
        logger.warning(
            "research-library mirror: %d Eintrag/Eintraege ohne Objekt im Bucket: %s",
            len(mirror.missing_ids),
            ", ".join(mirror.missing_ids[:10]),
        )
    logger.info(
        "📚 research-library mirror bereit: %d Volltexte (%d neu geladen, %d unveraendert, %.1f KB geladen) in %s",
        len(mirror.available_ids),
        mirror.downloaded,
        mirror.reused,
        mirror.bytes_downloaded / 1024,
        mirror.docs_dir,
    )
    return mirror


def link_sources(mirror: LibraryMirror) -> Dict[str, str]:
    """{Dateiname: absoluter Pfad im Spiegel} — die Vorlage fuer
    ``run_completion(seed_links=...)``."""
    return {f"{doc_id}.md": str(mirror.docs_dir / f"{doc_id}.md") for doc_id in mirror.available_ids}


def count_library_reads(chunks: List[Any]) -> Tuple[int, List[str]]:
    """(Anzahl Bibliotheksaufrufe, gelesene Dokument-ids) aus den CLI-Chunks.

    Zaehlweise wie beim Dokument-Agenten (_doc_agent_extract_files_read): jeder
    Read/Grep/Glob-Block, dessen Pfad/Muster in den Bibliotheksordner zeigt, ist
    ein Aufruf — das ist auf diesem Weg das Gegenstueck zu ``library_calls`` des
    Cloud-Executors. Read liefert zusaetzlich die Dokument-id.
    """
    marker = f"{LIBRARY_WORKDIR_NAME}/"
    calls = 0
    doc_ids: List[str] = []
    for message in chunks:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            name = getattr(block, "name", None)
            if name is None and isinstance(block, dict):
                name = block.get("name")
            if name not in ("Read", "Grep", "Glob"):
                continue
            block_input = getattr(block, "input", None)
            if block_input is None and isinstance(block, dict):
                block_input = block.get("input")
            block_input = block_input or {}
            candidates = [
                block_input.get("file_path"),
                block_input.get("path"),
                block_input.get("pattern") if name == "Glob" else None,
            ]
            hit = next(
                (c for c in candidates if isinstance(c, str) and (marker in c or c.rstrip("/").endswith(LIBRARY_WORKDIR_NAME))),
                None,
            )
            if hit is None:
                continue
            calls += 1
            if name == "Read":
                doc_id = Path(hit).stem
                if doc_id and doc_id not in doc_ids:
                    doc_ids.append(doc_id)
    return calls, doc_ids
