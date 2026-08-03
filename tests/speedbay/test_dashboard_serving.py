"""Parse-level tests for the production dashboard serving configuration."""

import configparser
from pathlib import Path

import yaml

DEPLOY_DIR = Path(__file__).resolve().parents[2] / "speedbay" / "deploy"


def test_caddy_proxies_dashboard_api_before_spa_fallback() -> None:
    caddyfile = (DEPLOY_DIR / "Caddyfile").read_text()

    proxy = "reverse_proxy /dashboard/api/* localhost:2024"
    fallback = "try_files {path} /_shell.html"
    assert ":8080 {" in caddyfile
    assert "root * /home/openswe/open-swe/ui/.output/public" in caddyfile
    assert proxy in caddyfile
    assert fallback in caddyfile
    assert "file_server" in caddyfile
    assert caddyfile.index(proxy) < caddyfile.index(fallback)


def test_tunnel_routes_dashboard_before_catch_all() -> None:
    config = yaml.safe_load((DEPLOY_DIR / "tunnel-config.yml").read_text())
    ingress = config["ingress"]

    dashboard = {
        "hostname": "openswe-dash.speedbay.com",
        "service": "http://localhost:8080",
    }
    catch_all = {"service": "http_status:404"}
    assert dashboard in ingress
    assert catch_all in ingress
    assert ingress.index(dashboard) < ingress.index(catch_all)


def test_dashboard_unit_runs_caddy_as_openswe() -> None:
    unit = configparser.ConfigParser(interpolation=None)
    with (DEPLOY_DIR / "openswe-dashboard.service").open() as unit_file:
        unit.read_file(unit_file)

    assert unit["Unit"]["After"].split() == ["network-online.target"]
    assert unit["Service"]["User"] == "openswe"
    assert unit["Service"]["Restart"] == "always"
    assert unit["Service"]["ExecStart"] == (
        "caddy run --config /home/openswe/open-swe/speedbay/deploy/Caddyfile"
    )
