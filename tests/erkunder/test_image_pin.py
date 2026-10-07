"""Static deployment contract; no daemon, image build or credentials required."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "docker"


def compose():
    return yaml.safe_load((DOCKER / "docker-compose.yml").read_text())


def test_cli_and_base_image_pins_match_worker():
    worker = (DOCKER / "Dockerfile.worker").read_text()
    erkunder = (DOCKER / "Dockerfile.erkunder").read_text()
    pattern = r"RUN npm install -g @anthropic-ai/claude-code@([^\s]+)"
    assert re.findall(pattern, erkunder) == re.findall(pattern, worker)
    assert len(re.findall(pattern, erkunder)) == 1
    assert re.findall(r"^FROM .+$", erkunder, re.MULTILINE) == re.findall(
        r"^FROM .+$", worker, re.MULTILINE
    )
    assert "bubblewrap" not in erkunder
    for package in ("pandas", "pyarrow", "numpy", "scipy", "matplotlib", "openpyxl"):
        assert re.search(rf"\b{package}==\d+\.\d+\.\d+\b", erkunder)


def test_compose_yaml_and_internal_network():
    config = compose()
    assert config["networks"]["erkunder-intern"]["internal"] is True
    assert "erkunder-arbeit" in config["volumes"]
    assert config["services"]["erkunder"]["networks"] == [
        "bridge-net",
        "erkunder-intern",
    ]


@pytest.mark.parametrize("number", [1, 2, 3])
def test_places_are_isolated(number):
    service = compose()["services"][f"erkunder-platz-{number}"]
    assert service["user"] == f"110{number}:1100"
    assert service["init"] is True
    assert service["mem_limit"] == service["memswap_limit"] == "3g"
    assert service["pids_limit"] == 512
    assert service["read_only"] is True
    assert service["tmpfs"] == ["/tmp:size=512m"]
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["networks"] == ["erkunder-intern"]
    assert service["volumes"] == ["erkunder-arbeit:/arbeit"]
    assert "secrets" not in service
    assert "env_file" not in service
    assert "ports" not in service
    environment = service["environment"]
    assert environment["HTTPS_PROXY"] == "http://erkunder-ausgang:8888"
    assert environment["HTTP_PROXY"] == environment["HTTPS_PROXY"]
    assert environment["NO_PROXY"] == "erkunder"
    assert environment["ERKUNDER_INTERNAL_TOKEN"] == "${ERKUNDER_INTERNAL_TOKEN:-}"
    assert environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert set(environment) == {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "ERKUNDER_INTERNAL_TOKEN",
    }


def test_controller_receives_only_internal_secret():
    service = compose()["services"]["erkunder"]
    assert "secrets" not in service
    assert "env_file" not in service
    assert "ports" not in service
    assert service["volumes"] == ["erkunder-arbeit:/arbeit"]
    assert set(service["environment"]) == {"ERKUNDER_INTERNAL_TOKEN"}
    assert (
        service["environment"]["ERKUNDER_INTERNAL_TOKEN"]
        == "${ERKUNDER_INTERNAL_TOKEN:-}"
    )
    assert service["mem_limit"] == service["memswap_limit"] == "1g"


@pytest.mark.parametrize("number", [1, 2, 3, 4])
def test_worker_inherits_authorization_from_platform_env(number):
    worker = compose()["services"][f"worker{number}"]
    assert "../secrets/platform.env" in worker["env_file"]
    assert "ERKUNDER_URL=http://erkunder:8100" in worker["environment"]
    # A valueless override would remove env_file values when the shell is unset.
    assert not any(
        item.split("=", 1)[0]
        in {"ERKUNDER_INTERNAL_TOKEN", "ERKUNDER_ALLOWED_KEY_SHA256"}
        for item in worker["environment"]
    )


def test_proxy_is_default_deny_with_single_host_and_connect_port():
    config = (DOCKER / "erkunder/tinyproxy.conf").read_text()
    assert 'LogFile "/tmp/tinyproxy.log"' in config
    assert "/dev/stdout" not in config
    assert re.findall(r"^ConnectPort (\d+)$", config, re.MULTILINE) == ["443"]
    assert "FilterDefaultDeny Yes" in config
    assert "FilterURLs Off" in config
    assert "FilterType ere" in config
    assert 'Filter "/etc/erkunder/tinyproxy.filter"' in config
    image = (DOCKER / "Dockerfile.erkunder").read_text()
    assert "'^api\\.anthropic\\.com$' > /etc/erkunder/tinyproxy.filter" in image
    proxy = compose()["services"]["erkunder-ausgang"]
    assert proxy["command"] == ["/usr/local/bin/erkunder-proxy"]
    assert set(proxy["networks"]) == {"erkunder-intern", "bridge-net"}
    assert "ports" not in proxy
    assert "secrets" not in proxy
    assert "env_file" not in proxy
    proxy_entrypoint = (DOCKER / "erkunder/erkunder-proxy").read_text()
    assert 'log=/tmp/tinyproxy.log' in proxy_entrypoint
    assert 'chown nobody:nogroup "$log"' in proxy_entrypoint
    assert "max_log_bytes=1048576" in proxy_entrypoint
    assert "ulimit -f 2048" in proxy_entrypoint
    assert 'tail -n 0 -F "$log" &' in proxy_entrypoint
    assert 'proxy_pid=$!' in proxy_entrypoint
    assert 'kill -0 "$mirror_pid"' in proxy_entrypoint
    assert ': > "$log"' in proxy_entrypoint
    assert (DOCKER / "erkunder/erkunder-proxy").stat().st_mode & 0o111
