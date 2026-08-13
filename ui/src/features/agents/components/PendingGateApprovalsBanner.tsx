import { ShieldAlert } from "lucide-react"
import { Link } from "@tanstack/react-router"

import { usePendingGateApprovals } from "@/features/agents/lib/gateApproval"

export function PendingGateApprovalsBanner() {
  const query = usePendingGateApprovals()
  const approvals = query.data?.approvals ?? []

  if (approvals.length === 0) return null

  return (
    <div
      data-testid="pending-gate-approvals-banner"
      className="mx-auto w-full max-w-2xl rounded-lg border border-[var(--ui-border)] bg-[var(--ui-panel)] p-3"
    >
      <div className="flex items-center gap-2 text-sm font-semibold text-[var(--ui-text)]">
        <ShieldAlert className="size-4 text-[var(--ui-danger)]" />
        {approvals.length === 1
          ? "1 gate breach pending approval"
          : `${approvals.length} gate breaches pending approval`}
      </div>
      <ul className="mt-2 space-y-1">
        {approvals.map((approval) => (
          <li
            key={`${approval.threadId}-${approval.fingerprint}`}
            className="text-xs text-[var(--ui-text-dim)]"
          >
            <Link
              to="/agents/$threadId"
              params={{ threadId: approval.threadId }}
              className="font-medium underline underline-offset-2"
            >
              {approval.issueIdentifier ?? "gate breach"}
            </Link>{" "}
            · {approval.failedRuleIds.join(", ")} · raw LOC{" "}
            {approval.diffStats.rawLoc}
          </li>
        ))}
      </ul>
    </div>
  )
}
