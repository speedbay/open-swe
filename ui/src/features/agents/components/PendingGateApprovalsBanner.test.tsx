/** @vitest-environment jsdom */

import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

const gateApprovalMock = vi.hoisted(() => ({
  approvals: [] as Array<Record<string, unknown>>,
}))

vi.mock("@/features/agents/lib/gateApproval", () => ({
  usePendingGateApprovals: () => ({
    data: { approvals: gateApprovalMock.approvals },
  }),
}))

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children }: { children: React.ReactNode }) => <a>{children}</a>,
}))

import { PendingGateApprovalsBanner } from "./PendingGateApprovalsBanner"

function approval(overrides: Record<string, unknown> = {}) {
  return {
    threadId: "t-1",
    fingerprint: "fp-1",
    status: "pending",
    issueId: "3f865ff3-c06c-4b6d-a043-4e20669f9363",
    issueIdentifier: "OPE-123",
    baseSha: "",
    headSha: "",
    failedRuleIds: ["atomicity"],
    diffStats: {
      rawLoc: 400,
      effectiveLoc: 400,
      productionFiles: 1,
      exceeded: [],
    },
    evidenceTail: "",
    approvalUrl: null,
    notified: true,
    requestedAt: null,
    decidedAt: null,
    decidedBy: null,
    ...overrides,
  }
}

afterEach(() => {
  cleanup()
  gateApprovalMock.approvals = []
})

it("shows the human Linear identifier instead of the internal id", () => {
  gateApprovalMock.approvals = [approval()]

  render(<PendingGateApprovalsBanner />)

  expect(screen.getByText("OPE-123")).toBeTruthy()
  expect(screen.queryByText("3f865ff3-c06c-4b6d-a043-4e20669f9363")).toBeNull()
})

it("uses the generic label when the human Linear identifier is absent", () => {
  gateApprovalMock.approvals = [approval({ issueIdentifier: null })]

  render(<PendingGateApprovalsBanner />)

  expect(screen.getByText("gate breach")).toBeTruthy()
  expect(screen.queryByText("3f865ff3-c06c-4b6d-a043-4e20669f9363")).toBeNull()
})
