import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Info, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { getDashboard } from "../api";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  formatDate,
  ListRow,
  LoadingState,
  SegmentedControl,
  shortText,
  statusTone,
  TechnicalDetails
} from "../components/Primitives";
import { queryKeys } from "../queryKeys";
import { enumLabel } from "../presentation";
import type { AttentionTask, BotMembershipStatus, DashboardSnapshot, IngestionStatus, RouteKey, Tone } from "../types";

type Navigate = (route: RouteKey, selectedId?: string, view?: "attention" | "all") => void;
type QueueKind = "dispatch" | "approval" | "processing";
type QueueFilter = QueueKind | "all";

export function DashboardScreen({ token, navigate }: { token: string; navigate: Navigate }) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState<QueueFilter>("all");
  const [order, setOrder] = useState<"items" | "tasks">("items");
  const dashboard = useQuery({
    queryKey: queryKeys.dashboard(),
    queryFn: () => getDashboard(token),
    enabled: Boolean(token),
    refetchInterval: 15_000
  });
  if (dashboard.isLoading) {
    return <LoadingState title={t("dashboard.loading")} />;
  }
  if (dashboard.error && !dashboard.data) {
    return (
      <div>
        <ErrorState title={t("dashboard.unavailable")} error={dashboard.error} />
        <Button onClick={() => void dashboard.refetch()}>{t("common.refresh")}</Button>
      </div>
    );
  }

  const snapshot = dashboard.data;
  const attention = snapshot?.attention_summary;
  const healthIssueCount = snapshot?.health_issue_summary?.open_issue_count ?? 0;
  const recentErrors = snapshot?.recent_errors ?? [];
  const policyStatus = snapshot?.policy_status;
  const policyDiff = policyStatus?.policy_import_diff;
  const actionCount = attention?.total_item_count ?? 0;
  const sendAttention = (attention?.uncertain_action_count ?? 0) + (attention?.failed_action_count ?? 0);
  const blockedOrFailed = (attention?.blocked_processing_count ?? 0) + (attention?.failed_processing_count ?? 0);
  const queueFilters = [
    { kind: "dispatch", label: t("dashboard.dispatchAttention"), count: sendAttention, tone: "danger", route: "dispatch", view: "attention" },
    { kind: "approval", label: t("dashboard.pendingApproval"), count: attention?.pending_approval_count ?? 0, tone: "warning", route: "approvals" },
    { kind: "processing", label: t("dashboard.blockedTasks"), count: blockedOrFailed, tone: "danger", route: "tasks", view: "all" }
  ] as const;
  const activeFilter = queueFilters.find((item) => item.kind === filter);
  const nextStep = dashboardNextStep(snapshot, dashboard.error);
  const emptyBadge = nextStep?.state === "waiting" ? t("dashboard.waiting") : nextStep?.state === "starting" ? t("dashboard.preparing") : nextStep ? t("dashboard.notReady") : t("dashboard.cleared");

  return (
    <section className="dashboard-screen" aria-label={t("dashboard.aria")}>
      <header className="workbench-header">
        <div>
          <h1>{t("dashboard.title")}</h1>
          <div className="dashboard-status">
            <Badge tone={actionCount || nextStep?.state === "attention" ? "warning" : nextStep ? "info" : "success"}>
              {actionCount ? t("dashboard.pendingItems", { count: actionCount }) : emptyBadge}
            </Badge>
            {actionCount ? <span>{t("dashboard.affectedTasks", { count: attention?.affected_task_count ?? 0 })}</span> : null}
          </div>
        </div>
        <div className="dashboard-refresh">
          <span>{dashboard.dataUpdatedAt ? t("common.updatedAt", { time: formatDate(new Date(dashboard.dataUpdatedAt).toISOString()) }) : t("common.notUpdated")}</span>
          <Button disabled={dashboard.isFetching} onClick={() => void dashboard.refetch()}>
            <RefreshCw aria-hidden="true" size={15} />
            {dashboard.isFetching ? t("common.refreshing") : t("common.refresh")}
          </Button>
        </div>
      </header>

      <div className="dashboard-layout">
        <div className="work-main">
          {dashboard.error ? <p className="detail-note danger">{t("common.cachedRefreshError", { code: "" })}</p> : null}
          {nextStep ? <NextStepPanel navigate={navigate} snapshot={snapshot} step={nextStep} /> : null}
          <div aria-label={t("dashboard.index")} className="decision-index" role="group">
            {queueFilters.map((item) => (
              <DecisionLink
                count={item.count}
                key={item.kind}
                label={item.label}
                onClick={() => setFilter((current) => current === item.kind ? "all" : item.kind)}
                selected={filter === item.kind}
                tone={item.tone}
              />
            ))}
          </div>
          {actionCount === 0 && !nextStep ? <EmptyState title={t("dashboard.emptyTitle")} detail={t("dashboard.emptyDetail")} /> : null}
          {snapshot && (actionCount > 0 || activeFilter) ? (
            <section className="queue-panel dashboard-queue">
              <div className="detail-title-row">
                <h2>{t("dashboard.queueTitle")}</h2>
                <SegmentedControl
                  label={t("dashboard.queueTitle")}
                  options={[
                    { value: "items", label: t("dashboard.byPriority") },
                    { value: "tasks", label: t("dashboard.byTask") }
                  ]}
                  onChange={setOrder}
                  value={order}
                />
              </div>
              {activeFilter ? (
                <div className="command-buttons">
                  <Button onClick={() => setFilter("all")}>{t("dashboard.showAll")}</Button>
                  <Button onClick={() => navigate(activeFilter.route, undefined, "view" in activeFilter ? activeFilter.view : undefined)}>{t("dashboard.openQueue")}</Button>
                </div>
              ) : null}
              <ActionQueue filter={filter} navigate={navigate} order={order} snapshot={snapshot} />
            </section>
          ) : null}
        </div>

        <aside className="dashboard-context">
          <TechnicalDetails summary={t("dashboard.runtimePolicy")}>
            <dl className="fact-list">
              <div><dt>{t("dashboard.initialization")}</dt><dd>{policyStatus?.initialized === true ? t("dashboard.initialized") : policyStatus?.initialized === false ? t("runtime.missing") : t("common.unknown")}</dd></div>
              <div><dt>{t("dashboard.policyImportDiff")}</dt><dd>{enumLabel(t, "status", policyDiff?.status, t("common.unknown"))}</dd></div>
              <div><dt>{t("dashboard.lastTick")}</dt><dd>{formatDate(snapshot?.daemon_liveness?.last_tick_finished_at)}</dd></div>
            </dl>
            <p className="detail-note">{policyDiffSummary(t, policyDiff?.status, policyStatus?.initialized === true)}</p>
            {policyDiff?.message ? <p className="detail-note">{policyDiff.message}</p> : null}
            <div className="command-buttons">
              {nextStep?.route !== "policy" && policyDiff?.status === "differs" ? <Button onClick={() => navigate("policy")}>{t("dashboard.setupOpenPolicy")}</Button> : null}
              <Button onClick={() => navigate("settings")}>{t("dashboard.openSettings")}</Button>
              <Button onClick={() => navigate("health")}>{t("dashboard.openHealth")}</Button>
            </div>
          </TechnicalDetails>

          <TechnicalDetails summary={t("dashboard.signalsTitle")}>
            <IngestionSignal status={snapshot?.ingestion_status} />
            <MembershipSignal status={snapshot?.bot_membership_status} />
          </TechnicalDetails>

          <TechnicalDetails summary={t("dashboard.recentErrors")}>
            {recentErrors.length ? (
              <ul className="timeline-list">
                {recentErrors.slice(0, 5).map((item, index) => (
                  <li key={`${String(item.type)}-${index}`}><AlertTriangle aria-hidden="true" size={14} /><span>{String(item.message ?? item.type ?? t("dashboard.recentIssue"))}</span><small title={String(item.status ?? "")}>{item.status ? enumLabel(t, "status", String(item.status)) : ""}</small></li>
                ))}
              </ul>
            ) : <p className="detail-note">{t("dashboard.noRecentErrors")}</p>}
            <p className="detail-note">{snapshot?.health_issue_summary ? t("dashboard.healthIssues", { count: healthIssueCount }) : t("common.unknown")}</p>
          </TechnicalDetails>
        </aside>
      </div>
    </section>
  );
}

type DashboardNextStep = {
  title: string;
  detail: string;
  state: "attention" | "starting" | "waiting";
  route?: RouteKey;
  action?: string;
  showCommand?: boolean;
};

function dashboardNextStep(snapshot: DashboardSnapshot | undefined, error: unknown): DashboardNextStep | null {
  if (error || !snapshot) {
    return { title: "dashboard.unknownTitle", detail: "dashboard.unknownDetail", state: "attention", route: "health", action: "dashboard.nextHealth" };
  }
  if (snapshot.store_status?.available === false) {
    return { title: "dashboard.storeUnavailableTitle", detail: "dashboard.storeUnavailableDetail", state: "attention", route: "health", action: "dashboard.nextHealth" };
  }
  if (snapshot.policy_status?.initialized === false) {
    return { title: "dashboard.setupPolicyTitle", detail: "dashboard.setupPolicyDetail", state: "attention", route: "policy", action: "dashboard.setupOpenPolicy" };
  }
  const daemon = snapshot.daemon_liveness?.status;
  if (daemon === "startup_failed") {
    return { title: "dashboard.startupFailedTitle", detail: "dashboard.startupFailedDetail", state: "attention", route: "health", action: "dashboard.nextHealth" };
  }
  if (daemon === "stale") {
    return { title: "dashboard.staleTitle", detail: "dashboard.staleDetail", state: "attention", route: "health", action: "dashboard.nextHealth" };
  }
  if (daemon === "not_started") {
    return { title: "dashboard.notStartedTitle", detail: "dashboard.notStartedDetail", state: "attention", route: "maintenance", action: "dashboard.openMaintenance", showCommand: true };
  }
  if (daemon === "stopped") {
    return { title: "dashboard.stoppedTitle", detail: "dashboard.stoppedDetail", state: "attention", route: "health", action: "dashboard.nextHealth", showCommand: true };
  }
  if (daemon === "starting") {
    return { title: "dashboard.startingTitle", detail: "dashboard.startingDetail", state: "starting", route: "health", action: "dashboard.nextHealth" };
  }
  if (daemon !== "live" || snapshot.policy_status?.initialized !== true) {
    return { title: "dashboard.unknownTitle", detail: "dashboard.unknownDetail", state: "attention", route: "health", action: "dashboard.nextHealth" };
  }
  if (snapshot.health_issue_summary?.highest_severity === "error" || snapshot.health_issue_summary?.highest_severity === "critical") {
    return { title: "dashboard.healthAttentionTitle", detail: "dashboard.healthAttentionDetail", state: "attention", route: "health", action: "dashboard.nextHealth" };
  }
  if (snapshot.has_messages === false) {
    return { title: "dashboard.waitingMessageTitle", detail: "dashboard.waitingMessageDetail", state: "waiting", route: "tasks", action: "dashboard.openTasks" };
  }
  return null;
}

function NextStepPanel({ step, snapshot, navigate }: { step: DashboardNextStep; snapshot: DashboardSnapshot | undefined; navigate: (route: RouteKey) => void }) {
  const { t } = useTranslation();
  const mode = snapshot?.runtime_mode;
  const route = step.route;
  return (
    <section aria-label={t("dashboard.nextStep")} className="next-step-panel" data-attention={step.state === "attention"}>
      {step.state === "attention" ? <AlertTriangle aria-hidden="true" size={19} /> : <Info aria-hidden="true" size={19} />}
      <div className="next-step-copy">
        <h2>{t(step.title)}</h2>
        <p>{t(step.detail)}</p>
        {step.showCommand && mode ? (
          <TechnicalDetails summary={t("dashboard.startCommand")}>
            {mode === "production" ? <p>{t("dashboard.productionStartWarning")}</p> : null}
            <code>python -m feishu_shadow_agent daemon --config config.yaml{mode === "dry_run" ? " --dry-run" : ""}</code>
          </TechnicalDetails>
        ) : null}
      </div>
      {route && step.action ? <Button onClick={() => navigate(route)}>{t(step.action)}</Button> : null}
    </section>
  );
}

function MembershipSignal({ status }: { status: BotMembershipStatus | undefined }) {
  const { t } = useTranslation();
  const attention = status?.facts.filter((fact) => fact.status !== "present") ?? [];
  return (
    <section className="signal-section">
      <div className="detail-title-row">
        <h3>{t("dashboard.membershipTitle")}</h3>
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
    </section>
  );
}

function IngestionSignal({ status }: { status: IngestionStatus | undefined }) {
  const { t } = useTranslation();
  const summary = status?.summary;
  const backlogs = status?.sources.filter((source) => !source.drain_complete) ?? [];
  return (
    <section className="signal-section">
      <div className="detail-title-row">
        <h3>{t("dashboard.ingestionTitle")}</h3>
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
    </section>
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

function DecisionLink({ count, label, onClick, selected, tone }: {
  count: number;
  label: string;
  onClick: () => void;
  selected: boolean;
  tone: "danger" | "warning";
}) {
  return (
    <button aria-pressed={selected} className={`decision-link ${count ? tone : "quiet"}`} onClick={onClick} type="button">
      <span className="decision-label">{label}</span>
      <strong>{count}</strong>
    </button>
  );
}

type QueueEntry = {
  kind: QueueKind;
  id: string;
  taskId: string | null;
  title: string;
  meta: string;
  status: string;
  tone: Tone;
};

function ActionQueue({ filter, navigate, order, snapshot }: {
  filter: QueueFilter;
  navigate: Navigate;
  order: "items" | "tasks";
  snapshot: DashboardSnapshot;
}) {
  const { t } = useTranslation();
  const tasks = snapshot.attention_tasks ?? [];
  const actions = new Map(
    [...(snapshot.stale_sending_actions ?? []), ...(snapshot.failed_or_needs_review_actions ?? [])]
      .map((action) => [action.action_id, action])
  );
  const entries: QueueEntry[] = [
    ...[...actions.values()]
      .sort((left, right) => Number(left.status === "failed") - Number(right.status === "failed"))
      .slice(0, 6)
      .map((action): QueueEntry => ({
        kind: "dispatch",
        id: String(action.action_id),
        taskId: action.task_short_id,
        title: t("dashboard.dispatchRecord", { id: action.action_id }),
        meta: `${action.task_short_id ?? t("common.notLinked")} · ${formatDate(action.updated_at)}`,
        status: enumLabel(t, "status", action.status),
        tone: statusTone(action.status)
      })),
    ...(snapshot.pending_approvals ?? []).slice().sort((left, right) => Number(right.is_overdue) - Number(left.is_overdue))
      .slice(0, 6)
      .map((approval): QueueEntry => ({
        kind: "approval",
        id: approval.approval_id,
        taskId: approval.task_short_id,
        title: shortText(approval.preview, approval.approval_id),
        meta: `${approval.task_short_id ?? t("common.notLinked")} · ${formatDate(approval.created_at)}`,
        status: approval.is_overdue ? t("approvals.overdue") : t("dashboard.pendingApproval"),
        tone: approval.is_overdue ? "danger" : "warning"
      })),
    ...tasks.filter((task) => task.blocked_processing_count || task.failed_processing_count)
      .map((task): QueueEntry => ({
        kind: "processing",
        id: task.task_short_id,
        taskId: task.task_short_id,
        title: task.task_label || task.task_short_id,
        meta: formatDate(task.latest_at),
        status: t("dashboard.processingCount", { count: task.blocked_processing_count + task.failed_processing_count }),
        tone: "danger"
      }))
  ];
  const rows = entries.filter((row) => filter === "all" || row.kind === filter);

  function renderRows(items: QueueEntry[]) {
    return (
      <div className="list-stack">
        {items.map((row) => (
          <ListRow
            badge={<Badge tone={row.tone}>{row.status}</Badge>}
            key={`${row.kind}:${row.id}`}
            meta={row.meta}
            onClick={() => navigate(row.kind === "dispatch" ? "dispatch" : row.kind === "approval" ? "approvals" : "tasks", row.id)}
            selected={false}
            title={row.title}
          />
        ))}
      </div>
    );
  }

  if (order === "items") {
    return rows.length ? renderRows(rows) : <EmptyState title={t("dashboard.noQueuePreview")} />;
  }

  const groups = new Map<string | null, QueueEntry[]>();
  for (const row of rows) {
    const group = groups.get(row.taskId);
    if (group) group.push(row);
    else groups.set(row.taskId, [row]);
  }
  for (const task of tasks) {
    if (taskAttentionCount(task, filter) && !groups.has(task.task_short_id)) groups.set(task.task_short_id, []);
  }
  if (!groups.size) return <EmptyState title={t("dashboard.noQueuePreview")} />;

  return (
    <div className="list-stack">
      {[...groups].map(([taskId, items]) => {
        const task = tasks.find((candidate) => candidate.task_short_id === taskId);
        const title = task?.task_label || taskId || t("common.notLinked");
        const count = task ? Math.max(items.length, taskAttentionCount(task, filter)) : items.length;
        return (
          <TechnicalDetails key={taskId ?? "unlinked"} summary={`${title} · ${t("dashboard.itemCount", { count })}`}>
            {task ? (
              <p className="detail-note">
                {t("dashboard.taskMeta", { chat: task.chat_id ?? t("common.notRecorded"), time: formatDate(task.latest_at) })}
                {" · "}
                {t("dashboard.taskAttention", { approvals: task.pending_approval_count, uncertain: task.uncertain_action_count, failed: task.failed_action_count, processing: task.blocked_processing_count + task.failed_processing_count })}
              </p>
            ) : null}
            {taskId ? <Button onClick={() => navigate("tasks", taskId)}>{t("approvals.openTask")}</Button> : null}
            {items.length ? renderRows(items) : null}
          </TechnicalDetails>
        );
      })}
    </div>
  );
}

function taskAttentionCount(task: AttentionTask, filter: QueueFilter): number {
  const approvals = task.pending_approval_count;
  const dispatch = task.failed_action_count + task.uncertain_action_count;
  const processing = task.blocked_processing_count + task.failed_processing_count;
  if (filter === "approval") return approvals;
  if (filter === "dispatch") return dispatch;
  if (filter === "processing") return processing;
  return approvals + dispatch + processing;
}
