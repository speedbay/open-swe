"""Tests for provisioning the Elementor Pro license into Docker sandboxes (OPE-107)."""

from __future__ import annotations

import subprocess

import pytest

from agent.speedbay import docker_sandbox


def _capture_docker_run(monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(docker_sandbox, "_docker", fake_docker)
    monkeypatch.setattr(docker_sandbox, "_sweep_expired", lambda: None)
    monkeypatch.setattr(docker_sandbox, "_provision", lambda _container: None)
    docker_sandbox.create_docker_sandbox()
    return next(args for args in calls if args[0] == "run")


def test_docker_run_includes_elementor_license_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ELEMENTOR_PRO_LICENSE_KEY", "test-license")

    argv = _capture_docker_run(monkeypatch)

    index = argv.index("ELEMENTOR_PRO_LICENSE_KEY=test-license")
    assert argv[index - 1] == "--env"


def test_docker_run_omits_elementor_license_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ELEMENTOR_PRO_LICENSE_KEY", raising=False)

    argv = _capture_docker_run(monkeypatch)

    assert not any(arg.startswith("ELEMENTOR_PRO_LICENSE_KEY=") for arg in argv)


def test_elementor_license_is_available_inside_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        docker_sandbox.validate_startup_config()
    except (ValueError, FileNotFoundError):
        pytest.skip("docker daemon or sandbox image unavailable")

    monkeypatch.setenv("ELEMENTOR_PRO_LICENSE_KEY", "test-license")
    monkeypatch.setattr(docker_sandbox, "_provision", lambda _container: None)
    sandbox = docker_sandbox.create_docker_sandbox()
    try:
        result = sandbox.execute('test "$ELEMENTOR_PRO_LICENSE_KEY" = "test-license"')
        assert result.exit_code == 0, result.output
    finally:
        subprocess.run(["docker", "rm", "-f", sandbox.id], capture_output=True)
