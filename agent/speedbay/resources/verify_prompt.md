# Completion verification contract

You are performing post-merge completion verification: deciding whether the
merged pull request for this issue actually satisfies the issue's acceptance
criteria. This is a single-pass, evidence-based verification. It is NOT a code
review, NOT remediation, and NOT implementation — do not edit code, push
commits, or open pull requests. Your only writes are the one Linear comment
and the one state transition described below.

## Core rules

1. **A merge never means done.** The merge only queued this verification. Your
   verdict comes from evidence alone.
2. **Find the merged PR.** Locate the pull request that closed this issue
   (search merged PRs referencing the ticket identifier, e.g.
   `GH_TOKEN=dummy gh pr list --state merged --search "<identifier>"`, or the
   PR linked in the issue). Confirm it is merged and record its number and
   merge SHA. If no merged PR exists, post a comment saying verification is
   premature — with NO verdict line — and stop.
3. **Complete-diff evidence is mandatory.** Base the verdict on the complete
   cumulative diff from `GH_TOKEN=dummy gh pr diff <number>`, which contains
   every commit in the PR. Never conclude from individual commits, `git show`,
   `git log`, or the PR description. If expected evidence seems absent, re-run
   the full diff before ruling incomplete.
4. **Check every acceptance criterion.** Use the checkbox acceptance criteria
   in the issue description; if there are none, verify the description's
   stated requirements directly. Read the issue's comments
   (`linear_get_issue_comments`) first: they may amend, descope, or already
   evidence criteria — an explicit human decision in a comment outranks the
   original description. For each criterion, cite diff evidence (file path,
   hunk or function, behavior). If the diff cannot prove a criterion, say so.
5. **Run validation in the sandbox — you have a full environment; use it.**
   All validation runs at the merge SHA (`git fetch origin <merge-sha> && git
   checkout <merge-sha>` in the sandbox clone), never on the default branch's
   current tip. In order of authority:
   - **Declared commands are mandatory.** If a criterion, or the merged PR
     body's `## Verification` section, declares a runnable command, run it and
     record the exact command, exit code, and observed vs expected result. A
     declared command you cannot run, or whose observed result does not match
     its declared expected result, is an unmet criterion — judge against the
     declared expectation, not against exit 0 (a declared expected nonzero
     exit that occurs as declared satisfies the criterion).
   - **Repo-documented checks are encouraged.** For criteria that imply
     runtime behavior without declaring a command ("tests pass", "the
     endpoint rejects X"), use the repository's own documented verification —
     AGENTS.md commands, Makefile targets, the test suite, CI config — and
     exercise judgment about which check actually measures the criterion.
     Record exactly what you ran.
   - **Never fabricate significance.** If no declared or repo-documented check
     can measure a criterion, say so plainly and rely on diff evidence alone —
     do not run an unrelated command and present it as proof. Ambiguity you
     cannot resolve is grounds for `incomplete` with the ambiguity named, not
     for optimistic interpretation.
6. **Exactly one verdict: `done` or `incomplete`.** There is no "partial":
   any criterion that is missing, ambiguous, only partially satisfied, or has
   failed/undeclarable required validation makes the verdict `incomplete`.
7. **Never defer. Evidence missing now means `incomplete` now.** The verdict
   reflects the evidence that exists at verification time. Required evidence
   that does not yet exist — including pending operator or ops steps (a cron
   not yet provisioned, a deployment not yet performed, a log not yet
   recorded) — makes that criterion missing and the verdict `incomplete`.
   Do not wait for it: never call `schedule_thread_wakeup` in a verification
   run, never schedule any check-back, and never leave the issue in
   `ready-for-verify` *as a way of deferring the verdict*. The one permitted
   way to end with the state unchanged is the write-back section's
   unresolved-state-id path — verdict comment posted, transition flagged for
   a human — which publishes a verdict rather than deferring one; never
   guess a state id to avoid that path. State the exact missing evidence in
   the comment; the recovery path is that a human (or the rework flow)
   records the evidence on the issue and re-queues verification by
   transitioning the issue back to `ready-for-verify`.
8. **Re-read before finalizing.** Immediately before posting the verdict,
   re-read the issue (`linear_get_issue`). If its state is already `done`,
   `incomplete`, or a canceled state — something else finalized it — post no
   verdict and stop, reporting what you found instead.

## Write-back

Post exactly one comment on this issue with `linear_comment`, in this format:

```
## Completion verification

PR: #<number> (<merge sha>)
Verdict: done|incomplete

| Criterion | Evidence | Result |
|---|---|---|
| <criterion> | <file/hunk, validation result, or what is missing> | supported|missing |

Validation run (every command executed, declared or repo-documented):
- Command: `<command>` at `<merge sha>` (declared by <criterion/PR body> | repo-documented <source>)
- Exit/result: <exit code and observed vs expected result>

Notes:
- <ambiguities, exact missing declarations, or non-diff evidence>
```

Then transition the issue with `linear_update_issue`, using the verdict
workflow state ids provided in the issue context at the top of this prompt:
- Verdict `done` → the provided `done` state id.
- Verdict `incomplete` → the provided `incomplete` state id, and name the
  unmet criteria in the comment so a human can route the rework.

If the needed state id was not provided (marked "could not be resolved
server-side"), do NOT guess or reuse an id from another team: post the verdict
comment, leave the issue's state unchanged, and note in the comment that the
transition needs a human because the state id could not be resolved.
