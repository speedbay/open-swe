"""One home for the Speed Bay org layer's tunable settings (OPE-31).

SPEEDBAY org-layer file — upstream does not own it. Every operational knob an
operator may want to change (timeouts, resource limits, endpoints) is declared
here and only here; the owning modules import from this file. Env-overridable
knobs are resolved through accessor functions at call time — not at import —
so a changed environment (tests, live re-tuning) takes effect without a
process restart.

Not configuration, and deliberately not here: PR-standards rule content
(``agent/speedbay/rules/`` — policy, changing it is a standards change), the
``PROJECT_QUALITY_GATES`` command map (ported from warehouse workflow.md with
its own re-sync contract, co-located with its runner), module wiring constants
(docker labels, container paths, exit-code conventions), and secrets
(``LINEAR_API_KEY`` and the GitHub App variables stay env-only in ``.env``).
"""

from __future__ import annotations

import os

# --- Docker sandbox (docker_sandbox.py) --------------------------------------

IMAGE_ENV = "DOCKER_SANDBOX_IMAGE"
DEFAULT_IMAGE = "openswe-sandbox:dev"
TTL_ENV = "DOCKER_SANDBOX_TTL_SECONDS"
DEFAULT_TTL_SECONDS = 24 * 3600  # container lifetime before the lazy sweep removes it
MEMORY_ENV = "DOCKER_SANDBOX_MEMORY"
DEFAULT_MEMORY = "4g"  # docker --memory limit per sandbox container
DEFAULT_EXECUTE_TIMEOUT = 300  # seconds; in-container timeout(1) per executed command
# Installation tokens live one hour; refresh with headroom so a long agent run
# never hands git/gh an expired token.
TOKEN_REFRESH_SECONDS = 50 * 60


def sandbox_image() -> str:
    """Sandbox image to boot; ``DOCKER_SANDBOX_IMAGE`` overrides the default."""
    return os.getenv(IMAGE_ENV, DEFAULT_IMAGE)


def sandbox_ttl_seconds() -> int:
    """Container TTL; ``DOCKER_SANDBOX_TTL_SECONDS`` overrides the default."""
    return int(os.getenv(TTL_ENV, str(DEFAULT_TTL_SECONDS)))


def sandbox_memory() -> str:
    """Container memory limit; ``DOCKER_SANDBOX_MEMORY`` overrides the default."""
    return os.getenv(MEMORY_ENV, DEFAULT_MEMORY)


# --- Verify sweep (verify_sweep.py) ------------------------------------------

VERIFY_SWEEP_MIN_AGE_ENV = "OPENSWE_VERIFY_SWEEP_MIN_AGE_SECONDS"
# One hour: long enough that a webhook-dispatched verify run has finished, so
# the sweep never races the fast path it backs up.
DEFAULT_VERIFY_SWEEP_MIN_AGE_SECONDS = 3600


def verify_sweep_min_age_seconds() -> int:
    """Age an issue must sit in ready-for-verify before the sweep re-dispatches."""
    value = int(os.getenv(VERIFY_SWEEP_MIN_AGE_ENV, str(DEFAULT_VERIFY_SWEEP_MIN_AGE_SECONDS)))
    if value < 0:
        raise ValueError(f"{VERIFY_SWEEP_MIN_AGE_ENV} must be non-negative, got {value}")
    return value


# --- PR gates (quality_gates.py, pr_standards.py) -----------------------------

WORKSPACE = "/workspace"  # sandbox checkout root both gates diff against
COMMAND_TIMEOUT_SECONDS = 15 * 60  # per quality-gate command
DIFF_TIMEOUT_SECONDS = 120  # git diff inside the sandbox
OUTPUT_TAIL_CHARS = 2000  # command-output tail kept as failure evidence
MAX_CORRECTIVE_ROUNDS = 3  # PR-standards blocks per thread before escalation (OPE-10)

# --- Linear (linear_guard.py) --------------------------------------------------

LINEAR_GQL_URL = "https://api.linear.app/graphql"
TRIGGER_OWNER_ENV = "OPENSWE_TRIGGER_OWNER_EMAILS"


def trigger_owner_emails() -> frozenset[str]:
    """Emails whose Linear comments this instance acts on (OPE-36).

    Comma-separated, case-insensitive. Empty/unset means unscoped — the
    single-instance deployment accepts every human mention. Multi-laptop
    deployments set one owner per instance so a shared webhook fan-out
    triggers exactly one backend.
    """
    raw = os.getenv(TRIGGER_OWNER_ENV, "")
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())
