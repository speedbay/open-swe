"""Unit tests for the speedbay/openswe lifecycle command."""

import importlib.machinery
import importlib.util
import pathlib

import pytest

OPENSWE = pathlib.Path(__file__).resolve().parents[2] / "speedbay" / "openswe"


def _load_openswe():
    loader = importlib.machinery.SourceFileLoader("speedbay_openswe", str(OPENSWE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _stub(path: pathlib.Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def test_ensure_docker_daemon_starts_colima_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "colima-started"
    calls = tmp_path / "colima-calls"
    _stub(tmp_path / "docker", f'test -f "{marker}"')
    _stub(
        tmp_path / "colima",
        f'test "$1" = start || exit 1\nprintf "%s\\n" "$*" >> "{calls}"\n: > "{marker}"',
    )
    monkeypatch.setenv("PATH", str(tmp_path))

    assert _load_openswe().ensure_docker_daemon() is True
    assert calls.read_text().splitlines() == ["start"]


def test_unreachable_docker_without_colima_fails_fast(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub(tmp_path / "docker", "exit 1")
    monkeypatch.setenv("PATH", str(tmp_path))
    openswe = _load_openswe()

    assert openswe.ensure_docker_daemon() is False
    monkeypatch.setattr(openswe, "backend_pids", lambda: [])
    monkeypatch.setattr(openswe, "tunnel_pids", lambda: [])
    assert openswe.start() == 1
    assert (
        "FAIL: docker daemon unreachable (start Docker Desktop / colima first)"
        in capsys.readouterr().err
    )


def test_ensure_docker_daemon_skips_colima_when_docker_is_healthy(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = tmp_path / "colima-calls"
    _stub(tmp_path / "docker", "exit 0")
    _stub(tmp_path / "colima", f'printf "%s\\n" "$*" >> "{calls}"')
    monkeypatch.setenv("PATH", str(tmp_path))

    assert _load_openswe().ensure_docker_daemon() is True
    assert not calls.exists()
