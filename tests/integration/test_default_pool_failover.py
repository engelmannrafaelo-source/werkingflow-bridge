"""
Tests for Default Pool failover behavior — Autobahn-compliant.

Confirms (red before fix, green after):
1. Client gets 503 (not 502) when ALL workers return 500
2. Client gets 503 (not 502) when ALL workers return 502
3. Cross-worker retry fires when workers crash (client sees 200 via retry to healthy worker)
4. 429-path is unchanged (client still sees 429)
5. 503 error body carries bridge_type = "capacity_busy"

Setup: isolated docker compose (project=nginx-test, port=19080).
NEVER uses --remove-orphans. NEVER touches live bridge containers.
"""
import os, json, time, socket, subprocess
import requests
import pytest

NGINX_PORT = 19080
NGINX_URL  = f"http://localhost:{NGINX_PORT}"
PROJECT    = "nginx-test"
HERE       = os.path.dirname(__file__)
COMPOSE_FILE = os.path.join(HERE, "docker-compose-failover-test.yml")
BRIDGE_DIR   = os.path.abspath(os.path.join(HERE, "..", ".."))
CONF_PATH    = os.path.join(HERE, "nginx-test.conf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def wait_for_port(port: int, timeout: int = 40) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("localhost", port), timeout=1):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.5)
    return False


def wait_for_nginx(port: int, timeout: int = 45) -> bool:
    """Wait until nginx is fully ready by polling /health (not just TCP port)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"http://localhost:{port}/health", timeout=2)
            if r.status_code < 502:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def compose_run(*args, env=None, check=True):
    result = subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "--project-name", PROJECT, *args],
        capture_output=True, text=True, env=env or os.environ.copy()
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"docker compose {args} failed:\n{result.stderr}\n{result.stdout}")
    return result


def start_stack(w1=500, w2=500, w3=500, w4=500):
    """Start test nginx stack with given worker status codes."""
    env = {**os.environ,
           "WORKER1_STATUS": str(w1), "WORKER2_STATUS": str(w2),
           "WORKER3_STATUS": str(w3), "WORKER4_STATUS": str(w4)}
    compose_run("down", env=env, check=False)
    time.sleep(1)  # let port release
    compose_run("up", "-d", env=env)
    if not wait_for_port(NGINX_PORT, timeout=30):
        logs = compose_run("logs", check=False).stdout
        compose_run("down", env=env, check=False)
        pytest.fail(f"nginx port did not open. Logs:\n{logs}")
    if not wait_for_nginx(NGINX_PORT, timeout=30):
        logs = compose_run("logs", check=False).stdout
        compose_run("down", env=env, check=False)
        pytest.fail(f"nginx health check failed after port open. Logs:\n{logs}")


def stop_stack():
    """Stop nginx test stack (no --remove-orphans)."""
    compose_run("down", check=False)


def post_chat(extra_headers=None):
    """POST a minimal chat completion request to the bridge."""
    headers = {"Content-Type": "application/json", "Authorization": "Bearer test"}
    if extra_headers:
        headers.update(extra_headers)
    return requests.post(
        f"{NGINX_URL}/v1/chat/completions",
        json={"model": "claude-3-5-sonnet-20241022",
              "messages": [{"role": "user", "content": "test"}]},
        headers=headers,
        timeout=15,
    )


# ---------------------------------------------------------------------------
# Module-level: preprocess nginx.conf (substitute template vars once)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def processed_nginx_conf():
    """Substitute ${BRIDGE_PROD_HOST} and ${BRIDGE_ID} in nginx.conf for tests."""
    import shutil as _shutil
    if os.path.isdir(CONF_PATH):
        _shutil.rmtree(CONF_PATH)
    conf_src = os.path.join(BRIDGE_DIR, "docker", "nginx.conf")
    with open(conf_src) as f:
        raw = f.read()
    processed = raw.replace("${BRIDGE_PROD_HOST}", "worker4").replace("${BRIDGE_ID}", "test")
    with open(CONF_PATH, "w") as f:
        f.write(processed)
    yield
    stop_stack()
    if os.path.isdir(CONF_PATH):
        _shutil.rmtree(CONF_PATH)
    elif os.path.exists(CONF_PATH):
        os.unlink(CONF_PATH)


# ---------------------------------------------------------------------------
# Tests: 500 normalisation — all workers return 500
# ---------------------------------------------------------------------------

class TestWorker500Normalisation:
    """Worker 500 crashes must be normalised to 503 (never 502)."""

    @pytest.fixture(autouse=True, scope="class")
    def stack(self):
        start_stack(500, 500, 500, 500)
        yield
        stop_stack()

    def test_status_is_503_not_502(self):
        """All workers return 500 → client MUST see 503 (not 502)."""
        r = post_chat()
        assert r.status_code == 503, (
            f"Expected 503 capacity_busy, got {r.status_code}. "
            "proxy_next_upstream may be returning 502 worker_crash.\n"
            f"Body: {r.text}"
        )

    def test_retry_after_header_present(self):
        """503 response must include Retry-After header."""
        r = post_chat()
        assert r.headers.get("Retry-After"), f"Missing Retry-After header. Status={r.status_code}"

    def test_bridge_type_is_capacity_busy(self):
        """Error body must carry bridge_type=capacity_busy (not worker_crash)."""
        r = post_chat()
        body = r.json()
        assert "error" in body, f"No error key in response: {body}"
        bridge_type = body["error"].get("bridge_type", "MISSING")
        assert bridge_type == "capacity_busy", (
            f"Expected bridge_type=capacity_busy, got {bridge_type}. "
            "@bridge_full must consolidate 5xx to single 503 capacity_busy."
        )


# ---------------------------------------------------------------------------
# Tests: 502 normalisation — all workers return 502
# ---------------------------------------------------------------------------

class TestWorker502Normalisation:
    """Worker 502 gateway errors must also be normalised to 503."""

    @pytest.fixture(autouse=True, scope="class")
    def stack(self):
        start_stack(502, 502, 502, 502)
        yield
        stop_stack()

    def test_status_is_503_not_502(self):
        """All workers return 502 → client MUST see 503 (not 502)."""
        r = post_chat()
        assert r.status_code == 503, (
            f"Expected 503, got {r.status_code}. "
            "@bridge_full must not pass 502 to client.\n"
            f"Body: {r.text}"
        )

    def test_bridge_type_is_capacity_busy(self):
        """502 from worker must map to bridge_type=capacity_busy."""
        r = post_chat()
        body = r.json()
        bridge_type = body.get("error", {}).get("bridge_type", "MISSING")
        assert bridge_type == "capacity_busy", (
            f"Expected capacity_busy, got {bridge_type}"
        )


# ---------------------------------------------------------------------------
# Tests: Cross-worker retry — 3 workers fail, 1 healthy
# ---------------------------------------------------------------------------

class TestCrossWorkerRetry:
    """nginx must retry across workers when some crash (Default Pool)."""

    @pytest.fixture(autouse=True, scope="class")
    def stack(self):
        # workers 1-3 return 500, worker4 returns 200
        start_stack(500, 500, 500, 200)
        yield
        stop_stack()

    def test_retry_reaches_healthy_worker(self):
        """3 of 4 workers fail with 500 → nginx retry MUST reach worker4 → 200."""
        # With proxy_next_upstream off (current bug), this returns 503/502.
        # After fix (proxy_next_upstream enabled + claude_workers upstream), returns 200.
        r = post_chat()
        assert r.status_code == 200, (
            f"Expected 200 via cross-worker retry, got {r.status_code}. "
            "proxy_next_upstream is likely still off for the default pool.\n"
            f"Body: {r.text}"
        )

    def test_retry_multiple_requests_all_succeed(self):
        """Verify retry is consistent across multiple requests."""
        for i in range(3):
            r = post_chat()
            assert r.status_code == 200, (
                f"Request {i+1}: Expected 200, got {r.status_code}. Body: {r.text}"
            )


# ---------------------------------------------------------------------------
# Tests: 429 path unchanged
# ---------------------------------------------------------------------------

class Test429Unchanged:
    """Anthropic rate-limit 429 must still surface as 429 to client (not 503)."""

    @pytest.fixture(autouse=True, scope="class")
    def stack(self):
        start_stack(429, 429, 429, 429)
        yield
        stop_stack()

    def test_429_returned_as_429(self):
        """All workers return 429 → client MUST see 429 (not 503)."""
        r = post_chat()
        assert r.status_code == 429, (
            f"Expected 429 rate_limited, got {r.status_code}. "
            "429 path must not be changed by the capacity_busy consolidation.\n"
            f"Body: {r.text}"
        )

    def test_bridge_type_is_rate_limited(self):
        """429 error must have bridge_type rate_limited or pool_exhausted."""
        r = post_chat()
        body = r.json()
        bridge_type = body.get("error", {}).get("bridge_type", "MISSING")
        assert bridge_type in ("rate_limited", "pool_exhausted"), (
            f"Expected rate_limited/pool_exhausted, got {bridge_type}"
        )
