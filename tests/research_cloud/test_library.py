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
    fetch_library_document,
    fetch_library_index,
    library_enabled,
    load_library_config,
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
