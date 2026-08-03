// SPEEDBAY org-layer module (OPE-10) — upstream does not own it. Gate-breach
// approval types, API calls, and query hooks live here, in a fork-added file,
// so upstream `types.ts` / `api.ts` / `queries.ts` stay unedited apart from the
// one marked `agentsRequest` export (see FORK.md registration table).
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"

import { agentsRequest } from "./api"
import { agentThreadKeys, invalidateAgentThreadLists } from "./queries"
import type { WorkflowApprovalStatus } from "./types"

export interface GateDiffStats {
  rawLoc: number
  effectiveLoc: number
  productionFiles: number
  exceeded: Array<string>
}

export interface GateApproval {
  fingerprint: string
  status: WorkflowApprovalStatus
  issueId: string | null
  baseSha: string
  headSha: string
  failedRuleIds: Array<string>
  diffStats: GateDiffStats
  evidenceTail: string
  approvalUrl: string | null
  notified: boolean
  requestedAt: string | null
  decidedAt: string | null
  decidedBy: string | null
}

export interface GateApprovalsResponse {
  threadId: string
  isOwner: boolean
  approvals: Array<GateApproval>
}

export interface PendingGateApproval extends GateApproval {
  threadId: string
}

export interface PendingGateApprovalsResponse {
  approvals: Array<PendingGateApproval>
}

// Key values are unchanged from the pre-consolidation implementation so any
// cached queries keep matching.
export const gateApprovalKeys = {
  forThread: (threadId: string) =>
    ["agent-threads", threadId, "gate-approvals"] as const,
  pending: ["agent-threads", "pending-gate-approvals"] as const,
}

export const gateApprovalsApi = {
  listGateApprovals: (threadId: string) =>
    agentsRequest<GateApprovalsResponse>(
      `/gate-approval/${encodeURIComponent(threadId)}`
    ),
  approveGateBreach: (threadId: string, fingerprint: string) =>
    agentsRequest<{ status: string; fingerprint: string }>(
      `/gate-approval/${encodeURIComponent(threadId)}/${encodeURIComponent(fingerprint)}/approve`,
      { method: "POST" }
    ),
  rejectGateBreach: (threadId: string, fingerprint: string) =>
    agentsRequest<{ status: string; fingerprint: string }>(
      `/gate-approval/${encodeURIComponent(threadId)}/${encodeURIComponent(fingerprint)}/reject`,
      { method: "POST" }
    ),
  listPendingGateApprovals: () =>
    agentsRequest<PendingGateApprovalsResponse>(`/gate-approval/pending`),
}

export function useGateApprovals(
  threadId: string,
  options: { pollWhileActive?: boolean } = {}
) {
  return useQuery({
    queryKey: gateApprovalKeys.forThread(threadId),
    queryFn: () => gateApprovalsApi.listGateApprovals(threadId),
    enabled: Boolean(threadId),
    refetchInterval: (query) =>
      options.pollWhileActive ||
      query.state.data?.approvals.some(
        (approval) => approval.status === "pending"
      )
        ? 3000
        : false,
    retry: false,
  })
}

export function useGateApprovalDecision(threadId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (vars: {
      fingerprint: string
      decision: "approve" | "reject"
    }) =>
      vars.decision === "approve"
        ? gateApprovalsApi.approveGateBreach(threadId, vars.fingerprint)
        : gateApprovalsApi.rejectGateBreach(threadId, vars.fingerprint),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: gateApprovalKeys.forThread(threadId),
      })
      void queryClient.invalidateQueries({
        queryKey: gateApprovalKeys.pending,
      })
      void queryClient.invalidateQueries({
        queryKey: agentThreadKeys.detail(threadId),
      })
      invalidateAgentThreadLists(queryClient)
    },
  })
}

export function usePendingGateApprovals() {
  return useQuery({
    queryKey: gateApprovalKeys.pending,
    queryFn: () => gateApprovalsApi.listPendingGateApprovals(),
    refetchInterval: 10000,
    retry: false,
  })
}
