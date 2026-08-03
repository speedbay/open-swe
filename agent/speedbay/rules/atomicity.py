"""Atomicity cap math as pure functions (OPE-14).

Ports the effective-LOC model from the warehouse harness's
``references/atomicity/RULES.md`` § 2 and the category weights from its
``extensions/atomicity-guardrails.ts`` (``SCOPE_CATEGORY_WEIGHTS``;
harness paths as of the fork). Numstat-shaped rows plus a path classifier in,
verdict out. No git, no I/O.

Classification is conservative by design: an undeclared or ambiguous path
counts as production (full weight, file cap) per RULES.md — "Undeclared or
ambiguous paths are counted conservatively as production/runtime weight."
"""

from __future__ import annotations

import re

import attrs

from .findings import GateFinding, GateSeverity

# Provenance: warehouse harness extensions/atomicity-guardrails.ts
# SCOPE_CATEGORY_WEIGHTS. production/config/migration are full weight; tests
# reduced; documentation low; generated/lockfile/fixture/snapshot excluded.
SCOPE_CATEGORY_WEIGHTS: dict[str, float] = {
    "production": 1.0,
    "config": 1.0,
    "migration": 1.0,
    "test": 0.5,
    "documentation": 0.25,
    "generated": 0.0,
    "lockfile": 0.0,
    "fixture": 0.0,
    "snapshot": 0.0,
}

# Provenance: warehouse harness references/atomicity/RULES.md § 2, Track A.
TRACK_A_EFFECTIVE_LOC_CAP = 300
TRACK_A_PRODUCTION_FILE_CAP = 10

# Categories whose files count toward the production-file cap. Provenance:
# RULES.md § 2 — "every changed production/runtime/config/schema file counts
# toward the Track-A file cap at full weight"; test files are excluded.
_FILE_CAP_CATEGORIES = frozenset({"production", "config", "migration"})

_LOCKFILE_NAMES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "uv.lock",
        "poetry.lock",
        "Cargo.lock",
        "composer.lock",
        "Gemfile.lock",
        "go.sum",
    }
)

_DOC_SUFFIXES = (".md", ".rst")

_TEST_SUFFIXES = (".test.ts", ".test.tsx", ".test.js", ".spec.ts", "_test.py", "_test.go")


def classify_path(path: str) -> str:
    """Classify a repo-relative path into a ``SCOPE_CATEGORY_WEIGHTS`` category.

    Ambiguity resolves to ``production`` (conservative, full weight).
    """
    name = path.rsplit("/", 1)[-1]
    parts = path.split("/")
    if name in _LOCKFILE_NAMES:
        return "lockfile"
    if "__snapshots__" in parts or name.endswith(".snap"):
        return "snapshot"
    if ".generated." in name:
        return "generated"
    if "fixtures" in parts:
        return "fixture"
    if (
        "tests" in parts
        or "__tests__" in parts
        or name.startswith("test_")
        or name.endswith(_TEST_SUFFIXES)
    ):
        return "test"
    if "migrations" in parts:
        return "migration"
    if name.endswith(_DOC_SUFFIXES) or parts[0] in ("docs", "doc"):
        return "documentation"
    return "production"


@attrs.define(frozen=True)
class FileScope:
    """Per-file scope evidence: category, raw LOC, and weighted effective LOC."""

    path: str
    category: str
    raw_loc: int
    effective_loc: float


@attrs.define(frozen=True)
class AtomicityVerdict:
    """Track-A cap verdict with the evidence reviewers must be shown.

    ``exceeded`` names each violated cap (empty when ``passed``). Raw LOC is
    retained as audit evidence per RULES.md even though the numeric thresholds
    use effective LOC.
    """

    passed: bool
    raw_loc: int
    effective_loc: float
    production_files: int
    exceeded: tuple[str, ...]
    files: tuple[FileScope, ...]


@attrs.define(frozen=True)
class NumstatRow:
    """One ``git diff --numstat`` row: line counts plus the postimage path.

    Counts must be non-negative — a negative count would silently shrink
    ``raw_loc``/``effective_loc`` and could flip an over-cap verdict to pass.
    """

    added: int = attrs.field(validator=attrs.validators.ge(0))
    removed: int = attrs.field(validator=attrs.validators.ge(0))
    path: str


# Git's compact rename notation: ``dir/{old => new}/file``.
_RENAME_SEGMENT = re.compile(r"\{(.*?) => (.*?)\}")


def _postimage_path(path: str) -> str:
    """Resolve git's rename notation to the postimage (destination) path.

    ``git diff --numstat`` with rename detection emits either the compact
    ``dir/{old => new}/file`` form or the full ``old/path => new/path`` form;
    classification must see the destination path, not the brace expression.
    An empty side (``dir/{sub => }/file``) leaves a doubled slash, collapsed
    afterwards.
    """
    if " => " not in path:
        return path
    if _RENAME_SEGMENT.search(path):
        expanded = _RENAME_SEGMENT.sub(lambda m: m.group(2), path)
        return re.sub("//+", "/", expanded).lstrip("/")
    return path.split(" => ", 1)[1]


def _count(field: str) -> int:
    """Parse one numstat count: a non-negative integer, or ``-`` (binary) as 0.

    Anything else is a malformed row and raises ``ValueError`` — the gate must
    fail closed rather than silently undercount an over-cap change set.
    """
    if field == "-":
        return 0
    if not field.isdigit():
        raise ValueError(f"malformed numstat count {field!r}")
    return int(field)


def parse_numstat(numstat: str) -> list[NumstatRow]:
    """Parse ``git diff --numstat`` output into ``NumstatRow`` rows.

    Binary files report ``-`` for both counts; they parse as 0 LOC (the file
    still counts toward the file cap through its category). Rename rows are
    resolved to their postimage path. A row with any other non-integer count
    raises ``ValueError``. Pure string parsing — the caller runs git.
    """
    rows: list[NumstatRow] = []
    for line in numstat.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3 or not fields[2]:
            continue
        added, removed, path = fields
        rows.append(
            NumstatRow(
                added=_count(added),
                removed=_count(removed),
                path=_postimage_path(path),
            )
        )
    return rows


def check_atomicity(
    rows: list[NumstatRow],
    *,
    effective_loc_cap: int = TRACK_A_EFFECTIVE_LOC_CAP,
    production_file_cap: int = TRACK_A_PRODUCTION_FILE_CAP,
) -> AtomicityVerdict:
    """Apply Track-A cap math to ``NumstatRow`` rows.

    Effective LOC = raw LOC x category weight, summed across files;
    production-file count is a separate full-weight cap (RULES.md § 2). Caps
    are inclusive: exactly at the cap passes, one over fails.
    """
    files: list[FileScope] = []
    for row in rows:
        category = classify_path(row.path)
        raw = row.added + row.removed
        files.append(
            FileScope(
                path=row.path,
                category=category,
                raw_loc=raw,
                effective_loc=raw * SCOPE_CATEGORY_WEIGHTS[category],
            )
        )
    raw_loc = sum(f.raw_loc for f in files)
    effective_loc = sum(f.effective_loc for f in files)
    production_files = sum(1 for f in files if f.category in _FILE_CAP_CATEGORIES)

    exceeded: list[str] = []
    if effective_loc > effective_loc_cap:
        exceeded.append(
            f"effective LOC {effective_loc:g} exceeds the Track-A cap of {effective_loc_cap}"
        )
    if production_files > production_file_cap:
        exceeded.append(
            f"production-file count {production_files} exceeds the Track-A cap "
            f"of {production_file_cap}"
        )
    return AtomicityVerdict(
        passed=not exceeded,
        raw_loc=raw_loc,
        effective_loc=effective_loc,
        production_files=production_files,
        exceeded=tuple(exceeded),
        files=tuple(files),
    )


def atomicity_findings(verdict: AtomicityVerdict) -> tuple[GateFinding, ...]:
    """Classify an ``AtomicityVerdict`` as gate findings (OPE-75).

    Atomicity breaches are ``HARD`` by declaration: re-partitioning
    implemented work is a human decision, never an in-run remediation. One
    finding per exceeded cap; a passing verdict yields no findings.
    """
    return tuple(
        GateFinding(
            domain="atomicity",
            rule="atomicity",
            message=reason,
            severity=GateSeverity.HARD,
        )
        for reason in verdict.exceeded
    )
