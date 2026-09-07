"""Tests fuer src.research_library_pool — die Bibliothek auf dem Abo-Pool-Weg.

boto3 wird nie erreicht: gemockt wird _get_s3_client, eine Schicht ueber dem
Netz (gleiche Naht wie tests/research_cloud/test_library.py).
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.research_cloud.library import LibraryConfig, LibraryUnavailableError
from src.research_cloud.prompt import (
    build_library_catalogue,
    build_pool_library_catalogue,
)
from src.research_library_pool import (
    LIBRARY_WORKDIR_NAME,
    count_library_reads,
    link_sources,
    mirror_root,
    sync_library_mirror,
)

_CONFIGURED = LibraryConfig(
    enabled=True,
    endpoint_url="https://fsn1.your-objectstorage.com",
    bucket="research-library",
    access_key_id="key",
    secret_access_key="secret",
    prefix="research-library/",
)

_INDEX = {
    "version": 1,
    "documents": [
        {"id": "oib-rl-5", "title": "OIB-Richtlinie 5", "jurisdiction": "AT"},
        {"id": "tirol-bo", "title": "Tiroler Bauordnung", "jurisdiction": "AT-T"},
        {"id": "ext-portal", "title": "Normenportal", "jurisdiction": "AT",
         "note": "KATALOG-EINTRAG OHNE VOLLTEXT"},
    ],
}


def _fake_s3(objects):
    """objects: {doc_id: text}. Liefert einen Client-Mock mit list_objects_v2
    und get_object, ETag = Laenge-basiert, damit Aenderungen sichtbar werden."""
    client = MagicMock()

    def _list(**kwargs):
        prefix = kwargs["Prefix"]
        return {
            "Contents": [
                {"Key": f"{prefix}{doc_id}.md", "ETag": f'"{len(text)}-{doc_id}"',
                 "Size": len(text.encode("utf-8"))}
                for doc_id, text in objects.items()
            ],
            "IsTruncated": False,
        }

    def _get(**kwargs):
        key = kwargs["Key"]
        doc_id = key.rsplit("/", 1)[-1][: -len(".md")]
        body = MagicMock()
        body.read.return_value = objects[doc_id].encode("utf-8")
        return {"Body": body}

    client.list_objects_v2.side_effect = _list
    client.get_object.side_effect = _get
    return client


@pytest.fixture
def mirror_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_LIBRARY_MIRROR_DIR", str(tmp_path / "mirror"))
    return tmp_path / "mirror"


@pytest.mark.asyncio
async def test_sync_laedt_volltexte_und_ueberspringt_katalogeintraege(mirror_dir, monkeypatch):
    client = _fake_s3({"oib-rl-5": "OIB Text", "tirol-bo": "Tirol Text"})
    monkeypatch.setattr("src.research_cloud.library._get_s3_client", lambda cfg: client)

    mirror = await sync_library_mirror(_CONFIGURED, _INDEX)

    assert sorted(mirror.available_ids) == ["oib-rl-5", "tirol-bo"]
    assert mirror.missing_ids == []          # ext- ist kein fehlender Volltext
    assert mirror.downloaded == 2 and mirror.reused == 0
    assert (mirror.docs_dir / "oib-rl-5.md").read_text(encoding="utf-8") == "OIB Text"


@pytest.mark.asyncio
async def test_zweiter_lauf_laedt_nichts_nach(mirror_dir, monkeypatch):
    client = _fake_s3({"oib-rl-5": "OIB Text", "tirol-bo": "Tirol Text"})
    monkeypatch.setattr("src.research_cloud.library._get_s3_client", lambda cfg: client)

    await sync_library_mirror(_CONFIGURED, _INDEX)
    second = await sync_library_mirror(_CONFIGURED, _INDEX)

    # Der Sinn des Spiegels: im Normalfall kostet ein Lauf keinen Download.
    assert second.downloaded == 0
    assert second.reused == 2
    assert second.bytes_downloaded == 0


@pytest.mark.asyncio
async def test_geaenderte_fassung_wird_nachgezogen(mirror_dir, monkeypatch):
    objects = {"oib-rl-5": "Fassung 2019", "tirol-bo": "Tirol Text"}
    client = _fake_s3(objects)
    monkeypatch.setattr("src.research_cloud.library._get_s3_client", lambda cfg: client)
    await sync_library_mirror(_CONFIGURED, _INDEX)

    objects["oib-rl-5"] = "Fassung 2023 (neu)"
    mirror = await sync_library_mirror(_CONFIGURED, _INDEX)

    assert mirror.downloaded == 1
    assert (mirror.docs_dir / "oib-rl-5.md").read_text(encoding="utf-8") == "Fassung 2023 (neu)"


@pytest.mark.asyncio
async def test_geloeschte_datei_wird_neu_geholt_trotz_manifest(mirror_dir, monkeypatch):
    """Der Aufraeum-Cron darf den Spiegel jederzeit leeren — das Manifest ist
    eine Abkuerzung, kein Bestandsnachweis."""
    client = _fake_s3({"oib-rl-5": "OIB Text", "tirol-bo": "Tirol Text"})
    monkeypatch.setattr("src.research_cloud.library._get_s3_client", lambda cfg: client)
    mirror = await sync_library_mirror(_CONFIGURED, _INDEX)
    (mirror.docs_dir / "oib-rl-5.md").unlink()

    again = await sync_library_mirror(_CONFIGURED, _INDEX)

    assert again.downloaded == 1
    assert (again.docs_dir / "oib-rl-5.md").exists()


@pytest.mark.asyncio
async def test_zurueckgezogenes_dokument_verschwindet_vom_spiegel(mirror_dir, monkeypatch):
    client = _fake_s3({"oib-rl-5": "OIB Text", "tirol-bo": "Tirol Text"})
    monkeypatch.setattr("src.research_cloud.library._get_s3_client", lambda cfg: client)
    mirror = await sync_library_mirror(_CONFIGURED, _INDEX)
    assert (mirror.docs_dir / "tirol-bo.md").exists()

    shrunk = {"version": 1, "documents": [_INDEX["documents"][0]]}
    again = await sync_library_mirror(_CONFIGURED, shrunk)

    # Eine zurueckgezogene Fassung darf nicht als Datei liegenbleiben und
    # weiter zitiert werden.
    assert not (again.docs_dir / "tirol-bo.md").exists()
    assert again.available_ids == ["oib-rl-5"]


@pytest.mark.asyncio
async def test_fehlendes_objekt_wird_gemeldet_nicht_verschwiegen(mirror_dir, monkeypatch):
    client = _fake_s3({"oib-rl-5": "OIB Text"})  # tirol-bo fehlt im Bucket
    monkeypatch.setattr("src.research_cloud.library._get_s3_client", lambda cfg: client)

    mirror = await sync_library_mirror(_CONFIGURED, _INDEX)

    assert mirror.available_ids == ["oib-rl-5"]
    assert mirror.missing_ids == ["tirol-bo"]


@pytest.mark.asyncio
async def test_leerer_bucket_ist_laut(mirror_dir, monkeypatch):
    client = _fake_s3({})
    monkeypatch.setattr("src.research_cloud.library._get_s3_client", lambda cfg: client)

    with pytest.raises(LibraryUnavailableError):
        await sync_library_mirror(_CONFIGURED, _INDEX)


@pytest.mark.asyncio
async def test_s3_fehler_ist_laut(mirror_dir, monkeypatch):
    def _boom(cfg):
        raise RuntimeError("InvalidAccessKeyId")

    monkeypatch.setattr("src.research_cloud.library._get_s3_client", _boom)

    # Kein stiller Weiterlauf ohne Bibliothek: das war der Fehlermodus vom
    # 04.09. (Lauf meldet Erfolg, hat aber aus dem offenen Netz geantwortet).
    with pytest.raises(LibraryUnavailableError):
        await sync_library_mirror(_CONFIGURED, _INDEX)


def test_mirror_root_trennt_bibliotheken(monkeypatch):
    monkeypatch.delenv("RESEARCH_LIBRARY_MIRROR_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CWD", "/app/instances")
    other = _CONFIGURED.model_copy(update={"prefix": "andere-bibliothek/"})
    assert mirror_root(_CONFIGURED) != mirror_root(other)
    assert str(mirror_root(_CONFIGURED)).startswith("/app/instances/")


@pytest.mark.asyncio
async def test_link_sources_zeigt_auf_die_spiegel_dateien(mirror_dir, monkeypatch):
    client = _fake_s3({"oib-rl-5": "OIB Text", "tirol-bo": "Tirol Text"})
    monkeypatch.setattr("src.research_cloud.library._get_s3_client", lambda cfg: client)
    mirror = await sync_library_mirror(_CONFIGURED, _INDEX)

    links = link_sources(mirror)

    assert set(links) == {"oib-rl-5.md", "tirol-bo.md"}
    assert all(Path(src).exists() for src in links.values())


# --- Katalog ---------------------------------------------------------------


def test_cloud_katalog_bleibt_unveraendert():
    """Der Cloud-Weg bleibt unangetastet (Auftrag 07.09.) — der gemeinsame
    Zeilen-Renderer darf seine Ausgabe nicht verschieben."""
    out = build_library_catalogue(_INDEX)
    assert "library_get" in out
    assert "- `oib-rl-5` — OIB-Richtlinie 5 [AT]" in out
    assert "- `ext-portal` — Normenportal [AT] — KEIN VOLLTEXT" in out
    assert "### Verzeichnis (3 Einträge)" in out
    # Keine Dateipfade auf dem Cloud-Weg — dort gibt es kein Arbeitsverzeichnis.
    assert f"{LIBRARY_WORKDIR_NAME}/" not in out


def test_pool_katalog_nennt_dateien_statt_werkzeuge():
    out = build_pool_library_catalogue(_INDEX, dir_name=LIBRARY_WORKDIR_NAME)
    assert f"{LIBRARY_WORKDIR_NAME}/<id>.md" in out
    assert "library_get" not in out and "library_index" not in out
    # Verzeichnis vollstaendig: das Modell waehlt, nicht der Code (31.07.)
    assert "### Verzeichnis (3 Einträge)" in out
    for doc_id in ("oib-rl-5", "tirol-bo", "ext-portal"):
        assert f"`{doc_id}`" in out


def test_pool_katalog_markiert_nicht_gespiegelte_eintraege():
    out = build_pool_library_catalogue(
        _INDEX, dir_name=LIBRARY_WORKDIR_NAME, unavailable_ids={"tirol-bo"}
    )
    assert "- `tirol-bo` — Tiroler Bauordnung [AT-T] — KEIN VOLLTEXT" in out
    assert "- `oib-rl-5` — OIB-Richtlinie 5 [AT]" in out
    assert "- `oib-rl-5` — OIB-Richtlinie 5 [AT] — KEIN VOLLTEXT" not in out


def test_leerer_index_ergibt_keinen_block():
    assert build_pool_library_catalogue(None, dir_name=LIBRARY_WORKDIR_NAME) == ""
    assert build_pool_library_catalogue({"documents": []}, dir_name=LIBRARY_WORKDIR_NAME) == ""


# --- Zaehlung --------------------------------------------------------------


class _Block:
    def __init__(self, name, input_):
        self.name = name
        self.input = input_


def test_zaehlt_reads_und_greps_im_bibliotheksordner():
    chunks = [
        {"content": [_Block("Read", {"file_path": "/app/instances/x/bibliothek/oib-rl-5.md"})]},
        {"content": [_Block("Grep", {"path": "bibliothek/", "pattern": "Brandabschnitt"})]},
        {"content": [_Block("Read", {"file_path": "/app/instances/x/bibliothek/oib-rl-5.md"})]},
        {"content": [_Block("Read", {"file_path": "/app/instances/x/unterlagen/fremd.md"})]},
        {"content": [_Block("WebSearch", {"query": "OIB 5"})]},
    ]

    calls, docs = count_library_reads(chunks)

    assert calls == 3          # zwei Reads + ein Grep
    assert docs == ["oib-rl-5"]  # dedupliziert, Fremdordner zaehlt nicht


def test_zaehlt_null_wenn_das_modell_die_bibliothek_nicht_anfasst():
    chunks = [{"content": [_Block("WebSearch", {"query": "OIB 5"})]}]
    assert count_library_reads(chunks) == (0, [])


def test_vertraegt_dict_bloecke_und_fremde_chunks():
    chunks = [
        "roher text",
        {"content": "kein block-array"},
        {"content": [{"name": "Read", "input": {"file_path": "bibliothek/tirol-bo.md"}}]},
    ]
    calls, docs = count_library_reads(chunks)
    assert calls == 1 and docs == ["tirol-bo"]
