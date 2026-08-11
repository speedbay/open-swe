"""Tests for the ``rules`` pure functions (OPE-14).

Fixture set mirrors the source implementations: cap math and classification
against the warehouse harness's ``references/atomicity/RULES.md`` § 2, and the body
contract against ``validateHygieneSections`` (hygiene-sections.mjs) — one
passing and one failing case per rule, each failure naming its rule.

Run:  .venv/bin/python -m pytest tests/speedbay/test_rules.py -q
"""

from __future__ import annotations

import pytest

from agent.speedbay.rules.atomicity import (
    TRACK_A_EFFECTIVE_LOC_CAP,
    TRACK_A_PRODUCTION_FILE_CAP,
    NumstatRow,
    check_atomicity,
    classify_path,
    parse_numstat,
)
from agent.speedbay.rules.hygiene import (
    Violation,
    check_attribution,
    check_body_sections,
    check_branch,
    check_commit_message,
    check_hygiene,
    check_title,
)

ISSUE = "OPE-14"


def _rule(violation: Violation | None) -> str:
    """Assert a violation was returned and unwrap its rule id."""
    assert violation is not None
    return violation.rule


def _body(**overrides: str) -> str:
    """A compliant COMMIT-HYGIENE body; keyword args replace one section."""
    sections = {
        "Why needed": "The gate needs rules.",
        "Solved / fixed": "- Ported the cap math.",
        "Workflow enabled / fixed": "Agents get deterministic verdicts.",
        "Verification": "- `pytest tests/speedbay -q`",
    }
    sections.update(overrides)
    parts = [f"Closes {ISSUE}", ""]
    for heading, content in sections.items():
        parts += [f"## {heading}", content, ""]
    return "\n".join(parts)


# --- atomicity: classification -------------------------------------------------


def test_classify_path_categories() -> None:
    assert classify_path("agent/speedbay/pr_standards.py") == "production"
    assert classify_path("pyproject.toml") == "production"  # ambiguous -> conservative
    assert classify_path("tests/speedbay/test_rules.py") == "test"
    assert classify_path("ui/src/__tests__/app.test.ts") == "test"
    assert classify_path("backend/util_test.py") == "test"
    assert classify_path("FORK.md") == "documentation"
    assert classify_path("docs/setup.rst") == "documentation"
    assert classify_path("backend/migrations/0002_add_index.py") == "migration"
    assert classify_path("uv.lock") == "lockfile"
    assert classify_path("ui/__snapshots__/app.snap") == "snapshot"
    assert classify_path("api/client.generated.ts") == "generated"
    assert classify_path("tests/fixtures/big.json") == "fixture"


# --- atomicity: cap math -------------------------------------------------------


def test_effective_loc_weights_per_category() -> None:
    verdict = check_atomicity(
        [
            NumstatRow(100, 0, "agent/api.py"),  # production x1.0 = 100
            NumstatRow(100, 0, "tests/test_api.py"),  # test x0.5 = 50
            NumstatRow(100, 0, "README.md"),  # documentation x0.25 = 25
            NumstatRow(100, 0, "uv.lock"),  # lockfile x0 = 0
        ]
    )
    assert verdict.raw_loc == 400
    assert verdict.effective_loc == 175
    assert verdict.production_files == 1
    assert verdict.passed


def test_effective_loc_cap_boundary() -> None:
    at_cap = check_atomicity([NumstatRow(TRACK_A_EFFECTIVE_LOC_CAP, 0, "agent/api.py")])
    assert at_cap.passed
    over = check_atomicity([NumstatRow(TRACK_A_EFFECTIVE_LOC_CAP + 1, 0, "agent/api.py")])
    assert not over.passed
    assert any("effective LOC" in reason for reason in over.exceeded)


def test_production_file_cap_boundary_and_test_exclusion() -> None:
    rows = [NumstatRow(1, 0, f"agent/mod_{i}.py") for i in range(TRACK_A_PRODUCTION_FILE_CAP)]
    rows += [NumstatRow(1, 0, f"tests/test_mod_{i}.py") for i in range(20)]  # excluded
    assert check_atomicity(rows).passed
    rows.append(NumstatRow(1, 0, "agent/one_too_many.py"))
    over = check_atomicity(rows)
    assert not over.passed
    assert any("production-file count" in reason for reason in over.exceeded)


def test_parse_numstat_including_binary() -> None:
    rows = parse_numstat("10\t2\tagent/api.py\n-\t-\tassets/logo.png\n\nnot a row")
    assert rows == [NumstatRow(10, 2, "agent/api.py"), NumstatRow(0, 0, "assets/logo.png")]
    verdict = check_atomicity(rows)
    assert verdict.production_files == 2  # binary file still counts toward the file cap


def test_parse_numstat_rejects_malformed_counts_and_negative_rows() -> None:
    with pytest.raises(ValueError, match="malformed numstat count 'x'"):
        parse_numstat("x\t200\tagent/api.py")
    with pytest.raises(ValueError, match="malformed numstat count '-3'"):
        parse_numstat("-3\t0\tagent/api.py")
    with pytest.raises(ValueError):
        NumstatRow(-1, 0, "agent/api.py")
    with pytest.raises(ValueError):
        NumstatRow(0, -1, "agent/api.py")


def test_parse_numstat_resolves_rename_notation_to_postimage() -> None:
    rows = parse_numstat(
        "5\t5\tsrc/{foo.py => foo.test.ts}\n"
        "1\t0\t{src => tests}/foo.py\n"
        "2\t0\told/name.py => tests/name.py\n"
        "3\t0\tdir/{sub => }/file.py\n"
    )
    assert [row.path for row in rows] == [
        "src/foo.test.ts",
        "tests/foo.py",
        "tests/name.py",
        "dir/file.py",
    ]
    # Postimage classification: the three renamed-to-test files weigh 0.5 and
    # stay out of the production-file cap instead of inflating it.
    verdict = check_atomicity(rows)
    assert verdict.production_files == 1  # dir/file.py only
    assert verdict.effective_loc == (5 + 5) * 0.5 + 1 * 0.5 + 2 * 0.5 + 3 * 1.0


# --- hygiene: title / branch / attribution --------------------------------------


def test_title_pass_and_fail() -> None:
    assert check_title("OPE-14: port the cap math", ISSUE) is None
    assert _rule(check_title("fix: port the cap math", ISSUE)) == "title-format"
    assert _rule(check_title("OPE-14:", ISSUE)) == "title-format"  # empty subject
    assert _rule(check_title("OPE-99: wrong issue", ISSUE)) == "title-issue-mismatch"


def test_branch_pass_and_fail() -> None:
    assert check_branch("ope-14-port-rules", ISSUE) is None
    assert _rule(check_branch("feature/port-rules", ISSUE)) == "branch-name"
    assert _rule(check_branch("OPE-14-port-rules", ISSUE)) == "branch-name"  # must be lowercase


def test_attribution_flags_claims_not_product_names() -> None:
    assert _rule(check_attribution("Co-authored-by: open-swe[bot] <x@y>")) == "ai-attribution"
    assert _rule(check_attribution("Generated with [Open SWE](https://x)")) == "ai-attribution"
    assert _rule(check_attribution("Made by [Open SWE]")) == "ai-attribution"
    assert _rule(check_attribution("Generated by Claude Code")) == "ai-attribution"
    # Legitimate subject matter must pass (agent-hygiene.md rule 5).
    assert check_attribution("Update OpenAI embedding config") is None
    assert check_attribution("Generated with deterministic seed") is None


# --- hygiene: body contract (one failing case per rule) --------------------------


def test_body_compliant() -> None:
    assert check_body_sections(_body(), ISSUE) is None


def test_body_missing_closes() -> None:
    body = _body().replace(f"Closes {ISSUE}", "")
    assert _rule(check_body_sections(body, ISSUE)) == "closes-line"


def test_body_closes_after_first_heading() -> None:
    body = _body().replace(f"Closes {ISSUE}\n", "")
    body += f"\nCloses {ISSUE}\n"
    assert _rule(check_body_sections(body, ISSUE)) == "closes-position"


def test_body_github_auto_close_rejected() -> None:
    assert _rule(check_body_sections(_body() + "\nFixes #123\n", ISSUE)) == "github-auto-close"
    assert (
        _rule(check_body_sections(_body() + "\nCloses speedbay/open-swe#7\n", ISSUE))
        == "github-auto-close"
    )


def test_body_non_closing_ref_rejected() -> None:
    assert _rule(check_body_sections(_body() + "\nRefs: OPE-1\n", ISSUE)) == "non-closing-ref"


def test_body_stray_close_rejected() -> None:
    assert _rule(check_body_sections(_body() + "\nResolves OPE-99\n", ISSUE)) == "stray-close"


def test_body_missing_duplicate_empty_and_order() -> None:
    missing = _body().replace("## Verification", "## Verified by")
    assert _rule(check_body_sections(missing, ISSUE)) == "missing-section"

    duplicate = _body() + "\n## Verification\nagain\n"
    assert _rule(check_body_sections(duplicate, ISSUE)) == "duplicate-section"

    empty = _body(**{"Why needed": ""})
    assert _rule(check_body_sections(empty, ISSUE)) == "empty-section"

    swapped = _body().replace("## Why needed\nThe gate needs rules.\n", "")
    swapped += "\n## Why needed\nThe gate needs rules.\n"
    assert _rule(check_body_sections(swapped, ISSUE)) == "section-order"


def test_commit_message_contract_and_generated_subject_exemptions() -> None:
    assert check_commit_message(f"{ISSUE}: port rules\n\n{_body()}", ISSUE) == ()
    assert _rule(check_commit_message(f"Closes {ISSUE}\n\n{_body()}", ISSUE)[0]) == "title-format"
    attribution = check_commit_message(
        f"{ISSUE}: port rules\n\n{_body()}\nCo-authored-by: Claude <x@y>", ISSUE
    )
    assert [violation.rule for violation in attribution] == ["ai-attribution"]
    assert check_commit_message("Merge branch 'main' into feature", ISSUE) == ()
    assert check_commit_message(f'Revert "{ISSUE}: port rules"', ISSUE) == ()


def test_check_hygiene_collects_named_violations() -> None:
    violations = check_hygiene(
        title="update stuff",
        body="Closes #5",
        branch="my-branch",
        issue_id=ISSUE,
    )
    assert {v.rule for v in violations} == {
        "title-format",
        "branch-name",
        "closes-line",
    }
    assert check_hygiene("OPE-14: port rules", _body(), "ope-14-port-rules", ISSUE) == ()
