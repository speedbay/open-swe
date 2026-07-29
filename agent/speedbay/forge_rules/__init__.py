"""Speed Bay PR-standards rule content: atomicity cap math and commit hygiene.

SPEEDBAY ORG-LAYER PACKAGE (FORK.md § Speed Bay org layer). Pure functions
only — no git, no subprocess, no network, no I/O: rows/strings in, verdict
values out. The gate middleware (OPE-8) will consume this module; this
package deliberately contains no wiring and is not yet imported by any
non-test code.

Rule sources (values copied with provenance comments; the fork never reads
warehouse files at runtime):

* ``atomicity``  — warehouse ``pi-forge/forge/references/atomicity/RULES.md``
  and ``pi-forge/forge/extensions/atomicity-guardrails.ts``.
* ``hygiene``    — warehouse ``pi-forge/forge/tools/pre-commit-hooks/
  hygiene-sections.mjs`` (``validateHygieneSections``, translated verbatim)
  plus the PR-observable rules in ``.macroscope/check-run-agents/
  agent-hygiene.md`` (the live blocking CRA). The prompt-side contract in
  ``agent/speedbay/conventions.py`` teaches the same rules; keep them in sync.
"""
