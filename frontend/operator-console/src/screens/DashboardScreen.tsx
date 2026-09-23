import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowUpRight, Bell, CheckCircle2, ClipboardList, RefreshCw, Send, ShieldCheck } from "lucide-react";
import { useTranslation } from "react-i18next";
import { expireApprovals, getDashboard } from "../api";
import {
  Badge,
  Button,
  CommandResultPanel,
  EmptyState,
  ErrorState,
  formatDate,
  ListRow,
  LoadingState,
  SectionHeader,
  shortText,
  statusTone,
  TechnicalDetails
} from "../components/Primitives";
import { invalidateAfterMaintenanceCommand, queryKeys } from "../queryKeys";
import { enumLabel } from "../presentation";
import type { ApprovalSummary, AttentionTask, BotMembershipStatus, CommandResult, DispatchActionSummary, IngestionStatus, RouteKey } from "../types";

export function DashboardScreen({ token, navigate }: { token: string; navigate: (route: RouteKey, selectedId?: string, view?: "attention" | "all") => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const dashboard = useQuery({
    queryKey: queryKeys.dashboard(),
    queryFn: () => getDashboard(token),
    enabled: Boolean(token),
    refetchInterval: 15_000
  });
  const expire = useMutation({
    mutationFn: () => expireApprovals(token, { reason: "operator console maintenance" }),
    onSuccess: async () => invalidateAfterMaintenanceCommand(queryClient)
  });

  if (dashboard.isLoading) {
    return <LoadingState title={t("dashboard.loading")} />;
  }
  if (dashboard.error && !dashboard.data) {
    return <ErrorState title={t("dashboard.unavailable")} error={dashboard.error} />;
  }

  const snapshot = dashboard.data;
  const pendingApprovals = snapshot?.pending_approvals ?? [];
  const recoveryActions = snapshot?.failed_or_needs_review_actions ?? [];
  const staleSendingActions = snapshot?.stale_sending_actions ?? [];
  const attentionTasks = snapshot?.attention_tasks ?? [];
  const attention = snapshot?.attention_summary;
  const healthIssueCount = snapshot?.health_issue_summary?.open_issue_count ?? 0;
  const recentErrors = snapshot?.recent_errors ?? [];
  const policyStatus = snapshot?.policy_status;
  const policyDiff = policyStatus?.policy_import_diff;
  const policyNeedsAttention = policyStatus?.initialized === false || policyDiff?.status === "differs";
  const actionCount = attention?.total_item_count ?? 0;
  const sendAttention = (attention?.uncertain_action_count ?? 0) + (attention?.failed_action_count ?? 0);
  const blockedOrFailed = (attention?.blocked_processing_count ?? 0) + (attention?.failed_processing_count ?? 0);

  return (
    <section className="work-grid" aria-label={t("dashboard.aria")}>
      <div className="work-main">
        <div className="queue-panel dashboard-overview">
          <SectionHeader
            eyebrow={t("dashboard.eyebrow")}
            title={t("dashboard.title")}
            badge={<Badge tone={actionCount ? "warning" : "success"}>{actionCount ? t("dashboard.itemCount", { count: actionCount }) : t("dashboard.cleared")}</Badge>}
          >
            <p className="section-note">{t("dashboard.intro")}</p>
            <div className="command-buttons">
              <Button disabled={dashboard.isFetching} onClick={() => void dashboard.refetch()}>
                <RefreshCw aria-hidden="true" size={15} />
                {dashboard.isFetching ? t("common.refreshing") : t("common.refresh")}
              </Button>
              <span className="detail-note">{t("common.updatedAt", { time: dashboard.dataUpdatedAt ? formatDate(new Date(dashboard.dataUpdatedAt).toISOString()) : t("common.notUpdated") })}</span>
            </div>
            {dashboard.error ? <p className="detail-note danger">{t("common.cachedRefreshError", { code: "" })}</p> : null}
          </SectionHeader>
          <div aria-label={t("dashboard.index")} className="decision-index">
            <DecisionLink count={sendAttention} hint={t("dashboard.viewDispatch")} label={t("dashboard.dispatchAttention")} onClick={() => navigate("dispatch", undefined, "attention")} tone="danger" />
            <DecisionLink count={attention?.pending_approval_count ?? 0} hint={t("dashboard.viewApproval")} label={t("dashboard.pendingApproval")} onClick={() => navigate("approvals")} tone="warning" />
            <DecisionLink count={blockedOrFailed} hint={t("dashboard.viewTasks")} label={t("dashboard.blockedTasks")} onClick={() => navigate("tasks", undefined, "all")} tone="danger" />
          </div>
          <p className="decision-context">{t("dashboard.affectedTasks", { count: attention?.affected_task_count ?? 0 })}</p>
          {actionCount === 0 ? <EmptyState title={t("dashboard.emptyTitle")} detail={t("dashboard.emptyDetail")} /> : null}
        </div>

        <AttentionTasks tasks={attentionTasks} onTask={(taskId) => navigate("tasks", taskId)} />
        <PreviewList
          approvals={pendingApprovals}
          actions={recoveryActions}
          staleActions={staleSendingActions}
          onApproval={(approvalId) => navigate("approvals", approvalId)}
          onAction={(actionId) => navigate("dispatch", String(actionId))}
        />
      </div>

      <aside className="work-detail">
        <IngestionPanel status={snapshot?.ingestion_status} />
        <MembershipPanel status={snapshot?.bot_membership_status} />

        <div className="detail-panel">
          <p className="eyebrow">{t("dashboard.runtimeBoundary")}</p>
          <h2>{t("dashboard.runtimePolicy")}</h2>
          <dl className="fact-list">
            <div><dt>{t("dashboard.initialization")}</dt><dd><Badge tone={policyStatus?.initialized ? "success" : "warning"}>{policyStatus?.initialized ? t("dashboard.initialized") : t("runtime.missing")}</Badge></dd></div>
            <div><dt>{t("dashboard.policyImportDiff")}</dt><dd><Badge tone={statusTone(policyDiff?.status)}>{enumLabel(t, "status", policyDiff?.status, t("common.unknown"))}</Badge></dd></div>
            <div><dt>{t("dashboard.lastTick")}</dt><dd>{formatDate(snapshot?.last_run?.last_tick_finished_at)}</dd></div>
          </dl>
          <p className="detail-note">{policyDiffSummary(t, policyDiff?.status, policyStatus?.initialized === true)}</p>
          {policyDiff?.message ? (
            <TechnicalDetails>
              <p className="detail-note">{policyDiff.message}</p>
            </TechnicalDetails>
          ) : null}
          {policyNeedsAttention ? <div className="quiet-empty"><AlertTriangle aria-hidden="true" size={18} /><span>{t("dashboard.policyNeedsReview")}</span></div> : null}
          <div className="command-buttons">
            <Button onClick={() => navigate("settings")}>{t("dashboard.openSettings")}</Button>
            <Button onClick={() => navigate("health")}>{t("dashboard.openHealth")}</Button>
          </div>
        </div>

        <div className="detail-panel">
          <p className="eyebrow">{t("dashboard.maintenance")}</p>
          <h2>{t("dashboard.expireTitle")}</h2>
          <p className="detail-note">{t("dashboard.expireDetail")}</p>
          <Button disabled={expire.isPending} onClick={() => expire.mutate()} tone="warning">{t("dashboard.expire")}</Button>
          <CommandResultPanel result={(expire.data as CommandResult | undefined) ?? null} />
        </div>

        <div className="detail-panel">
          <p className="eyebrow">{t("dashboard.recentSignals")}</p>
          <h2>{t("dashboard.recentErrors")}</h2>
          {recentErrors.length ? (
            <ul className="timeline-list">
              {recentErrors.slice(0, 5).map((item, index) => (
                <li key={`${String(item.type)}-${index}`}><AlertTriangle aria-hidden="true" size={14} /><span>{String(item.message ?? item.type ?? "recent issue")}</span><small title={String(item.status ?? "")}>{item.status ? enumLabel(t, "status", String(item.status)) : ""}</small></li>
              ))}
            </ul>
          ) : <div className="quiet-empty"><CheckCircle2 aria-hidden="true" size={18} /><span>{t("dashboard.noRecentErrors")}</span></div>}
          <p className="detail-note">{t("dashboard.healthIssues", { count: healthIssueCount })}</p>
        </div>
      </aside>
    </section>
  );
}

function MembershipPanel({ status }: { status: BotMembershipStatus | undefined }) {
  const { t } = useTranslation();
  const attention = status?.facts.filter((fact) => fact.status !== "present") ?? [];
  return (
    <div className="detail-panel">
      <p className="eyebrow">{t("dashboard.membershipEyebrow")}</p>
      <div className="detail-title-row">
        <h2>{t("dashboard.membershipTitle")}</h2>
        <Badge tone={status?.summary.absent ? "danger" : status?.summary.unknown || status?.summary.unobserved ? "warning" : "success"}>
          {t("dashboard.membershipSummary", { absent: status?.summary.absent ?? 0, unknown: status?.summary.unknown ?? 0, unobserved: status?.summary.unobserved ?? 0 })}
        </Badge>
      </div>
      {attention.length ? (
        <ul className="timeline-list">
          {attention.slice(0, 5).map((fact) => (
            <li key={fact.chat_id}>
              <AlertTriangle aria-hidden="true" size={14} />
              <span>{fact.chat_id}</span>
              <small title={`${fact.status} · ${fact.source ?? "unknown"}`}>{enumLabel(t, "status", fact.status)} · {enumLabel(t, "source", fact.source, t("dashboard.unknownSource"))} · {formatDate(fact.checked_at)}</small>
            </li>
          ))}
        </ul>
      ) : <div className="quiet-empty"><CheckCircle2 aria-hidden="true" size={18} /><span>{t("dashboard.membershipClear")}</span></div>}
    </div>
  );
}

function IngestionPanel({ status }: { status: IngestionStatus | undefined }) {
  const { t } = useTranslation();
  const summary = status?.summary;
  const backlogs = status?.sources.filter((source) => !source.drain_complete) ?? [];
  return (
    <div className="detail-panel">
      <p className="eyebrow">{t("dashboard.ingestionEyebrow")}</p>
      <div className="detail-title-row">
        <h2>{t("dashboard.ingestionTitle")}</h2>
        <Badge tone={summary?.backlog_count ? "warning" : "success"}>{t("dashboard.sourceCount", { count: summary?.backlog_count ?? 0 })}</Badge>
      </div>
      <dl className="fact-list">
        <div><dt>{t("dashboard.oldestCheckpoint")}</dt><dd>{formatAge(summary?.oldest_checkpoint_age_seconds, t)}</dd></div>
        <div><dt>{t("dashboard.budgetExhausted")}</dt><dd>{summary?.budget_exhausted_count ?? 0}</dd></div>
      </dl>
      {backlogs.length ? (
        <ul className="timeline-list">
          {backlogs.slice(0, 5).map((source) => (
            <li key={source.checkpoint_key}>
              <AlertTriangle aria-hidden="true" size={14} />
              <span>{source.checkpoint_key}</span>
              <small title={source.backlog?.reason ?? "deferred"}>{t("dashboard.backlogProgress", { reason: enumLabel(t, "reason", source.backlog?.reason ?? "deferred"), pages: source.backlog?.pages_fetched ?? 0, messages: source.backlog?.messages_fetched ?? 0 })}</small>
            </li>
          ))}
        </ul>
      ) : <div className="quiet-empty"><CheckCircle2 aria-hidden="true" size={18} /><span>{t("dashboard.noBacklog")}</span></div>}
    </div>
  );
}

function formatAge(seconds: number | null | undefined, t: ReturnType<typeof useTranslation>["t"]): string {
  if (seconds == null) {
    return t("dashboard.noCheckpoint");
  }
  if (seconds < 60) {
    return t("dashboard.seconds", { count: seconds });
  }
  if (seconds < 3600) {
    return t("dashboard.minutes", { count: Math.floor(seconds / 60) });
  }
  return t("dashboard.hours", { count: Math.floor(seconds / 3600) });
}

function policyDiffSummary(t: ReturnType<typeof useTranslation>["t"], status: string | null | undefined, initialized: boolean): string {
  if (status === "matches") {
    return t("policy.diffMatches");
  }
  if (status === "differs") {
    return initialized ? t("policy.diffDiffers") : t("policy.diffUninitialized");
  }
  return t("policy.diffUnknown");
}

function DecisionLink({ count, hint, label, onClick, tone }: {
  count: number;
  hint: string;
  label: string;
  onClick: () => void;
  tone: "danger" | "warning";
}) {
  return (
    <button className={`decision-link ${count ? tone : "quiet"}`} onClick={onClick} type="button">
      <span className="decision-label">{label}</span>
      <strong>{count}</strong>
      <span className="decision-hint">{hint}<ArrowUpRight aria-hidden="true" size={15} /></span>
    </button>
  );
}

function AttentionTasks({ tasks, onTask }: { tasks: AttentionTask[]; onTask: (taskId: string) => void }) {
  const { t } = useTranslation();
  if (!tasks.length) {
    return null;
  }
  return (
    <section className="queue-panel">
      <div className="subsection-title"><ClipboardList aria-hidden="true" size={16} /><h2>{t("dashboard.groupedTasks")}</h2></div>
      <div className="list-stack">
        {tasks.map((task) => (
          <ListRow
            badge={<Badge tone="warning">{t("dashboard.itemCount", { count: attentionTaskCount(task) })}</Badge>}
            key={task.task_id}
            meta={t("dashboard.taskMeta", { chat: task.chat_id ?? t("common.notRecorded"), time: formatDate(task.latest_at) })}
            onClick={() => onTask(task.task_short_id)}
            selected={false}
            title={task.task_label || task.task_short_id}
          >
            <span className="row-preview">
              {t("dashboard.taskAttention", { approvals: task.pending_approval_count, uncertain: task.uncertain_action_count, failed: task.failed_action_count, processing: task.blocked_processing_count + task.failed_processing_count })}
            </span>
          </ListRow>
        ))}
      </div>
    </section>
  );
}

function attentionTaskCount(task: AttentionTask): number {
  return task.pending_approval_count + task.failed_action_count + task.uncertain_action_count + task.blocked_processing_count + task.failed_processing_count;
}

function PreviewList({ approvals, actions, staleActions, onApproval, onAction }: {
  approvals: ApprovalSummary[];
  actions: DispatchActionSummary[];
  staleActions: DispatchActionSummary[];
  onApproval: (approvalId: string) => void;
  onAction: (actionId: number) => void;
}) {
  const { t } = useTranslation();
  const dispatchRows = [...actions, ...staleActions].filter(
    (action, index, rows) => rows.findIndex((candidate) => candidate.action_id === action.action_id) === index
  );
  return (
    <div className="split-panels">
      <section className="queue-panel">
        <div className="subsection-title"><Bell aria-hidden="true" size={16} /><h2>{t("dashboard.approvalPreview")}</h2></div>
        {approvals.length ? (
          <div className="list-stack">
            {approvals.slice(0, 6).map((approval) => (
              <ListRow
                badge={<Badge tone={approval.is_overdue ? "danger" : statusTone(approval.status)}>{approval.is_overdue ? t("approvals.overdue") : enumLabel(t, "status", approval.status)}</Badge>}
                key={approval.approval_id}
                meta={`${approval.task_short_id ?? t("common.notLinked")} · ${t("dashboard.sourceMessage", { id: approval.source_message_id ?? "unknown" })} · ${formatDate(approval.created_at)}`}
                onClick={() => onApproval(approval.approval_id)}
                selected={false}
                title={approval.approval_id}
              ><span className="row-preview">{t("dashboard.suggestedReply", { preview: shortText(approval.preview) })}</span></ListRow>
            ))}
          </div>
        ) : <EmptyState title={t("dashboard.noApprovals")} detail={t("dashboard.noApprovalsDetail")} />}
      </section>

      <section className="queue-panel">
        <div className="subsection-title"><Send aria-hidden="true" size={16} /><h2>{t("dashboard.dispatchPreview")}</h2></div>
        {dispatchRows.length ? (
          <div className="list-stack">
            {dispatchRows.slice(0, 6).map((action) => (
              <ListRow
                badge={<Badge tone={statusTone(action.status)}>{enumLabel(t, "status", action.status)}</Badge>}
                key={action.action_id}
                meta={`${action.task_short_id ?? t("common.notLinked")} · ${t("dashboard.targetMessage", { id: action.target_message_id ?? "unknown" })} · ${formatDate(action.updated_at)}`}
                onClick={() => onAction(action.action_id)}
                selected={false}
                title={t("dashboard.dispatchRecord", { id: action.action_id })}
              ><span className="row-preview">{t("dashboard.dispatchStatus", { status: enumLabel(t, "status", action.status) })}</span></ListRow>
            ))}
          </div>
        ) : <EmptyState title={t("dashboard.noDispatch")} detail={t("dashboard.noDispatchDetail")} />}
      </section>

      <section className="queue-panel full-span">
        <div className="subsection-title"><ShieldCheck aria-hidden="true" size={16} /><h2>{t("dashboard.guidance")}</h2></div>
        <p className="detail-note">{t("dashboard.guidanceDetail")}</p>
      </section>
    </div>
  );
}
