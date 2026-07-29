"""Atomicity cap math as pure functions (OPE-14).

Ports the effective-LOC model from warehouse
``pi-forge/forge/references/atomicity/RULES.md`` § 2 and the category weights
from ``pi-forge/forge/extensions/atomicity-guardrails.ts``
(``SCOPE_CATEGORY_WEIGHTS``). Numstat-shaped rows plus a path classifier in,
verdict out. No git, no I/O.

Classification is conservative by design: an undeclared or ambiguous path
counts as production (full weight, file cap) per RULES.md — "Undeclared or
ambiguous paths are counted conservatively as production/runtime weight."
"""

from __future__ import annotations

from dataclasses import dataclass

# Provenance: pi-forge/forge/extensions/atomicity-guardrails.ts
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

# Provenance: pi-forge/forge/references/atomicity/RULES.md § 2, Track A.
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


@dataclass(frozen=True)
class FileScope:
    """Per-file scope evidence: category, raw LOC, and weighted effective LOC."""

    path: str
    category: str
    raw_loc: int
    effective_loc: float


@dataclass(frozen=True)
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


def parse_numstat(numstat: str) -> list[tuple[int, int, str]]:
    """Parse ``git diff --numstat`` output into ``(added, removed, path)`` rows.

    Binary files report ``-`` for both counts; they parse as 0 LOC (the file
    still counts toward the file cap through its category). Pure string
    parsing — the caller runs git.
    """
    rows: list[tuple[int, int, str]] = []
    for line in numstat.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3 or not fields[2]:
            continue
        added, removed, path = fields
        rows.append(
            (
                int(added) if added.isdigit() else 0,
                int(removed) if removed.isdigit() else 0,
                path,
            )
        )
    return rows


def check_atomicity(
    rows: list[tuple[int, int, str]],
    *,
    effective_loc_cap: int = TRACK_A_EFFECTIVE_LOC_CAP,
    production_file_cap: int = TRACK_A_PRODUCTION_FILE_CAP,
) -> AtomicityVerdict:
    """Apply Track-A cap math to ``(added, removed, path)`` numstat rows.

    Effective LOC = raw LOC x category weight, summed across files;
    production-file count is a separate full-weight cap (RULES.md § 2). Caps
    are inclusive: exactly at the cap passes, one over fails.
    """
    files: list[FileScope] = []
    for added, removed, path in rows:
        category = classify_path(path)
        raw = added + removed
        files.append(
            FileScope(
                path=path,
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
