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


def test_sandbox_bakes_and_launch_checks_playwright_chromium() -> None:
    dockerfile = SANDBOX_DOCKERFILE.read_text()
    browser_path = "ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright"
    install = "npm install --global playwright@1.61.1"
    browser_install = "playwright install --with-deps chromium"
    smoke = "playwright screenshot --browser chromium about:blank /tmp/playwright-browser-smoke.png"
    screenshot_check = "test -s /tmp/playwright-browser-smoke.png"

    assert browser_path in dockerfile
    assert install in dockerfile
    assert browser_install in dockerfile
    assert smoke in dockerfile
    assert screenshot_check in dockerfile
    assert dockerfile.index(browser_path) < dockerfile.index(install)
    assert dockerfile.index(install) < dockerfile.index(browser_install) < dockerfile.index(smoke)
