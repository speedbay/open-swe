"""Parse-level tests for the Speed Bay sandbox image."""

from pathlib import Path

SANDBOX_DOCKERFILE = (
    Path(__file__).resolve().parents[2] / "speedbay" / "docker" / "Dockerfile.sandbox"
)


def test_sandbox_installs_node_22_from_nodesource() -> None:
    dockerfile = SANDBOX_DOCKERFILE.read_text()
    base_apt_block = dockerfile.split("# GitHub CLI", maxsplit=1)[0]
    base_packages = {line.strip(" \\") for line in base_apt_block.splitlines()}
    nodesource_repo = "https://deb.nodesource.com/node_22.x nodistro main"
    nodesource_install = "apt-get install -y --no-install-recommends nodejs"

    assert {"nodejs", "npm"}.isdisjoint(base_packages)
    assert nodesource_repo in dockerfile
    assert dockerfile.index(nodesource_repo) < dockerfile.index(nodesource_install)
