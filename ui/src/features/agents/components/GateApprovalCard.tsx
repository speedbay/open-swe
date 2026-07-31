import { useMemo, useState } from "react"
import { ShieldAlert } from "lucide-react"

import type { GateApproval } from "@/features/agents/lib/types"
import {
  useGateApprovalDecision,
  useGateApprovals,
} from "@/features/agents/lib/queries"
import { Button } from "@/components/ui/button"

function shortSha(value: string): string {
  return value ? value.slice(0, 7) : "unknown"
}

function pending(
  approvals: Array<GateApproval> | undefined
): Array<GateApproval> {
  return (approvals ?? []).filter((approval) => approval.status === "pending")
}

export function GateApprovalCard({
  threadId,
  pollWhileActive = false,
}: {
  threadId: string
  pollWhileActive?: boolean
}) {
  const query = useGateApprovals(threadId, { pollWhileActive })
  const decision = useGateApprovalDecision(threadId)
  const [error, setError] = useState<string | null>(null)
  const approvals = useMemo(
    () => pending(query.data?.approvals),
    [query.data?.approvals]
  )

  if (approvals.length === 0) return null

  const isOwner = query.data?.isOwner === true
  const decide = async (approval: GateApproval, kind: "approve" | "reject") => {
    setError(null)
    try {
      await decision.mutateAsync({
        fingerprint: approval.fingerprint,
        decision: kind,
      })
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="border-b border-[var(--ui-border)] bg-[var(--ui-panel)] px-4 py-3">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-3">
        {approvals.map((approval) => {
          const busy = decision.isPending
          return (
            <section
              key={approval.fingerprint}
              data-testid="gate-approval-card"
              className="rounded-lg border border-[var(--ui-border)] bg-[var(--ui-bg)] p-3 shadow-sm"
            >
              <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                <div className="min-w-0 space-y-1">
                  <div className="flex items-center gap-2 text-sm font-semibold text-[var(--ui-text)]">
                    <ShieldAlert className="size-4 text-[var(--ui-danger)]" />
                    PR-standards gate breach — approval required
                  </div>
                  <p className="text-xs text-[var(--ui-text-dim)]">
                    {approval.issueId ?? "Linear issue"} ·{" "}
                    {shortSha(approval.baseSha)} → {shortSha(approval.headSha)}{" "}
                    · corrective round {approval.rounds}
                  </p>
                  <p className="font-mono text-[0.68rem] break-all text-[var(--ui-text-dim)]">
                    Fingerprint: {approval.fingerprint}
                  </p>
                </div>
                <div className="flex shrink-0 flex-wrap gap-2">
                  <Button
                    disabled={!isOwner || busy}
                    onClick={() => void decide(approval, "approve")}
                  >
                    Approve
                  </Button>
                  <Button
                    variant="destructive"
                    disabled={!isOwner || busy}
                    onClick={() => void decide(approval, "reject")}
                  >
                    Reject
                  </Button>
                </div>
              </div>

              {!isOwner && (
                <p className="mt-3 text-xs text-[var(--ui-text-dim)]">
                  Only the thread owner can approve or reject this gate breach.
                </p>
              )}
              {error && (
                <p className="mt-3 text-xs text-[color:var(--ui-danger)]">
                  {error}
                </p>
              )}

              <div className="mt-3 grid gap-3 md:grid-cols-[minmax(0,1fr)_auto]">
                <div className="min-w-0">
                  <p className="text-xs font-medium text-[var(--ui-text)]">
                    Failed rules
                  </p>
                  <ul className="mt-1 space-y-1 text-xs text-[var(--ui-text-dim)]">
                    {approval.failedRuleIds.map((rule) => (
                      <li key={rule} className="font-mono">
                        {rule}
                      </li>
                    ))}
                  </ul>
                </div>
                <div className="rounded-md border border-[var(--ui-border)] px-3 py-2 text-xs text-[var(--ui-text-dim)]">
                  <span>raw LOC {approval.diffStats.rawLoc}</span>
                  <span className="mx-2">
                    effective {Math.round(approval.diffStats.effectiveLoc)}
                  </span>
                  <span>{approval.diffStats.productionFiles} prod files</span>
                </div>
              </div>

              {approval.diffStats.exceeded.length > 0 && (
                <p className="mt-3 text-xs text-[var(--ui-text-dim)]">
                  {approval.diffStats.exceeded.join("; ")}
                </p>
              )}
            </section>
          )
        })}
      </div>
    </div>
  )
}
