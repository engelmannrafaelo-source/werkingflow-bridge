"""Org-disabled subscription access must fail loud, not answer 200.

Befund 28.09.2026 (~18:00Z, prod, worker-sahori): every chat call came back
HTTP 200 with the assistant content "Your organization has disabled Claude
subscription access for Claude Code" (x-bridge-cost-eur 0). The CLI emits that
as a normal assistant turn; the worker passed it through as a completion,
/health stayed healthy, the Lua pool router kept routing ~1/4 of the traffic
there, and nginx never retried because a 200 is not an error.

Pinned here:
  - detection (structured CLI `error` field first, short text second)
  - the capacity lock (reason org_subscription_disabled, re-armed per hit)
  - the 503 with code account_org_disabled instead of a 200
  - /health reports it as a FIELD while `status` stays "healthy"
"""
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

import src.claude_cli as claude_cli
import src.middleware.capacity_lock as capacity_lock_mod
from src.claude_cli import (
    ClaudeCodeCLI,
    ORG_DISABLED_LOCK_REASON,
    OrgSubscriptionDisabledError,
    WorkerUnavailableError,
    detect_org_subscription_disabled,
    handle_org_subscription_disabled,
)

# Verbatim CLI 2.1.283 text (the "·" is \xB7 in the binary).
CLI_ORG_TEXT = (
    "Your organization has disabled Claude subscription access for Claude Code "
    "· Use an Anthropic API key instead, or ask your admin to enable access"
)


# Local stand-ins with the SDK's class NAMES and fields — same pattern as
# test_cli_continuation_not_content.py (run_completion dispatches on names).
@dataclass
class TextBlock:
    text: str


@dataclass
class UserMessage:
    content: Any


@dataclass
class AssistantMessage:
    content: list
    model: str


@dataclass
class ResultMessage:
    subtype: str
    duration_ms: int
    duration_api_ms: int
    is_error: bool
    num_turns: int
    session_id: str
    total_cost_usd: Optional[float] = None
    usage: Optional[dict] = field(default=None)
    result: Optional[str] = None


def _result(is_error: bool, text: str) -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=0, is_error=is_error,
        num_turns=1, session_id="s", total_cost_usd=0.0,
        usage={"input_tokens": 0, "output_tokens": 0}, result=text,
    )


@pytest.fixture(autouse=True)
def fresh_lock(tmp_path, monkeypatch):
    """Isolated capacity lock: own persist file, fresh singleton."""
    monkeypatch.setattr(
        capacity_lock_mod.CapacityLock, "PERSIST_PATH", str(tmp_path / "cap_lock.json")
    )
    monkeypatch.setattr(capacity_lock_mod, "_INSTANCE", None)
    yield capacity_lock_mod.get_capacity_lock()
    monkeypatch.setattr(capacity_lock_mod, "_INSTANCE", None)


# ── detection ────────────────────────────────────────────────────────────────

class TestDetection:
    def test_structured_cli_error_on_dataclass(self):
        msg = AssistantMessage(content=[TextBlock(text="anything")], model="<synthetic>")
        msg._bridge_cli_error = "oauth_org_not_allowed"
        assert detect_org_subscription_disabled(msg) == "cli_error:oauth_org_not_allowed"

    def test_structured_cli_error_on_dict(self):
        assert (
            detect_org_subscription_disabled({"type": "assistant", "error": "oauth_org_not_allowed"})
            == "cli_error:oauth_org_not_allowed"
        )

    def test_other_cli_error_is_not_org_disabled(self):
        msg = AssistantMessage(content=[TextBlock(text="x")], model="<synthetic>")
        msg._bridge_cli_error = "rate_limit"
        assert detect_org_subscription_disabled(msg) is None

    def test_assistant_text_case_insensitive(self):
        msg = AssistantMessage(content=[TextBlock(text=CLI_ORG_TEXT.upper())], model="<synthetic>")
        assert detect_org_subscription_disabled(msg) == "assistant_text"

    def test_dict_chunk_text(self):
        assert detect_org_subscription_disabled(
            {"content": [TextBlock(text=CLI_ORG_TEXT)]}
        ) == "assistant_text"

    def test_result_text_only_when_is_error(self):
        assert detect_org_subscription_disabled(_result(True, CLI_ORG_TEXT)) == "result_text"
        assert detect_org_subscription_disabled(_result(False, CLI_ORG_TEXT)) is None

    def test_long_answer_mentioning_it_is_not_a_hit(self):
        """A model WRITING about the error (report, docs) must not park the worker."""
        text = ("Ein Bericht ueber Bridge-Fehler. " * 60) + CLI_ORG_TEXT
        assert len(text) > claude_cli.ORG_DISABLED_TEXT_MAX_LEN
        msg = AssistantMessage(content=[TextBlock(text=text)], model="m")
        assert detect_org_subscription_disabled(msg) is None

    def test_user_turn_is_never_checked(self):
        """The caller's own prompt may quote the sentence."""
        assert detect_org_subscription_disabled(UserMessage(content=[TextBlock(text=CLI_ORG_TEXT)])) is None
        assert detect_org_subscription_disabled({"type": "user", "content": CLI_ORG_TEXT}) is None

    def test_normal_answer(self):
        msg = AssistantMessage(content=[TextBlock(text="Hallo Welt")], model="m")
        assert detect_org_subscription_disabled(msg) is None
        assert detect_org_subscription_disabled(None) is None


def test_sdk_parser_patch_carries_cli_error_field():
    """claude-code-sdk drops unknown fields; the patched parser must keep `error`."""
    pytest.importorskip("claude_code_sdk._internal.message_parser")
    parsed = claude_cli._resilient_parse_message({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": CLI_ORG_TEXT}], "model": "<synthetic>"},
        "parent_tool_use_id": None,
        "session_id": "s",
        "error": "oauth_org_not_allowed",
    })
    assert type(parsed).__name__ == "AssistantMessage"
    assert detect_org_subscription_disabled(parsed) == "cli_error:oauth_org_not_allowed"


# ── lock + raise ─────────────────────────────────────────────────────────────

class TestHandle:
    def test_locks_worker_and_raises(self, fresh_lock, caplog):
        before = time.time()
        with pytest.raises(OrgSubscriptionDisabledError) as ei:
            handle_org_subscription_disabled("worker-sahori", "assistant_text")
        # Subclass on purpose: every `except WorkerUnavailableError: raise` propagates it.
        assert isinstance(ei.value, WorkerUnavailableError)
        assert ei.value.lock_seconds == claude_cli.ORG_DISABLED_LOCK_SECONDS
        info = fresh_lock.get_lock_info("worker-sahori")
        assert info["reason"] == ORG_DISABLED_LOCK_REASON
        assert info["locked_until_ts"] >= before + claude_cli.ORG_DISABLED_LOCK_SECONDS - 1
        assert fresh_lock.is_locked("worker-sahori")
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert any("ORG SUBSCRIPTION DISABLED" in r.getMessage() and "worker-sahori" in r.getMessage()
                   for r in errors)

    def test_rearmed_on_every_hit(self, fresh_lock, monkeypatch):
        t = [1_000_000.0]
        monkeypatch.setattr(claude_cli.time, "time", lambda: t[0])
        monkeypatch.setattr(capacity_lock_mod.time, "time", lambda: t[0])
        with pytest.raises(OrgSubscriptionDisabledError):
            handle_org_subscription_disabled("w1", "assistant_text")
        first = fresh_lock.get_lock_info("w1")["locked_until_ts"]
        t[0] += 600
        with pytest.raises(OrgSubscriptionDisabledError):
            handle_org_subscription_disabled("w1", "assistant_text")
        assert fresh_lock.get_lock_info("w1")["locked_until_ts"] == first + 600


class TestLockSecondsEnv:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("BRIDGE_ORG_DISABLED_LOCK_S", raising=False)
        assert claude_cli._parse_org_disabled_lock_seconds() == 3600

    def test_override(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_ORG_DISABLED_LOCK_S", "900")
        assert claude_cli._parse_org_disabled_lock_seconds() == 900

    @pytest.mark.parametrize("bad", ["abc", "0", "-5", "1.5"])
    def test_invalid_fails_loud(self, monkeypatch, bad):
        monkeypatch.setenv("BRIDGE_ORG_DISABLED_LOCK_S", bad)
        with pytest.raises(ValueError):
            claude_cli._parse_org_disabled_lock_seconds()


# ── run_completion: the block sentence is never yielded ──────────────────────

def _make_cli(tmp_path: Path) -> ClaudeCodeCLI:
    cli = object.__new__(ClaudeCodeCLI)
    cli.timeout = 30
    cli.cwd = tmp_path
    cli.claude_env_vars = {}
    cli.cache_dir = tmp_path
    cli.max_cache_size_mb = 10
    cli.file_discovery = MagicMock()
    return cli


@pytest.mark.asyncio
async def test_run_completion_raises_and_yields_nothing(monkeypatch, tmp_path, fresh_lock):
    monkeypatch.setenv("INSTANCE_NAME", "worker-sahori")

    async def fake_query(prompt, options):
        yield AssistantMessage(content=[TextBlock(text=CLI_ORG_TEXT)], model="<synthetic>")
        yield _result(True, CLI_ORG_TEXT)

    monkeypatch.setattr(claude_cli, "query", fake_query)
    got = []
    with pytest.raises(OrgSubscriptionDisabledError):
        async for chunk in _make_cli(tmp_path).run_completion(prompt="Hallo", model="claude-sonnet-5"):
            got.append(chunk)
    assert not any(CLI_ORG_TEXT.lower() in str(c).lower() for c in got)
    assert fresh_lock.get_lock_info("worker-sahori")["reason"] == ORG_DISABLED_LOCK_REASON


# ── HTTP surface: 503 + /health field ────────────────────────────────────────

@pytest.fixture()
def main_mod():
    return pytest.importorskip("src.main")


@pytest.mark.asyncio
async def test_handler_answers_503_with_code(main_mod):
    from starlette.requests import Request

    # Starlette resolves handlers by MRO — the subclass handler must win over
    # the parent's 429 worker_unavailable_handler.
    assert main_mod.app.exception_handlers[OrgSubscriptionDisabledError] is \
        main_mod.org_subscription_disabled_handler
    resp = await main_mod.org_subscription_disabled_handler(
        Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}),
        OrgSubscriptionDisabledError("worker-sahori", "assistant_text", 3600),
    )
    import json
    body = json.loads(resp.body)
    assert resp.status_code == 503
    assert body["error"]["code"] == "account_org_disabled"
    assert body["error"]["reason"] == "account_org_disabled"
    assert body["error"]["retryable"] is True
    assert resp.headers["Retry-After"] == "3600"


def test_precheck_blocks_only_org_lock(main_mod, fresh_lock):
    assert main_mod._org_disabled_precheck("w1") is None
    fresh_lock.lock_until("w1", time.time() + 300, "weekly_window")
    assert main_mod._org_disabled_precheck("w1") is None  # other lock reasons: untouched
    fresh_lock.lock_until("w2", time.time() + 300, ORG_DISABLED_LOCK_REASON)
    resp = main_mod._org_disabled_precheck("w2")
    assert resp is not None and resp.status_code == 503


def test_health_reports_field_but_stays_healthy(main_mod, fresh_lock, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("INSTANCE_NAME", "worker-sahori")
    client = TestClient(main_mod.app)
    body = client.get("/health").json()
    assert body["status"] == "healthy" and body["org_disabled"] is False
    assert body["capacity_lock"] is None

    fresh_lock.lock_until("worker-sahori", time.time() + 3600, ORG_DISABLED_LOCK_REASON)
    resp = client.get("/health")
    assert resp.status_code == 200  # docker healthcheck is `curl -f` — must stay 2xx
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["org_disabled"] is True
    assert body["capacity_lock"]["reason"] == ORG_DISABLED_LOCK_REASON
    assert body["capacity_lock"]["remaining_s"] > 3500
