"""Parse-level tests for the Docker prune systemd units."""

import configparser
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parents[2] / "speedbay" / "deploy"


def _unit(name: str) -> configparser.ConfigParser:
    unit = configparser.ConfigParser(interpolation=None)
    with (DEPLOY_DIR / name).open() as unit_file:
        unit.read_file(unit_file)
    return unit


def test_prune_service_targets_only_old_openswe_containers() -> None:
    unit = _unit("openswe-prune.service")
    command = unit["Service"]["ExecStart"]

    assert command == (
        "/usr/bin/docker container prune -f --filter label=openswe.sandbox=1 --filter until=168h"
    )
    assert "docker system prune" not in command
    assert "label=openswe.sandbox=1" in command
    assert "until=168h" in command
    assert unit["Service"]["User"] == "openswe"
    assert unit["Service"]["Type"] == "oneshot"
    assert unit["Service"]["StandardOutput"] == "journal"


def test_prune_timer_runs_weekly_and_catches_up() -> None:
    unit = _unit("openswe-prune.timer")

    assert unit["Timer"]["OnCalendar"] == "weekly"
    assert unit["Timer"].getboolean("Persistent") is True
    assert unit["Timer"]["Unit"] == "openswe-prune.service"
