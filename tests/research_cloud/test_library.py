"""Tests for src.research_cloud.library — the S3 fetch layer behind the
executor's library_index/library_get tools. boto3 itself is never hit: the
synchronous S3 call is mocked at _get_s3_client, one layer above the network.
"""
import json
from unittest.mock import MagicMock

import pytest

from src.research_cloud.library import (
    LibraryConfig,
    LibraryFetchError,
    LibraryUnavailableError,
    entry_has_fulltext,
    fetch_library_document,
    fetch_library_index,
    library_enabled,
    load_library_config,
    load_library_for_run,
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
        {
            "id": "doc-a",
            "title": "Doc A",
            "publisher": "Publisher A",
            "source_url": "https://example.org/doc-a.pdf",
        }
    ],
}


def _fake_s3_client(objects: dict):
    """objects: key -> str body. Missing key raises like a real 404 would."""

    def get_object(Bucket, Key):
        if Key not in objects:
            raise Exception(f"NoSuchKey: {Key}")
        body = MagicMock()
        body.read.return_value = objects[Key].encode("utf-8")
        return {"Body": body}

    client = MagicMock()
    client.get_object.side_effect = get_object
    return client


def test_load_library_config_defaults_disabled(monkeypatch):
    for var in [
        "RESEARCH_LIBRARY_ENABLED",
        "RESEARCH_LIBRARY_S3_ENDPOINT_URL",
        "RESEARCH_LIBRARY_S3_BUCKET",
        "RESEARCH_LIBRARY_S3_ACCESS_KEY_ID",
        "RESEARCH_LIBRARY_S3_SECRET_ACCESS_KEY",
    ]:
        monkeypatch.delenv(var, raising=False)
    config = load_library_config()
    assert config.enabled is False
    assert config.configured is False
    assert library_enabled(config) is False


def test_load_library_config_reads_env(monkeypatch):
    monkeypatch.setenv("RESEARCH_LIBRARY_ENABLED", "true")
    monkeypatch.setenv("RESEARCH_LIBRARY_S3_ENDPOINT_URL", "https://example.com")
    monkeypatch.setenv("RESEARCH_LIBRARY_S3_BUCKET", "my-bucket")
    monkeypatch.setenv("RESEARCH_LIBRARY_S3_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("RESEARCH_LIBRARY_S3_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("RESEARCH_LIBRARY_S3_PREFIX", "custom-prefix")
    config = load_library_config()
    assert config.enabled is True
    assert config.configured is True
    assert config.prefix == "custom-prefix/"  # trailing slash normalized
    assert library_enabled(config) is True


def test_library_enabled_false_when_flag_on_but_not_configured():
    """Flag on but creds missing must NOT advertise the tool — every call
    would fail, and the model can't know that in advance."""
    config = LibraryConfig(enabled=True)
    assert config.configured is False
    assert library_enabled(config) is False


@pytest.mark.asyncio
async def test_fetch_library_index_returns_parsed_json(monkeypatch):
    monkeypatch.setattr(
        "src.research_cloud.library._get_s3_client",
        lambda config: _fake_s3_client({"research-library/index.json": json.dumps(_INDEX)}),
    )
    index = await fetch_library_index(_CONFIGURED)
    assert index == _INDEX


@pytest.mark.asyncio
async def test_fetch_library_index_raises_on_invalid_json(monkeypatch):
    monkeypatch.setattr(
        "src.research_cloud.library._get_s3_client",
        lambda config: _fake_s3_client({"research-library/index.json": "not json"}),
    )
    with pytest.raises(LibraryFetchError, match="not valid JSON"):
        await fetch_library_index(_CONFIGURED)


@pytest.mark.asyncio
async def test_fetch_library_index_raises_when_not_configured():
    with pytest.raises(LibraryFetchError, match="not configured"):
        await fetch_library_index(LibraryConfig())


@pytest.mark.asyncio
async def test_fetch_library_document_returns_entry_and_text(monkeypatch):
    monkeypatch.setattr(
        "src.research_cloud.library._get_s3_client",
        lambda config: _fake_s3_client(
            {
                "research-library/index.json": json.dumps(_INDEX),
                "research-library/docs/doc-a.md": "# Doc A\n\nFull text.",
            }
        ),
    )
    doc = await fetch_library_document("doc-a", _CONFIGURED)
    assert doc["entry"]["title"] == "Doc A"
    assert doc["text"] == "# Doc A\n\nFull text."


@pytest.mark.asyncio
async def test_fetch_library_document_unknown_id_raises(monkeypatch):
    monkeypatch.setattr(
        "src.research_cloud.library._get_s3_client",
        lambda config: _fake_s3_client({"research-library/index.json": json.dumps(_INDEX)}),
    )
    with pytest.raises(LibraryFetchError, match="unknown library document id"):
        await fetch_library_document("does-not-exist", _CONFIGURED)


@pytest.mark.asyncio
async def test_fetch_library_document_s3_error_raises_library_fetch_error(monkeypatch):
    monkeypatch.setattr(
        "src.research_cloud.library._get_s3_client",
        lambda config: _fake_s3_client({"research-library/index.json": json.dumps(_INDEX)}),
        # docs/doc-a.md deliberately missing -> get_object raises
    )
    with pytest.raises(LibraryFetchError, match="S3 GetObject failed"):
        await fetch_library_document("doc-a", _CONFIGURED)


# ---------------------------------------------------------------------------
# load_library_for_run — the once-per-run resolution that decides whether the
# run may start at all (2026-09-05). Three outcomes, and the middle one is the
# reason it exists: a library switched ON that does not work used to be
# indistinguishable from one switched off.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_library_for_run_returns_none_when_flag_is_off():
    assert await load_library_for_run(LibraryConfig()) is None


@pytest.mark.asyncio
async def test_load_library_for_run_names_the_empty_variables():
    """The prod failure mode from the ADR-0009 worker move: variable names
    moved with the containers, values did not. Presence-only gating swallowed
    it for two weeks — now it names which of the six are empty."""
    half = LibraryConfig(enabled=True, endpoint_url="https://fsn1.example.com", bucket="b")
    with pytest.raises(LibraryUnavailableError) as excinfo:
        await load_library_for_run(half)
    assert "RESEARCH_LIBRARY_S3_ACCESS_KEY_ID" in str(excinfo.value)
    assert "RESEARCH_LIBRARY_S3_SECRET_ACCESS_KEY" in str(excinfo.value)


@pytest.mark.asyncio
async def test_load_library_for_run_raises_when_index_does_not_load(monkeypatch):
    """Stale credentials — the harder state: presence is fine, every fetch
    fails. Fail-soft would report a successful research that quietly answered
    from the open web."""
    monkeypatch.setattr(
        "src.research_cloud.library._get_s3_client",
        lambda config: _fake_s3_client({}),  # index.json missing -> SignatureDoesNotMatch-alike
    )
    with pytest.raises(LibraryUnavailableError, match="index does not load"):
        await load_library_for_run(_CONFIGURED)


@pytest.mark.asyncio
async def test_load_library_for_run_returns_index_when_usable(monkeypatch):
    monkeypatch.setattr(
        "src.research_cloud.library._get_s3_client",
        lambda config: _fake_s3_client({"research-library/index.json": json.dumps(_INDEX)}),
    )
    index = await load_library_for_run(_CONFIGURED)
    assert index["documents"] == _INDEX["documents"]


@pytest.mark.asyncio
async def test_catalogue_only_entry_is_refused_with_its_source(monkeypatch):
    """The 20 ``ext-`` entries have no docs/<id>.md. Before, library_get built
    the key anyway and returned an opaque S3 error that reads like a broken
    library; now it says what the entry is and where to go instead."""
    index = {
        "documents": [
            {
                "id": "ext-portal",
                "title": "Externes Portal",
                "source_url": "https://example.org/portal",
            }
        ]
    }
    monkeypatch.setattr(
        "src.research_cloud.library._get_s3_client",
        lambda config: _fake_s3_client({"research-library/index.json": json.dumps(index)}),
    )
    with pytest.raises(LibraryFetchError) as excinfo:
        await fetch_library_document("ext-portal", _CONFIGURED)
    assert "without a stored full text" in str(excinfo.value)
    assert "https://example.org/portal" in str(excinfo.value)


def test_entry_has_fulltext_keys_off_the_ext_prefix():
    assert entry_has_fulltext({"id": "at-tirol-tbv2026-anlage1"}) is True
    assert entry_has_fulltext({"id": "ext-klimaaktiv-publikationen"}) is False
