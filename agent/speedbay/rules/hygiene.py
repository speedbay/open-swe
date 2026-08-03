"""Commit/PR hygiene rules as pure functions (OPE-14).

The body contract is a verbatim translation of the warehouse harness's
``tools/pre-commit-hooks/hygiene-sections.mjs`` (harness path as of the fork)
(``validateHygieneSections`` and its three regexes) — same checks, same order,
same messages — so this fork and warehouse cannot drift into disagreeing about
what is compliant. Title, branch-name, and AI-attribution rules port the
PR-observable rules from ``.macroscope/check-run-agents/agent-hygiene.md``
(the live blocking CRA) and the warehouse harness's ``COMMIT-HYGIENE.md``.

Strings in, verdict values out. No git, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import attrs

from .findings import GateFinding, GateSeverity

# Provenance: hygiene-sections.mjs HYGIENE_SECTIONS (canonical order).
HYGIENE_SECTIONS = ("Why needed", "Solved / fixed", "Workflow enabled / fixed", "Verification")

# Provenance: hygiene-sections.mjs AUTO_CLOSE_HYGIENE_LINE — closing keywords
# targeting GitHub issue refs (#123 / owner/repo#123), which would auto-close
# a GitHub issue. Linear <TEAM>-NNN targets are intentionally not matched.
AUTO_CLOSE_HYGIENE_LINE = re.compile(
    r"^\s*(close[ds]?|fix(es|ed)?|resolve[ds]?)(:\s*|\s+)([\w.-]+/[\w.-]+#\d+|#\d+)", re.I
)

# Provenance: hygiene-sections.mjs NON_CLOSING_REF_LINE — Linear treats `refs`
# as a non-closing magic word that SUPPRESSES the on-merge status automation
# (verified empirically in FRG-592).
NON_CLOSING_REF_LINE = re.compile(r"^\s*refs?(:\s*|\s+)[A-Za-z]+-\d+", re.I)

# Provenance: hygiene-sections.mjs LINEAR_CLOSE_LINE — only the canonical
# `Closes <issueId>` line may use a closing keyword.
LINEAR_CLOSE_LINE = re.compile(
    r"^\s*(close[ds]?|fix(es|ed)?|resolve[ds]?)(:\s*|\s+)[A-Za-z]+-\d+", re.I
)

# Provenance: agent-hygiene.md rule 1 — `^[A-Z]+-\d+:` then an imperative
# subject. Imperativeness is a semantic judgment left to the CRA/reviewers;
# this pure rule enforces the prefix format and a non-empty subject.
TITLE_LINE = re.compile(r"^([A-Z]+-\d+): \S")

# Provenance: agent-hygiene.md rule 5 / COMMIT-HYGIENE.md — attribution
# trailers and authorship claims, anchored to agent identities so legitimate
# subject matter ("Update OpenAI embedding config", "Generated with
# deterministic seed") passes. Same anchoring lesson as
# speedbay/githooks/commit-msg (OPE-28).
_AGENT_NAMES = (
    r"(open[ -]?swe|claude|anthropic|openai|chatgpt|gpt-?\d*|copilot|gemini|cursor|devin|aider)"
)
AI_ATTRIBUTION_LINE = re.compile(
    r"^\s*co-authored-by:\s*.*" + _AGENT_NAMES + r"|"
    r"generated (with|by)\s+\[?" + _AGENT_NAMES + r"|"
    r"made by \[?" + _AGENT_NAMES,
    re.I,
)


@attrs.define(frozen=True)
class Violation:
    """One named hygiene violation: a stable rule id plus a human message."""

    rule: str
    message: str


def check_title(title: str, issue_id: str) -> Violation | None:
    """Validate a PR title / commit headline: ``<TEAM>-NNN: <subject>``."""
    match = TITLE_LINE.match(title)
    if match is None:
        return Violation(
            "title-format",
            f"title must start with '{issue_id}: ' followed by an imperative subject; got {title!r}",
        )
    if match.group(1) != issue_id:
        return Violation(
            "title-issue-mismatch",
            f"title references {match.group(1)}, expected {issue_id}",
        )
    return None


def check_branch(branch: str, issue_id: str) -> Violation | None:
    """Validate the source branch carries the issue id: ``<team>-NNN-*``."""
    if re.match(rf"^{re.escape(issue_id.lower())}-[a-z0-9][a-z0-9-]*$", branch) is None:
        return Violation(
            "branch-name",
            f"branch must match '{issue_id.lower()}-<slug>'; got {branch!r}",
        )
    return None


def check_attribution(text: str) -> Violation | None:
    """Reject AI-attribution trailers/authorship claims anywhere in ``text``."""
    for line in text.splitlines():
        if AI_ATTRIBUTION_LINE.search(line):
            return Violation("ai-attribution", f"no AI attribution allowed; found {line.strip()!r}")
    return None


def check_body_sections(body: str, issue_id: str) -> Violation | None:
    """Validate the COMMIT-HYGIENE body structure.

    Verbatim translation of ``validateHygieneSections`` (hygiene-sections.mjs):
    identical checks in identical order with identical messages, returning the
    first violation or ``None`` when compliant. Each failure mode carries a
    distinct rule id.
    """
    lines = body.split("\n")
    stripped = [line.rstrip("\r") for line in lines]

    closes_line = next(
        (i for i, line in enumerate(stripped) if line.strip() == f"Closes {issue_id}"), -1
    )
    if closes_line == -1:
        return Violation("closes-line", f"body must contain 'Closes {issue_id}'")
    first_heading = next((i for i, line in enumerate(stripped) if line.startswith("## ")), -1)
    if first_heading != -1 and closes_line > first_heading:
        return Violation(
            "closes-position", f"'Closes {issue_id}' must appear before the first '## ' heading"
        )
    auto_close = next((line for line in stripped if AUTO_CLOSE_HYGIENE_LINE.match(line)), None)
    if auto_close is not None:
        return Violation(
            "github-auto-close",
            f"body must not contain GitHub auto-close line '{auto_close.strip()}'",
        )
    non_closing = next((line for line in stripped if NON_CLOSING_REF_LINE.match(line)), None)
    if non_closing is not None:
        return Violation(
            "non-closing-ref",
            f"body must not contain non-closing ref line '{non_closing.strip()}' "
            f"(Linear 'refs' suppresses the on-merge automation; use 'Closes {issue_id}')",
        )
    stray = next(
        (
            line
            for line in stripped
            if LINEAR_CLOSE_LINE.match(line) and line.strip() != f"Closes {issue_id}"
        ),
        None,
    )
    if stray is not None:
        return Violation(
            "stray-close",
            f"only 'Closes {issue_id}' may use a closing keyword; found '{stray.strip()}'",
        )

    heading_indexes = {
        heading: [i for i, line in enumerate(stripped) if line == f"## {heading}"]
        for heading in HYGIENE_SECTIONS
    }
    for heading, indexes in heading_indexes.items():
        if not indexes:
            return Violation("missing-section", f"missing '## {heading}' section")
        if len(indexes) > 1:
            return Violation("duplicate-section", f"duplicate '## {heading}' section")
    firsts = [heading_indexes[heading][0] for heading in HYGIENE_SECTIONS]
    if any(later <= earlier for earlier, later in zip(firsts, firsts[1:], strict=False)):
        return Violation("section-order", "hygiene sections must appear in COMMIT-HYGIENE order")

    for heading in HYGIENE_SECTIONS:
        index = heading_indexes[heading][0]
        next_heading = next(
            (i for i, line in enumerate(stripped) if i > index and line.startswith("## ")),
            len(stripped),
        )
        if not "\n".join(stripped[index + 1 : next_heading]).strip():
            return Violation("empty-section", f"'## {heading}' section is empty")
    return None


def check_hygiene(title: str, body: str, branch: str, issue_id: str) -> tuple[Violation, ...]:
    """Full hygiene verdict for one PR / commit: every violated rule, named.

    Collects the title, branch, attribution (title + body), and body-structure
    verdicts. Empty tuple means compliant.
    """
    checks = (
        check_title(title, issue_id),
        check_branch(branch, issue_id),
        check_attribution(f"{title}\n{body}"),
        check_body_sections(body, issue_id),
    )
    return tuple(v for v in checks if v is not None)


def hygiene_findings(violations: Iterable[Violation]) -> tuple[GateFinding, ...]:
    """Classify hygiene ``Violation``s as gate findings (OPE-75).

    Hygiene breaches are ``REMEDIABLE`` by declaration: title/body/branch
    fixes are mechanical, never touch the diff, and the required format is
    already in the system prompt — the agent retries in-run.
    """
    return tuple(
        GateFinding(
            domain="hygiene",
            rule=violation.rule,
            message=violation.message,
            severity=GateSeverity.REMEDIABLE,
        )
        for violation in violations
    )
