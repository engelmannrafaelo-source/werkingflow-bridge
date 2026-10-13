"""Public build provenance; no app startup or external service required."""
import json
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src import version


def test_public_version_has_only_contract_fields(tmp_path, monkeypatch):
    metadata = tmp_path / "build.json"
    metadata.write_text(json.dumps({
        "git_sha": "a" * 40, "built_at": "2026-10-11T12:00:00Z",
        "secret": "must-not-leak",
    }))
    monkeypatch.setattr(version, "BUILD_METADATA", metadata)
    app = FastAPI()
    app.include_router(version.router)
    response = TestClient(app).get("/v1/version")
    assert response.status_code == 200
    assert response.json() == {
        "git_sha": "a" * 40, "built_at": "2026-10-11T12:00:00Z",
        "contract_version": "1",
    }
    assert TestClient(app).get("/v1/version/private").status_code == 404


def test_missing_metadata_fails_loud_without_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "BUILD_METADATA", tmp_path / "missing.json")
    app = FastAPI()
    app.include_router(version.router)
    response = TestClient(app).get("/v1/version")
    assert response.status_code == 503
    assert response.json()["detail"]["retryable"] is False
    assert response.json()["detail"]["message"].endswith("Build metadata unavailable")


@pytest.mark.parametrize("sha,stamp", [
    ("unknown", "2026-10-11T12:00:00Z"),
    ("a" * 40, "not-a-date"),
    ("a" * 40, "2026-10-11T12:00:00"),
])
def test_invalid_metadata_is_not_published(tmp_path, monkeypatch, sha, stamp):
    metadata = tmp_path / "build.json"
    metadata.write_text(json.dumps({"git_sha": sha, "built_at": stamp}))
    monkeypatch.setattr(version, "BUILD_METADATA", metadata)
    app = FastAPI()
    app.include_router(version.router)
    assert TestClient(app).get("/v1/version").status_code == 503


def test_worker_mounts_version_router():
    # The worker import starts unrelated SDK setup; verify its composition via AST.
    import ast
    tree = ast.parse((Path(__file__).parents[1] / "src/main.py").read_text())
    assert any(isinstance(n, ast.ImportFrom) and n.module == "src.version"
               and any(a.name == "router" and a.asname == "version_router"
                       for a in n.names) for n in ast.walk(tree))
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "include_router" and n.args
               and isinstance(n.args[0], ast.Name) and n.args[0].id == "version_router"
               for n in ast.walk(tree))

def test_tenant_exemption_is_exact():
    from src.tenant.middleware import TenantMiddleware
    assert TenantMiddleware._is_exempt_path(None, "/v1/version")
    assert not TenantMiddleware._is_exempt_path(None, "/v1/version/private")
    assert not TenantMiddleware._is_exempt_path(None, "/v1/research")


def test_docker_build_records_commit_and_build_time(tmp_path):
    import os
    import shlex
    import subprocess
    from datetime import datetime, timezone
    dockerfile = (Path(__file__).parents[1] / "docker/Dockerfile.worker").read_text()
    command = next(line.removeprefix("RUN ") for line in dockerfile.splitlines()
                   if line.startswith("RUN python -c") and "build-metadata.json" in line)
    target = tmp_path / "build-metadata.json"
    command = command.replace("/app/src/build-metadata.json", str(target))
    before = datetime.now(timezone.utc)
    subprocess.run([__import__("sys").executable, *shlex.split(command)[1:]], env={**os.environ, "GIT_COMMIT": "b" * 40},
                   check=True, timeout=5)
    data = json.loads(target.read_text())
    assert data["git_sha"] == "b" * 40
    assert before <= datetime.fromisoformat(data["built_at"]) <= datetime.now(timezone.utc)


@pytest.mark.parametrize("missing", ["git_sha", "built_at"])
def test_missing_metadata_key_fails(tmp_path, monkeypatch, missing):
    data = {"git_sha": "a" * 40, "built_at": "2026-10-11T12:00:00Z"}
    del data[missing]
    target = tmp_path / "build.json"
    target.write_text(json.dumps(data))
    monkeypatch.setattr(version, "BUILD_METADATA", target)
    app = FastAPI()
    app.include_router(version.router)
    assert TestClient(app).get("/v1/version").status_code == 503


def test_real_worker_exception_envelope(tmp_path, monkeypatch):
    # Compile the actual registered handler without starting unrelated SDKs.
    import ast
    from starlette.exceptions import HTTPException
    from starlette.responses import JSONResponse
    from src.middleware.bridge_error import bridge_error, SOURCE_BRIDGE_INTERNAL, TYPE_INTERNAL
    tree = ast.parse((Path(__file__).parents[1] / "src/main.py").read_text())
    handler = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == "http_exception_handler")
    handler.decorator_list = []
    handler.returns = None
    for arg in handler.args.args:
        arg.annotation = None
    ns = dict(JSONResponse=JSONResponse, bridge_error=bridge_error,
              SOURCE_BRIDGE_INTERNAL=SOURCE_BRIDGE_INTERNAL, TYPE_INTERNAL=TYPE_INTERNAL)
    exec(compile(ast.Module(body=[handler], type_ignores=[]), "worker-handler", "exec"), ns)
    app = FastAPI()
    app.add_exception_handler(HTTPException, ns["http_exception_handler"])
    app.include_router(version.router)
    monkeypatch.setattr(version, "BUILD_METADATA", tmp_path / "absent.json")
    response = TestClient(app).get("/v1/version")
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["retryable"] is False
    assert error["type"] != "bridge_throttle"
    assert error["message"].endswith("Build metadata unavailable")
