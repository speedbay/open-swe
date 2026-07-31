"""Unit tests for the speedbay/githooks/commit-msg hook (OPE-58).

Runs the hook script directly via subprocess on temp message files — no docker,
no git repo needed: the hook's contract is (msg-file in, exit code + rewritten
file out). The in-container registration path is covered separately by
tests/speedbay/test_docker_sandbox.py::test_commit_msg_hook_strips_attribution.
"""

import pathlib
import subprocess

HOOK = pathlib.Path(__file__).resolve().parents[2] / "speedbay" / "githooks" / "commit-msg"


def _run(tmp_path: pathlib.Path, message: str) -> tuple[subprocess.CompletedProcess[str], str]:
    msg_file = tmp_path / "COMMIT_EDITMSG"
    msg_file.write_text(message)
    proc = subprocess.run(["sh", str(HOOK), str(msg_file)], capture_output=True, text=True)
    return proc, msg_file.read_text()


def test_missing_closes_line_rejected_with_guidance(tmp_path: pathlib.Path) -> None:
    proc, _ = _run(tmp_path, "OPE-1: subject\n\nA substantive body with no closing line.\n")
    assert proc.returncode == 1
    assert "Closes <TEAM>-NNN" in proc.stderr


def test_closes_line_accepted(tmp_path: pathlib.Path) -> None:
    proc, out = _run(tmp_path, "OPE-1: subject\n\nCloses OPE-1\n\n## Why needed\nBecause.\n")
    assert proc.returncode == 0
    assert "Closes OPE-1" in out


def test_prose_mention_of_closes_does_not_count(tmp_path: pathlib.Path) -> None:
    proc, _ = _run(tmp_path, "OPE-1: subject\n\nThis nearly Closes OPE-1 but mid-line.\n")
    assert proc.returncode == 1


def test_attribution_still_stripped_when_compliant(tmp_path: pathlib.Path) -> None:
    proc, out = _run(
        tmp_path,
        "OPE-1: subject\n\nCloses OPE-1\n\nCo-authored-by: open-swe[bot] <x@y>\n",
    )
    assert proc.returncode == 0
    assert "Co-authored-by" not in out
    assert "Closes OPE-1" in out
