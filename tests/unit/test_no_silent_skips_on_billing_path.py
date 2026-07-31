"""Regression guard: the activity/billing writer must never skip silently.

A `return` in src/activity/ai_call_writer.py means "no usage_events row" or "no
budget deduction". Both are legitimate outcomes — a row is tenant-scoped and
some calls have no tenant; some apps are not in the plan catalog. What is NOT
legitimate is doing it without a trace: on a money path, a silently missing
record is spend nobody can see and nobody can reconcile.

It also destroys the ledger's usefulness as a MEASURING instrument. That is not
hypothetical. On 2026-07-30 a missing usage_events row was read as "the
research-cloud path does not work"; the wrong conclusion survived two rounds of
investigation until handler instrumentation showed the path had been working the
whole time. The instrument had failed silently, and nothing in the logs said so.

SCOPE — deliberately this one module. A repo-wide version of this check was
attempted and DISCARDED: two attempts gave two unreliable answers (33 hits, then
67 after a "refinement" that flagged `except ... as e` bindings themselves). A
check that cannot be trusted gets suppressed, and then it protects nothing.
Scoped to a small file whose invariant is precise, it is a real guard.
"""
import ast
import pathlib

import pytest

MODULE = pathlib.Path(__file__).resolve().parents[2] / "src" / "activity" / "ai_call_writer.py"

# How far back a logger call may sit and still count as "this skip is explained".
# Generous on purpose: the point is to catch a return with NO logging near it,
# not to police formatting.
LOOKBACK_LINES = 14


def _bare_returns_without_logging(source: str):
    """(function_name, lineno, source_line) for every value-less return whose
    preceding lines carry no logger call.

    Value-less is the precise signal: `return` means "give up on this call",
    while `return x` is a computed result (resolve_ledger_cost, the cached
    identity check) and says nothing about skipping.
    """
    tree = ast.parse(source)
    lines = source.split("\n")
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Return) or inner.value is not None:
                continue
            start = max(node.lineno - 1, inner.lineno - 1 - LOOKBACK_LINES)
            context = "\n".join(lines[start:inner.lineno])
            if "logger." not in context:
                offenders.append((node.name, inner.lineno, lines[inner.lineno - 1].strip()))
    return offenders


def test_module_exists():
    assert MODULE.is_file(), f"expected the activity writer at {MODULE}"


def test_no_skip_returns_without_a_log_line():
    offenders = _bare_returns_without_logging(MODULE.read_text(encoding="utf-8"))
    assert not offenders, (
        "Silent skip(s) on the activity/billing path — a `return` here means no "
        "usage_events row and/or no budget deduction, and it must say so:\n"
        + "\n".join(f"  {fn}() line {ln}: {src}" for fn, ln, src in offenders)
    )


@pytest.mark.parametrize("snippet,expect_offender", [
    # A bare return with no logging nearby -> must be caught.
    ("def f():\n    if x:\n        return\n", True),
    # Same, but explained -> accepted.
    ("def f():\n    if x:\n        logger.warning('why')\n        return\n", False),
    # A computed result is not a skip -> never an offender, logged or not.
    ("def f():\n    return 42\n", False),
])
def test_detector_discriminates(snippet, expect_offender):
    """The guard is only worth having if it fails on the thing it claims to
    catch — verified in both directions rather than assumed."""
    assert bool(_bare_returns_without_logging(snippet)) is expect_offender
