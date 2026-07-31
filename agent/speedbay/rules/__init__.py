"""Speed Bay PR-standards rule content: atomicity cap math and commit hygiene.

SPEEDBAY ORG-LAYER PACKAGE (OPERATIONS.md § Speed Bay org layer). Pure functions
only — no git, no subprocess, no network, no I/O: rows/strings in, verdict
values out. The gate middleware (OPE-8, ``agent/speedbay/pr_standards.py``)
consumes this module; this package deliberately contains no wiring.

Rule sources (values copied with provenance comments; the fork never reads
warehouse files at runtime):

* ``atomicity``  — the warehouse harness's ``references/atomicity/RULES.md``
  and ``extensions/atomicity-guardrails.ts`` (harness paths as of the fork).
* ``hygiene``    — the warehouse harness's ``tools/pre-commit-hooks/
  hygiene-sections.mjs`` (``validateHygieneSections``, translated verbatim)
  plus the PR-observable rules in ``.macroscope/check-run-agents/
  agent-hygiene.md`` (the live blocking CRA). The prompt-side contract in
  ``agent/speedbay/conventions.py`` teaches the same rules; keep them in sync.
"""
