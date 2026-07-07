#!/usr/bin/env python3
"""
Unit Tests for File Discovery Feature

Tests both header-based opt-in and backwards-compatible /sc:research behavior.
"""

import pytest
import httpx
from pathlib import Path


@pytest.mark.asyncio
async def test_file_discovery_header_enabled():
    """Test: File discovery activates with X-Claude-File-Discovery header."""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8000/v1/chat/completions",
            json={
                "model": "claude-sonnet-4-5-20250929",
                "messages": [
                    {"role": "user", "content": "Erstelle eine test.txt Datei mit 'Hello World'"}
                ],
                "stream": False,
                "enable_tools": True
            },
            headers={
                "X-Claude-File-Discovery": "enabled",
                "X-Claude-Max-Turns": "5"
            },
            timeout=120.0
        )

        assert response.status_code == 200
        data = response.json()

        # Should have metadata if files were created
        # (may be absent if Claude didn't actually create files)
        if "x_claude_metadata" in data:
            assert "files_created" in data["x_claude_metadata"]
            assert "discovery_status" in data["x_claude_metadata"]


@pytest.mark.asyncio
async def test_file_discovery_header_disabled():
    """Test: No file discovery without header."""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8000/v1/chat/completions",
            json={
                "model": "claude-sonnet-4-5-20250929",
                "messages": [
                    {"role": "user", "content": "Erstelle eine test.txt Datei"}
                ],
                "stream": False,
                "enable_tools": True
            },
            headers={
                "X-Claude-Max-Turns": "5"
                # NO X-Claude-File-Discovery header
            },
            timeout=120.0
        )

        assert response.status_code == 200
        data = response.json()

        # Should NOT have metadata
        assert "x_claude_metadata" not in data


@pytest.mark.asyncio
async def test_chat_completions_never_sniffs_research_from_prompt():
    """Endpoint separation (Rafael, 2026-07-07): /v1/chat/completions imitates
    the plain Anthropic API — a prompt that starts with (or mentions)
    /sc:research is ordinary text, NEVER an implicit research/file-discovery
    activation. Research runs exclusively on /v1/research. The old
    auto-detection turned prompts that merely MENTIONED the tool into
    file-output mode and killed tools-disabled calls (energy phase-4 incident
    2026-07-06)."""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8000/v1/chat/completions",
            json={
                "model": "claude-sonnet-4-5-20250929",
                "messages": [
                    {"role": "user", "content": "/sc:research --depth quick\n\nPython asyncio"}
                ],
                "stream": False,
                "enable_tools": True
            },
            headers={
                "X-Claude-Max-Turns": "20"
                # NO X-Claude-File-Discovery header → no file discovery, period.
            },
            timeout=600.0
        )

        assert response.status_code == 200
        data = response.json()

        # No implicit research mode: no file-discovery metadata block
        assert "files_created" not in (data.get("x_claude_metadata") or {})


@pytest.mark.asyncio
async def test_file_discovery_no_files_created():
    """Test: Metadata absent when no files created (clean response)."""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8000/v1/chat/completions",
            json={
                "model": "claude-sonnet-4-5-20250929",
                "messages": [
                    {"role": "user", "content": "Was ist 2+2?"}  # No file creation
                ],
                "stream": False,
                "enable_tools": True
            },
            headers={
                "X-Claude-File-Discovery": "enabled",
                "X-Claude-Max-Turns": "3"
            },
            timeout=60.0
        )

        assert response.status_code == 200
        data = response.json()

        # Should NOT have metadata (no files created)
        assert "x_claude_metadata" not in data


@pytest.mark.asyncio
async def test_file_discovery_header_values():
    """Test: Various header values for enabling file discovery."""

    valid_values = ["enabled", "true", "1"]

    for value in valid_values:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "http://localhost:8000/v1/chat/completions",
                json={
                    "model": "claude-sonnet-4-5-20250929",
                    "messages": [
                        {"role": "user", "content": "Test"}
                    ],
                    "stream": False,
                    "enable_tools": True
                },
                headers={
                    "X-Claude-File-Discovery": value
                },
                timeout=30.0
            )

            assert response.status_code == 200
            # Just verify request succeeds (actual file discovery depends on content)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
