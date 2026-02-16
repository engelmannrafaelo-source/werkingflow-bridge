"""Gemini CLI Wrapper — Subprocess-based Gemini integration.

Calls the `gemini` CLI binary as a subprocess with --output-format json.
Uses the CLI's built-in OAuth (Google account subscription models).

This is separate from the OpenAI-compatible path (gemini_oauth.py + httpx)
because the CLI handles its own auth, session management, and billing
through Google's infrastructure.

Output format (from `gemini -p "..." --output-format json`):
{
  "session_id": "uuid",
  "response": "...",
  "stats": {
    "models": {
      "gemini-2.5-flash": {
        "api": {"totalRequests": 1, "totalErrors": 0, "totalLatencyMs": 2455},
        "tokens": {"input": 8296, "prompt": 8296, "candidates": 1, "total": 8348}
      }
    }
  }
}
"""

import asyncio
import json
import logging
import shutil
import time
from typing import Optional

logger = logging.getLogger(__name__)

# CLI timeout: 5 minutes (generous for long prompts)
CLI_TIMEOUT_SECONDS = 300


def _find_gemini_binary() -> str:
    """Find the gemini CLI binary path.

    Raises:
        RuntimeError: If gemini CLI is not installed
    """
    path = shutil.which("gemini")
    if not path:
        raise RuntimeError(
            "Gemini CLI not installed. Install: npm install -g @anthropic-ai/gemini-cli"
        )
    return path


async def call_gemini_cli(
    prompt: str,
    model: str = "gemini-2.5-flash",
    system_prompt: Optional[str] = None,
) -> dict:
    """Call Gemini CLI as subprocess and return OpenAI-compatible response.

    Args:
        prompt: The user prompt
        model: Gemini model ID (e.g. "gemini-2.5-flash", "gemini-2.5-pro")
        system_prompt: Optional system prompt (prepended to prompt)

    Returns:
        OpenAI-compatible response dict with choices and usage

    Raises:
        RuntimeError: If CLI call fails or returns invalid JSON
    """
    binary = _find_gemini_binary()

    # Build the full prompt (system + user)
    full_prompt = prompt
    if system_prompt:
        full_prompt = f"{system_prompt}\n\n{prompt}"

    cmd = [binary, "-p", full_prompt, "--model", model, "--output-format", "json"]

    start = time.time()
    logger.info(f"🌐 Gemini CLI call: model={model}, prompt_len={len(full_prompt)}")

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        process.kill()
        raise RuntimeError(
            f"Gemini CLI timed out after {CLI_TIMEOUT_SECONDS}s (model={model})"
        )

    duration = time.time() - start

    if process.returncode != 0:
        error_msg = stderr.decode()[:500] if stderr else "unknown error"
        logger.error(f"❌ Gemini CLI failed (exit {process.returncode}): {error_msg}")
        raise RuntimeError(f"Gemini CLI exit {process.returncode}: {error_msg}")

    # Parse JSON from stdout (skip any non-JSON lines like "Loaded cached credentials.")
    raw_output = stdout.decode()
    json_start = raw_output.find("{")
    if json_start < 0:
        logger.error(f"❌ Gemini CLI returned no JSON: {raw_output[:200]}")
        raise RuntimeError(f"Gemini CLI returned no JSON output")

    try:
        cli_response = json.loads(raw_output[json_start:])
    except json.JSONDecodeError as e:
        logger.error(f"❌ Gemini CLI invalid JSON: {e}")
        raise RuntimeError(f"Gemini CLI returned invalid JSON: {e}")

    response_text = cli_response.get("response", "")
    if not response_text:
        logger.warning("Gemini CLI returned empty response")

    # Extract token usage from stats
    stats = cli_response.get("stats", {})
    models_stats = stats.get("models", {})
    tokens = {}
    for model_name, model_data in models_stats.items():
        tokens = model_data.get("tokens", {})
        break  # First model's stats

    prompt_tokens = tokens.get("input", 0) or tokens.get("prompt", 0)
    completion_tokens = tokens.get("candidates", 0)
    total_tokens = tokens.get("total", 0) or (prompt_tokens + completion_tokens)

    logger.info(
        f"✅ Gemini CLI response in {duration:.2f}s "
        f"(tokens: {total_tokens}, model: {model})"
    )

    # Return OpenAI-compatible format
    return {
        "id": f"gemini-{cli_response.get('session_id', 'unknown')[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_text,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }
