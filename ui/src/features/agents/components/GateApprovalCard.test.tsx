/** @vitest-environment jsdom */

// SPEEDBAY org-layer test (OPE-75) — the gate approval card must stay visible
// on the first PR-standards violation, with issue/fingerprint/rules/diff-stat
// content and Approve/Reject controls, and without corrective-round wording.

import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

vi.mock("@/features/agents/lib/gateApproval", () => ({
  useGateApprovals: () => ({
    data: {
      threadId: "t-1",
      isOwner: true,
      approvals: [
        {
          fingerprint: "fp-1",
          status: "pending",
          issueId: "OPE-75",
          baseSha: "b".repeat(40),
          headSha: "h".repeat(40),
          failedRuleIds: ["atomicity"],
          diffStats: {
            rawLoc: 400,
            effectiveLoc: 400,
            productionFiles: 1,
            exceeded: ["effective LOC 400 exceeds the Track-A cap of 300"],
          },
          evidenceTail: "",
          approvalUrl: null,
          notified: true,
          requestedAt: null,
          decidedAt: null,
          decidedBy: null,
        },
      ],
    },
  }),
  useGateApprovalDecision: () => ({ isPending: false, mutateAsync: vi.fn() }),
}))

import { GateApprovalCard } from "./GateApprovalCard"

afterEach(() => cleanup())

it("shows the first-violation card without corrective-round wording", () => {
  render(<GateApprovalCard threadId="t-1" />)

  expect(screen.getByTestId("gate-approval-card")).toBeTruthy()
  expect(screen.getByText(/OPE-75/)).toBeTruthy()
  expect(screen.getByText(/fp-1/)).toBeTruthy()
  expect(screen.getByText("atomicity")).toBeTruthy()
  expect(screen.getByText(/raw LOC 400/)).toBeTruthy()
  expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy()
  expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy()

  expect(screen.queryByText(/corrective round/i)).toBeNull()
})
