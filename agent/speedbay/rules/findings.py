"""Gate-finding classification for the PR-standards gate (OPE-75).

SPEEDBAY org-layer file — upstream does not own it. One small abstraction
separating two questions the gate used to conflate:

- **Severity** (here): intrinsic to the rule — *who can fix this in
  principle?* Atomicity breaches need a human decision; hygiene breaches are
  agent-correctable metadata fixes. Severity is declared where the rule
  lives and never mutates at runtime.
- **Disposition** (``pr_standards.py``): middleware policy — *what happens
  to this run?* A remediable finding can still end a run once its
  remediation budget is exhausted, without ever being reclassified.

Rule modules own their classification: ``rules/atomicity.py`` emits ``HARD``
findings, ``rules/hygiene.py`` emits ``REMEDIABLE`` findings. A future rule
picks its severity at definition time; the middleware only partitions.
"""

from __future__ import annotations

from enum import Enum

import attrs


class GateSeverity(Enum):
    """Who can fix a gate finding in principle.

    ``HARD``: a human decision point — re-partitioning implemented work is
    judgment-laden (the OPE-68 amend-loop risk); the run halts for dashboard
    approve/reject on the first violation.

    ``REMEDIABLE``: agent-correctable in-run — mechanical title/body/branch
    fixes that never touch the diff; the agent retries within a bounded
    budget before escalating.
    """

    HARD = "hard"
    REMEDIABLE = "remediable"


@attrs.frozen
class GateFinding:
    """One named gate violation with its intrinsic classification.

    ``domain`` is the rule family (``"atomicity"`` / ``"hygiene"``), ``rule``
    the stable per-rule id (e.g. ``"title-format"``), ``message`` the
    evidence shown to the agent and the human, and ``severity`` the rule's
    declared classification (see ``GateSeverity``).
    """

    domain: str
    rule: str
    message: str
    severity: GateSeverity
