/** @vitest-environment jsdom */

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

const gateApprovalMock = vi.hoisted(() => ({
  approvals: [] as Array<Record<string, unknown>>,
  mutateAsync: vi.fn(),
}))

vi.mock("@/features/agents/lib/gateApproval", () => ({
  useGateApprovals: () => ({
    data: {
      threadId: "t-1",
      isOwner: true,
      approvals: gateApprovalMock.approvals,
    },
  }),
  useGateApprovalDecision: () => ({
    isPending: false,
    mutateAsync: gateApprovalMock.mutateAsync,
  }),
}))

import { GateApprovalCard } from "./GateApprovalCard"

function approval(overrides: Record<string, unknown> = {}) {
  return {
    fingerprint: "fp-1",
    status: "pending",
    issueId: "3f865ff3-c06c-4b6d-a043-4e20669f9363",
    issueIdentifier: "OPE-123",
    baseSha: "b".repeat(40),
    headSha: "h".repeat(40),
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
  gateApprovalMock.mutateAsync.mockReset()
})

it("keeps a decision failure on the acted-on fingerprint and shows human identifiers", async () => {
  gateApprovalMock.approvals = [
    approval(),
    approval({
      fingerprint: "fp-2",
      issueId: "8c4aa094-790d-48a0-afc8-ae5f8cb5f693",
      issueIdentifier: "OPE-124",
    }),
  ]
  gateApprovalMock.mutateAsync.mockImplementation(({ fingerprint }) =>
    fingerprint === "fp-2"
      ? Promise.reject(new Error("Decision failed for fp-2"))
      : Promise.resolve()
  )

  render(<GateApprovalCard threadId="t-1" />)

  const cards = screen.getAllByTestId("gate-approval-card")
  expect(cards).toHaveLength(2)
  const firstCard = cards[0]!
  const secondCard = cards[1]!
  expect(within(firstCard).getByText(/OPE-123/)).toBeTruthy()
  expect(within(secondCard).getByText(/OPE-124/)).toBeTruthy()
  expect(screen.queryByText("3f865ff3-c06c-4b6d-a043-4e20669f9363")).toBeNull()
  expect(screen.queryByText("8c4aa094-790d-48a0-afc8-ae5f8cb5f693")).toBeNull()
  expect(within(firstCard).getByText(/Fingerprint: fp-1/)).toBeTruthy()
  expect(within(secondCard).getByText(/Fingerprint: fp-2/)).toBeTruthy()

  fireEvent.click(within(secondCard).getByRole("button", { name: "Approve" }))

  await waitFor(() => {
    expect(
      within(secondCard).getByText("Decision failed for fp-2")
    ).toBeTruthy()
  })
  expect(within(firstCard).queryByText("Decision failed for fp-2")).toBeNull()
})

it("uses the generic issue label when the human identifier is absent", () => {
  gateApprovalMock.approvals = [approval({ issueIdentifier: null })]

  render(<GateApprovalCard threadId="t-1" />)

  expect(screen.getByText(/Linear issue/)).toBeTruthy()
  expect(screen.queryByText("3f865ff3-c06c-4b6d-a043-4e20669f9363")).toBeNull()
})
