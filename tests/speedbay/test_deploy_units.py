"""Parse-level tests for the systemd deployment configuration."""

import configparser
from pathlib import Path

import yaml

DEPLOY_DIR = Path(__file__).resolve().parents[2] / "speedbay" / "deploy"
SETUP_PATH = Path(__file__).resolve().parents[2] / "SETUP.md"


def _setup() -> str:
    return SETUP_PATH.read_text()


def _unit(name: str) -> configparser.ConfigParser:
    unit = configparser.ConfigParser(interpolation=None)
    with (DEPLOY_DIR / name).open() as unit_file:
        unit.read_file(unit_file)
    return unit


def test_backend_unit_supervises_run_dev_after_docker() -> None:
    unit = _unit("openswe-backend.service")

    assert unit["Service"]["User"] == "openswe"
    assert unit["Service"]["Restart"] == "always"
    assert unit["Service"]["WorkingDirectory"] == "/home/openswe/open-swe"
    assert unit["Service"]["ExecStart"] == "/home/openswe/open-swe/speedbay/run-dev.sh"
    assert unit["Service"]["StandardOutput"] == "append:/tmp/openswe-backend.log"
    assert unit["Service"]["StandardError"] == "inherit"
    assert unit["Unit"]["Requires"].split() == ["docker.service"]
    assert set(unit["Unit"]["After"].split()) == {"network-online.target", "docker.service"}


def test_setup_initial_install_commands_run_as_checkout_owner() -> None:
    setup = _setup()

    assert (
        """```bash
sudo -Hu openswe git clone git@github.com:speedbay/open-swe.git /home/openswe/open-swe
sudo -Hu openswe sh -c 'cd /home/openswe/open-swe && uv sync'
```"""
        in setup
    )
    assert (
        """```bash
sudo -Hu openswe sh -c 'cd /home/openswe/open-swe && docker build -f speedbay/docker/Dockerfile.sandbox -t openswe-sandbox:dev speedbay/docker && docker image inspect openswe-sandbox:dev >/dev/null && echo image-present'
```"""
        in setup
    )
    assert (
        """```bash
sudo -Hu openswe sh -c 'cd /home/openswe/open-swe/ui && pnpm install --frozen-lockfile && pnpm build'
```"""
        in setup
    )


def test_setup_upgrade_commands_run_as_checkout_owner() -> None:
    setup = _setup()

    assert (
        """```bash
sudo -Hu openswe sh -c 'cd /home/openswe/open-swe && git pull --ff-only && uv sync && docker build -f speedbay/docker/Dockerfile.sandbox -t openswe-sandbox:dev speedbay/docker && cd ui && pnpm install --frozen-lockfile && pnpm build'
sudo systemctl daemon-reload
sudo systemctl restart openswe-backend.service openswe-tunnel.service openswe-dashboard.service
```"""
        in setup
    )


def test_setup_checkout_owner_has_documented_docker_access() -> None:
    setup = _setup()

    assert (
        """```bash
sudo usermod -aG docker openswe
```"""
        in setup
    )
    assert (
        """```bash
sudo -u openswe docker info >/dev/null && git --version && uv --version && node -v
pnpm --version && cloudflared --version && caddy version
```"""
        in setup
    )
    assert (
        """```bash
sudo -Hu openswe sh -c 'cd /home/openswe/open-swe && docker build -f speedbay/docker/Dockerfile.sandbox -t openswe-sandbox:dev speedbay/docker && docker image inspect openswe-sandbox:dev >/dev/null && echo image-present'
```"""
        in setup
    )
    assert (
        """```bash
sudo -Hu openswe sh -c 'cd /home/openswe/open-swe && git pull --ff-only && uv sync && docker build -f speedbay/docker/Dockerfile.sandbox -t openswe-sandbox:dev speedbay/docker && cd ui && pnpm install --frozen-lockfile && pnpm build'
sudo systemctl daemon-reload
sudo systemctl restart openswe-backend.service openswe-tunnel.service openswe-dashboard.service
```"""
        in setup
    )


def test_tunnel_unit_supervises_named_tunnel() -> None:
    unit = _unit("openswe-tunnel.service")

    assert unit["Service"]["User"] == "openswe"
    assert unit["Service"]["Restart"] == "always"
    assert unit["Service"]["ExecStart"] == (
        "cloudflared tunnel --config "
        "/home/openswe/open-swe/speedbay/deploy/tunnel-config.yml run openswe"
    )
    assert unit["Service"]["StandardOutput"] == "append:/tmp/openswe-tunnel.log"
    assert unit["Service"]["StandardError"] == "inherit"
    assert unit["Unit"]["After"].split() == ["network-online.target"]


def test_tunnel_config_routes_public_webhooks_and_health_then_returns_404() -> None:
    config = yaml.safe_load((DEPLOY_DIR / "tunnel-config.yml").read_text())

    assert config["tunnel"] == "openswe"
    assert config["credentials-file"] == (
        "/home/openswe/.cloudflared/66d09a43-7dac-4001-9adb-b6df1806796d.json"
    )
    assert config["ingress"] == [
        {
            "hostname": "openswe.speedbay.com",
            "path": "^/webhooks/.*",
            "service": "http://localhost:2024",
        },
        {
            "hostname": "openswe.speedbay.com",
            "path": "^/health$",
            "service": "http://localhost:2024",
        },
        {"hostname": "openswe-dash.speedbay.com", "service": "http://localhost:8080"},
        {"service": "http_status:404"},
    ]
    assert not any(
        rule.get("hostname") == "openswe.speedbay.com" and "path" not in rule
        for rule in config["ingress"]
    )
