import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Bell, CheckCircle2, Send, ShieldCheck } from "lucide-react";
import { expireApprovals, getDashboard } from "../api";
import { Badge, Button, CommandResultPanel, EmptyState, ErrorState, formatDate, ListRow, LoadingState, SectionHeader, shortText, statusTone } from "../components/Primitives";
import { invalidateAfterMaintenanceCommand, queryKeys } from "../queryKeys";
import type { CommandResult, DispatchActionSummary, ApprovalSummary, RouteKey } from "../types";

export function DashboardScreen({
  token,
  navigate
}: {
  token: string;
  navigate: (route: RouteKey, selectedId?: string) => void;
}) {
  const queryClient = useQueryClient();
  const dashboard = useQuery({
    queryKey: queryKeys.dashboard(),
    queryFn: () => getDashboard(token),
    enabled: Boolean(token),
    refetchInterval: 15_000
  });
  const expire = useMutation({
    mutationFn: () => expireApprovals(token, { reason: "operator console maintenance" }),
    onSuccess: async () => {
      await invalidateAfterMaintenanceCommand(queryClient);
    }
  });

  if (dashboard.isLoading) {
    return <LoadingState title="Loading dashboard" />;
  }
  if (dashboard.error) {
    return <ErrorState title="Dashboard unavailable" error={dashboard.error} />;
  }

  const snapshot = dashboard.data;
  const pendingApprovals = snapshot?.pending_approvals ?? [];
  const overdueApprovals = pendingApprovals.filter((approval) => approval.is_overdue);
  const recoveryActions = snapshot?.failed_or_needs_review_actions ?? [];
  const staleSendingActions = snapshot?.stale_sending_actions ?? [];
  const healthWarnings = snapshot?.recent_health_warnings ?? [];
  const recentErrors = snapshot?.recent_errors ?? [];
  const policyStatus = snapshot?.policy_status;
  const policyDiff = policyStatus?.policy_import_diff;
  const policyNeedsAttention = policyStatus?.initialized === false || policyDiff?.status === "differs";
  const dispatchAttention = recoveryActions.length + staleSendingActions.length;
  const actionCount =
    pendingApprovals.length + dispatchAttention + healthWarnings.length + (policyNeedsAttention ? 1 : 0);

  return (
    <section className="work-grid" aria-label="Dashboard">
      <div className="work-main">
        <div className="queue-panel">
          <SectionHeader
            eyebrow="Action Queue"
            title="Operator attention"
            badge={<Badge tone={actionCount ? "warning" : "success"}>{actionCount ? "Review" : "Clear"}</Badge>}
          >
            <p className="section-note">Pending blockers, recovery work, and runtime warnings from the local store.</p>
          </SectionHeader>
          <div className="metric-row">
            <Metric label="Pending approvals" value={pendingApprovals.length} tone={pendingApprovals.length ? "warning" : "neutral"} />
            <Metric label="Overdue" value={overdueApprovals.length} tone={overdueApprovals.length ? "danger" : "neutral"} />
            <Metric label="Dispatch recovery" value={dispatchAttention} tone={dispatchAttention ? "danger" : "neutral"} />
            <Metric label="Health warnings" value={healthWarnings.length} tone={healthWarnings.length ? "warning" : "neutral"} />
          </div>
          {actionCount === 0 ? (
            <EmptyState title="No pending operator actions" detail="Daemon and policy status remain visible for quick checks." />
          ) : null}
        </div>

        <PreviewList
          approvals={pendingApprovals}
          actions={recoveryActions}
          staleActions={staleSendingActions}
          onApproval={(approvalId) => navigate("approvals", approvalId)}
          onAction={(actionId) => navigate("dispatch", String(actionId))}
        />
      </div>

      <aside className="work-detail">
        <div className="detail-panel">
          <p className="eyebrow">Product Policy</p>
          <h2>Runtime source of truth</h2>
          <dl className="fact-list">
            <div>
              <dt>Initialized</dt>
              <dd>
                <Badge tone={policyStatus?.initialized ? "success" : "warning"}>
                  {policyStatus?.initialized ? "yes" : "missing"}
                </Badge>
              </dd>
            </div>
            <div>
              <dt>Policy Import Diff</dt>
              <dd>
                <Badge tone={statusTone(policyDiff?.status)}>{policyDiff?.status ?? "unknown"}</Badge>
              </dd>
            </div>
            <div>
              <dt>Last tick</dt>
              <dd>{formatDate(snapshot?.last_run?.last_tick_finished_at)}</dd>
            </div>
          </dl>
          {policyDiff?.message ? <p className="detail-note">{policyDiff.message}</p> : null}
          {policyNeedsAttention ? (
            <div className="quiet-empty">
              <AlertTriangle aria-hidden="true" size={18} />
              <span>Policy attention needed</span>
            </div>
          ) : null}
        </div>

        <div className="detail-panel">
          <p className="eyebrow">Maintenance</p>
          <h2>Approval expiry</h2>
          <p className="detail-note">
            Query views show overdue approvals without mutating them. Expiry stays an explicit command.
          </p>
          <Button disabled={expire.isPending} onClick={() => expire.mutate()} tone="warning">
            Expire overdue approvals
          </Button>
          <CommandResultPanel result={(expire.data as CommandResult | undefined) ?? null} />
        </div>

        <div className="detail-panel">
          <p className="eyebrow">Recent Highlights</p>
          <h2>Command and audit signals</h2>
          {recentErrors.length ? (
            <ul className="timeline-list">
              {recentErrors.slice(0, 5).map((item, index) => (
                <li key={`${String(item.type)}-${index}`}>
                  <AlertTriangle aria-hidden="true" size={14} />
                  <span>{String(item.message ?? item.type ?? "recent issue")}</span>
                  <small>{String(item.status ?? "")}</small>
                </li>
              ))}
            </ul>
          ) : (
            <div className="quiet-empty">
              <CheckCircle2 aria-hidden="true" size={18} />
              <span>No recent failed command highlights</span>
            </div>
          )}
        </div>
      </aside>
    </section>
  );
}

function Metric({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className={`metric-card ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function PreviewList({
  approvals,
  actions,
  staleActions,
  onApproval,
  onAction
}: {
  approvals: ApprovalSummary[];
  actions: DispatchActionSummary[];
  staleActions: DispatchActionSummary[];
  onApproval: (approvalId: string) => void;
  onAction: (actionId: number) => void;
}) {
  const dispatchRows = [...actions, ...staleActions];
  return (
    <div className="split-panels">
      <section className="queue-panel">
        <div className="subsection-title">
          <Bell aria-hidden="true" size={16} />
          <h2>Approval preview</h2>
        </div>
        {approvals.length ? (
          <div className="list-stack">
            {approvals.slice(0, 6).map((approval) => (
              <ListRow
                badge={<Badge tone={approval.is_overdue ? "danger" : statusTone(approval.status)}>{approval.is_overdue ? "overdue" : approval.status}</Badge>}
                key={approval.approval_id}
                meta={`${approval.task_short_id ?? "no task"} · ${formatDate(approval.created_at)}`}
                onClick={() => onApproval(approval.approval_id)}
                selected={false}
                title={approval.approval_id}
              >
                <span className="row-preview">{shortText(approval.preview)}</span>
              </ListRow>
            ))}
          </div>
        ) : (
          <EmptyState title="No pending approvals" detail="Approval blockers will appear here when they need review." />
        )}
      </section>

      <section className="queue-panel">
        <div className="subsection-title">
          <Send aria-hidden="true" size={16} />
          <h2>Dispatch recovery</h2>
        </div>
        {dispatchRows.length ? (
          <div className="list-stack">
            {dispatchRows.slice(0, 6).map((action) => (
              <ListRow
                badge={<Badge tone={statusTone(action.status)}>{action.status}</Badge>}
                key={action.action_id}
                meta={`${action.kind} · ${formatDate(action.updated_at)}`}
                onClick={() => onAction(action.action_id)}
                selected={false}
                title={`Action ${action.action_id}`}
              >
                <span className="row-preview">{action.target_message_id ?? "no target message"}</span>
              </ListRow>
            ))}
          </div>
        ) : (
          <EmptyState title="No dispatch recovery" detail="Failed or review-needed send actions will appear here." />
        )}
      </section>

      <section className="queue-panel full-span">
        <div className="subsection-title">
          <ShieldCheck aria-hidden="true" size={16} />
          <h2>Daily workflow</h2>
        </div>
        <p className="detail-note">
          Use Approvals for human-reviewed replies, Tasks for conversation context, and Dispatch for retry, cancel, or mark-sent recovery.
        </p>
      </section>
    </div>
  );
}
