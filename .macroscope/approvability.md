---
# Macroscope Approvability config — the fork's blocking-correctness gate (OPE-29).
#
# Docs checked: 2026-07-28.
# - Approvability + custom eligibility rules (additive to built-ins):
#   https://docs.macroscope.com/approvability
# - Required-status-check caveats:
#   https://docs.macroscope.com/approvability#using-approvability-as-a-required-status-check
#
# WHY conclusion: failure — the built-in Correctness check ALWAYS concludes
# neutral, even for HIGH findings (verified live on PR #8), so requiring it in
# the branch ruleset cannot block a merge. Approvability is the layer that
# aggregates correctness severity into a verdict; failing it is Macroscope's
# supported way to make blocking-severity correctness findings actually block.
# This supersedes OPE-26's "approvability deliberately omitted" decision.
#
# Documented caveat: a failing required Approvability check cannot be cleared
# by human approval alone — fix the findings and re-review, or use the repo
# ruleset's admin bypass (the deliberate escape hatch).
tools:
  - github_api_read_only
conclusion: failure
waitsFor: ["*"]
waitsForTimeout: 30
---

# open-swe fork Approvability eligibility

These rules are **additive** to Macroscope's built-in eligibility (which
already withholds auto-approval for large refactors, schema changes,
security/auth code, and breaking changes). They make the fork's sensitive
surfaces explicit: if a PR changes files under any of the following paths,
DO NOT auto-approve — route it to human review.

## Review and CI policy

- `.macroscope/` — controls how Macroscope reviews this repo
- `.github/workflows/` — CI policy and required-check surface

## Credential handling and git identity

- `speedbay/mint_token.py`, `speedbay/bin/`, `speedbay/gitconfig`,
  `speedbay/githooks/` — GitHub App token minting, credential helpers,
  commit-identity hooks
- Any path containing `.env`, secrets, credentials, or tokens

## Loop prevention and sandbox isolation

- `agent/speedbay/linear_guard.py` — the self-trigger guard; a regression
  re-opens the run-spawning loop (OPE-23)
- `agent/speedbay/docker_sandbox.py` and `speedbay/docker/` — the container
  isolation boundary between agent commands and the host (OPE-7)

## Upstream deviation touchpoints (FORK.md § Upstream deviations)

- `agent/server.py`, `agent/utils/sandbox.py`, `agent/webhooks/linear_routes.py`,
  `agent/dashboard/options.py`, `agent/utils/linear_team_repo_map.py` — the
  fork's entire upstream merge surface; changes here alter it

## Scope boundary

These instructions cover Approvability eligibility only. Lint, format, type
and test gates are Speedbay CI's job; commit/PR-metadata rules are the
agent-hygiene check's job. Both are observed through the `waitsFor: ["*"]`
dependency above, so this verdict lands after every other check has reported.
