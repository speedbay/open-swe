"""Unit tests for the speedbay/openswe lifecycle command."""

import importlib.machinery
import importlib.util
import os
import pathlib
import subprocess

import pytest

OPENSWE = pathlib.Path(__file__).resolve().parents[2] / "speedbay" / "openswe"
GH_SHIM = pathlib.Path(__file__).resolve().parents[2] / "speedbay" / "bin" / "gh"


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


def test_gh_shim_uses_next_path_entry_without_recursing(tmp_path: pathlib.Path) -> None:
    first = tmp_path / "first"
    real = tmp_path / "real"
    first.mkdir()
    real.mkdir()
    (first / "gh").symlink_to(GH_SHIM)
    received = tmp_path / "received"
    _stub(real / "gh", f'printf "%s\\n" "$@" > "{received}"')

    subprocess.run(
        [str(first / "gh"), "issue", "list"],
        env={**os.environ, "GH_TOKEN": "real", "PATH": f"{first}:{real}:{os.environ['PATH']}"},
        check=True,
    )

    assert received.read_text().splitlines() == ["issue", "list"]


def test_gh_shim_searches_the_current_directory_for_a_trailing_path_colon(
    tmp_path: pathlib.Path,
) -> None:
    first = tmp_path / "first"
    tools = tmp_path / "tools"
    real = tmp_path / "real"
    first.mkdir()
    tools.mkdir()
    real.mkdir()
    (first / "gh").symlink_to(GH_SHIM)
    for command in ("basename", "dirname", "readlink"):
        command_path = next(
            path
            for path in os.environ["PATH"].split(":")
            if (pathlib.Path(path) / command).is_file()
        )
        (tools / command).symlink_to(pathlib.Path(command_path) / command)
    received = tmp_path / "received"
    _stub(real / "gh", f'printf "%s\\n" "$@" > "{received}"')

    subprocess.run(
        [str(GH_SHIM), "issue", "list"],
        cwd=real,
        env={"GH_TOKEN": "real", "PATH": f"{first}:{tools}:"},
        check=True,
    )

    assert received.read_text().splitlines() == ["issue", "list"]


def test_missing_docker_executable_is_a_clean_start_failure(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    openswe = _load_openswe()
    spawned = False
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(openswe, "backend_pids", lambda: [])
    monkeypatch.setattr(openswe, "tunnel_pids", lambda: [])

    def spawn(*_args: object) -> None:
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(openswe, "_spawn", spawn)

    assert openswe.start() == 1
    assert (
        "FAIL: docker daemon unreachable (start Docker Desktop / colima first)"
        in capsys.readouterr().err
    )
    assert spawned is False


def _wait_for_exit(process: subprocess.Popen[bytes]) -> None:
    process.wait(timeout=5)


@pytest.mark.parametrize(
    ("failure", "spawn_error"),
    [
        ("backend_health", None),
        ("tunnel_spawn", FileNotFoundError("cloudflared")),
        ("tunnel_spawn", PermissionError("cloudflared")),
        ("public_health", None),
    ],
)
def test_failed_start_terminates_only_invocation_processes(
    failure: str,
    spawn_error: OSError | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    openswe = _load_openswe()
    existing = subprocess.Popen(["sleep", "30"])
    started: list[subprocess.Popen[bytes]] = []
    wait_results = iter([failure != "backend_health", failure != "public_health"])

    def spawn(*_args: object) -> subprocess.Popen[bytes]:
        if spawn_error is not None and started:
            raise spawn_error
        process = subprocess.Popen(["sleep", "30"])
        started.append(process)
        return process

    monkeypatch.setattr(openswe, "backend_pids", lambda: [])
    monkeypatch.setattr(openswe, "tunnel_pids", lambda: [])
    monkeypatch.setattr(openswe, "ensure_docker_daemon", lambda: True)
    monkeypatch.setattr(openswe, "_spawn", spawn)
    monkeypatch.setattr(openswe, "_wait_for", lambda *_args: next(wait_results))

    try:
        assert openswe.start() == 1
        for process in started:
            _wait_for_exit(process)
        assert existing.poll() is None
        if spawn_error is not None:
            assert "FAIL: tunnel could not start" in capsys.readouterr().err
    finally:
        if existing.poll() is None:
            existing.terminate()
            _wait_for_exit(existing)


def test_failed_start_allows_immediate_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    openswe = _load_openswe()
    started: list[subprocess.Popen[bytes]] = []
    spawn_calls: list[list[str]] = []
    waits = iter([False, True, True])

    def spawn(cmd: list[str], _log_path: str) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(["sleep", "30"])
        started.append(process)
        spawn_calls.append(cmd)
        return process

    monkeypatch.setattr(openswe, "backend_pids", lambda: [])
    monkeypatch.setattr(openswe, "tunnel_pids", lambda: [])
    monkeypatch.setattr(openswe, "ensure_docker_daemon", lambda: True)
    monkeypatch.setattr(openswe, "_spawn", spawn)
    monkeypatch.setattr(openswe, "_wait_for", lambda *_args: next(waits))

    try:
        assert openswe.start() == 1
        assert openswe.start() == 0
        assert len(spawn_calls) == 3
        assert spawn_calls[1][0] == str(openswe.REPO / "speedbay" / "run-dev.sh")
        assert spawn_calls[2][0] == "cloudflared"
    finally:
        for process in started:
            if process.poll() is None:
                process.terminate()
        for process in started:
            _wait_for_exit(process)
