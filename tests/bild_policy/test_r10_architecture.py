"""Reject direct provider SDKs, URLs and transport calls outside the boundary."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PURE_ADAPTERS = {
    "src/bedrock_service.py",
    "src/vision_provider.py",
    "src/providers/openai_compatible.py",
    "src/research_cloud/executor.py",
    "src/research_cloud/luecken.py",
    "src/research_cloud/perplexity.py",
}
BOUNDARY = "src/providers/text_egress.py"


def violations(source: str, path: str) -> list[str]:
    tree = ast.parse(source)
    findings = []
    if path == BOUNDARY:
        return findings
    docs = {
        id(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
    }
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                aliases[name.asname or name.name] = name.name
        if isinstance(node, ast.ImportFrom):
            for name in node.names:
                aliases[name.asname or name.name] = f"{node.module}.{name.name}"

    def name(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return name(node.value) + "." + node.attr
        return ""

    provider_names = set()
    assignments = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
    for _ in range(len(assignments) + 1):
        previous = set(provider_names)
        for assignment in assignments:
            value = name(assignment.value) or ast.unparse(assignment.value)
            if any(
                marker in value
                for marker in (
                    "text_egress.ANTHROPIC",
                    "text_egress.OPENAI",
                    "text_egress.PERPLEXITY",
                )
            ) or any(
                isinstance(n, ast.Name) and n.id in provider_names
                for n in ast.walk(assignment.value)
            ):
                provider_names.update(
                    t.id for t in assignment.targets if isinstance(t, ast.Name)
                )
        if provider_names == previous:
            break

    decorators = {
        id(d)
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        for d in n.decorator_list
    }
    for node in ast.walk(tree):
        if id(node) in decorators:
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = (
                [n.name for n in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            if any(m.split(".")[0] in {"anthropic", "openai"} for m in modules):
                findings.append(f"{node.lineno}: direct provider SDK")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
        ):
            if any(
                host in node.value
                for host in ("api.anthropic.com", "api.openai.com", "api.perplexity.ai")
            ):
                findings.append(f"{node.lineno}: provider URL outside boundary")
        if not isinstance(node, ast.Call):
            continue
        called = name(node.func)
        if (
            called.endswith(
                (
                    ".invoke_model",
                    ".invoke_model_with_response_stream",
                    ".invoke_endpoint",
                )
            )
            and "text_egress." not in called
        ):
            findings.append(f"{node.lineno}: direct provider invocation")
        if (
            called.endswith((".post", ".stream", ".request", ".send"))
            and "text_egress." not in called
        ):
            # A provider adapter cannot acquire a second, unguarded transport.
            if path in PURE_ADAPTERS:
                findings.append(f"{node.lineno}: direct provider transport")
            # Also catch provider targets aliased into other services.
            for arg in node.args:
                target = ast.unparse(arg)
                if any(
                    isinstance(n, ast.Name) and n.id in provider_names
                    for n in ast.walk(arg)
                ) or any(
                    marker in target
                    for marker in ("ANTHROPIC", "PERPLEXITY", "_stt", "text_egress.")
                ):
                    findings.append(f"{node.lineno}: provider target outside boundary")
        if called in {"__import__", "importlib.import_module"}:
            for arg in node.args:
                if isinstance(arg, ast.Constant) and arg.value in {
                    "anthropic",
                    "openai",
                }:
                    findings.append(f"{node.lineno}: dynamic provider SDK")
    return findings


def test_no_provider_bypass():
    found = {}
    for source in (ROOT / "src").rglob("*.py"):
        relative = source.relative_to(ROOT).as_posix()
        errors = violations(source.read_text(), relative)
        if errors:
            found[relative] = errors
    assert not found, found


@pytest.mark.parametrize(
    "mutant",
    [
        "import anthropic as a\nclient = a.Anthropic()\n",
        "from openai import OpenAI as Client\nclient = Client()\n",
        "import importlib\nclient = importlib.import_module('anthropic')\n",
        "import httpx\nhttpx.post('https://api.anthropic.com/v1/messages', json={})\n",
        "async def send(c, url, body):\n    return await c.post(url, json=body)\n",
    ],
)
def test_injected_direct_client_is_rejected(mutant):
    assert violations(mutant, "src/research_cloud/executor.py")


def test_privacy_alias_cannot_hide_provider_destination():
    mutant = (
        "from src.providers import text_egress as egress\n"
        "url = egress.ANTHROPIC_MESSAGES_URL\n"
        "alias = url\n"
        "async def call(client):\n"
        "    return await client.post(alias, json={})\n"
    )
    assert violations(mutant, "src/privacy_service/app.py")
