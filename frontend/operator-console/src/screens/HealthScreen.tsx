import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { AlertTriangle, ArrowLeft, CheckCircle2, ExternalLink, Send } from "lucide-react";
import { useTranslation } from "react-i18next";
import { getHealthIssues } from "../api";
import {
  QueueControls,
  Fact,
  Badge,
  CopyValue,
  EmptyState,
  ErrorState,
  FieldList,
  formatDate,
  ListRow,
  LoadingState,
  TechnicalDetails
} from "../components/Primitives";
import { useWorkbenchNavigation } from "../components/useWorkbenchNavigation";
import { queryKeys } from "../queryKeys";
import { enumLabel } from "../presentation";
import type { HealthIssue, HealthIssueLink, HealthIssuesResponse, Tone } from "../types";

export function HealthScreen({ token, selectedId }: { token: string; selectedId: string | null }) {
  const { t } = useTranslation();
  const [openedIssueId, setOpenedIssueId] = useState(selectedId);
  const pane = openedIssueId ? "detail" : "list";
  const { heading, rememberPosition } = useWorkbenchNavigation(pane);
  const health = useQuery({
    queryKey: queryKeys.healthIssues(),
    queryFn: () => getHealthIssues(token),
    enabled: Boolean(token),
    refetchInterval: 15_000
  });
  const issues = useMemo(() => health.data?.issues ?? [], [health.data]);
  const selectedIssue = openedIssueId ? issues.find((issue) => issue.id === openedIssueId) : issues[0];

  useEffect(() => {
    setOpenedIssueId(selectedId);
  }, [selectedId]);

  function openIssue(issueId: string | null): void {
    setOpenedIssueId(issueId);
    window.location.hash = issueId ? `health/${encodeURIComponent(issueId)}` : "health";
  }

  const data = health.data;
  const openIssueCount = data?.summary.open_issue_count;
  const highestSeverity = data?.summary.highest_severity;

  return (
    <section className="workbench" data-pane={pane} aria-label={t("health.aria")}>
      <header className="workbench-header">
        <h1 ref={heading} tabIndex={-1}>{t("health.diagnostics")}</h1>
        {openedIssueId ? (
          <button className="button workbench-back" onClick={() => openIssue(null)} type="button">
            <ArrowLeft aria-hidden="true" size={16} />
            {t("health.backToIssues")}
          </button>
        ) : null}
      </header>
      {health.isLoading ? <LoadingState title={t("health.loading")} /> : null}
      {health.error && !data ? <ErrorState title={t("health.error")} error={health.error} /> : null}
      <div className="work-grid health-layout">
        <div className="work-main workbench-pane" data-pane="list">
          <div className="queue-panel">
            {openIssueCount ? <Badge tone={issueTone(highestSeverity)}>{t("health.openIssueCount", { count: openIssueCount })}</Badge> : null}
            <QueueControls
              error={health.error}
              isFetching={health.isFetching}
              onRefresh={() => void health.refetch()}
              updatedAt={health.dataUpdatedAt}
            />
            {issues.length ? (
              <div className="list-stack">
                {issues.map((issue) => (
                  <ListRow
                    badge={<Badge tone={issueTone(issue.severity)}>{enumLabel(t, "severity", issue.severity)}</Badge>}
                    key={issue.id}
                    onClick={(event) => {
                      rememberPosition(event.currentTarget, "list");
                      openIssue(issue.id);
                    }}
                    selected={issue.id === selectedIssue?.id}
                    title={issueCopy(t, issue).title}
                  />
                ))}
              </div>
            ) : data ? (
              <EmptyState title={t("health.noIssues")} />
            ) : null}
          </div>
          {data ? <RuntimePanel data={data} /> : null}
          {data ? <FailedCommandsPanel data={data} /> : null}
          {data ? <FailedDispatchPanel data={data} /> : null}
        </div>

        <aside className="work-detail workbench-pane" data-pane="detail">
          {selectedIssue ? <IssueDetailPanel issue={selectedIssue} /> : openedIssueId && data ? <EmptyState title={t("health.issueResolved")} /> : null}
          {openedIssueId && health.error ? (
            <QueueControls error={health.error} isFetching={health.isFetching} onRefresh={() => void health.refetch()} updatedAt={health.dataUpdatedAt} />
          ) : null}
        </aside>
      </div>
    </section>
  );
}

function IssueDetailPanel({ issue }: { issue: HealthIssue }) {
  const { t } = useTranslation();
  const copy = issueCopy(t, issue);
  return (
    <div className="detail-panel health-issue-detail">
      <div className="detail-title-row">
        <h2>{copy.title}</h2>
        <Badge tone={issueTone(issue.severity)}>{enumLabel(t, "severity", issue.severity)}</Badge>
      </div>
      <FieldList>
        <Fact label={t("health.category")} value={enumLabel(t, "category", issue.category)} />
        <Fact label={t("health.detectedAt")} value={formatDate(issue.detected_at)} />
      </FieldList>
      <p className="detail-note">{copy.detail}</p>
      {copy.localized ? (
        <details className="technical-details">
          <summary>{t("health.technicalDetails")}</summary>
          <p className="detail-note">{issue.title}: {issue.detail}</p>
        </details>
      ) : null}
      <IssueLinks links={issue.links} />
      {issue.recommended_actions.length ? (
        <ul className="warning-list" aria-label={t("health.actions")}>
          {issue.recommended_actions.map((action) => {
            const href = action === "run_doctor" ? "#maintenance" : action === "inspect_settings" ? "#settings" : action === "inspect_policy" || action === "import_config" ? "#policy" : undefined;
            const label = enumLabel(t, "action", action);
            return <li key={action}>{href ? <a href={href}>{label}</a> : action === "inspect_runtime" ? t("health.inspectRuntimeStep") : label}</li>;
          })}
        </ul>
      ) : null}
    </div>
  );
}

function IssueLinks({ links }: { links: HealthIssueLink[] }) {
  const { t } = useTranslation();
  if (!links.length) return null;
  const task = links.find((link) => link.type === "task");
  return (
    <div className="health-link-list">
      {links.map((link) => {
        const target = linkTarget(t, link, task?.id);
        if (!target.href) {
          return (
            <div className="detail-note" key={`${link.type}-${link.id}`}>
              {target.label} <CopyValue label={t("message.messageId")} value={link.id} />
            </div>
          );
        }
        return (
          <a className="button info" href={target.href} key={`${link.type}-${link.id}`}>
            <ExternalLink aria-hidden="true" size={15} />
            {target.label}
          </a>
        );
      })}
    </div>
  );
}

function RuntimePanel({ data }: { data: HealthIssuesResponse }) {
  const { t } = useTranslation();
  const liveness = data.runtime.daemon_liveness ?? {};
  const lastRun = data.runtime.last_run ?? {};
  const store = data.runtime.store;
  const daemonStatus = String(liveness.status ?? "unknown");
  const storageStatus = String(store?.status ?? (store?.available === true ? "available" : store?.available === false ? "unavailable" : "unknown"));
  return (
    <TechnicalDetails summary={t("health.runtimeOverview")}>
      <dl className="overview-facts">
        <Fact label={t("health.daemon")} value={<Badge tone={runtimeTone(daemonStatus)}>{enumLabel(t, "status", daemonStatus)}</Badge>} />
        <Fact label={t("health.storage")} value={<Badge tone={storageStatus === "available" ? "success" : storageStatus === "unknown" ? "muted" : "danger"}>{enumLabel(t, "status", storageStatus)}</Badge>} />
        <Fact label={t("health.lastHeartbeat")} value={formatSeconds(t, liveness.heartbeat_age_seconds)} />
        <Fact label={t("health.runStatus")} value={enumLabel(t, "status", String(liveness.run_status ?? lastRun.status ?? "unknown"))} />
        <Fact label={t("health.lastTick")} value={formatDate(String(lastRun.last_tick_finished_at ?? ""))} />
        <Fact label={t("health.generatedAt")} value={formatDate(data.generated_at)} />
      </dl>
    </TechnicalDetails>
  );
}

function FailedCommandsPanel({ data }: { data: HealthIssuesResponse }) {
  const { t } = useTranslation();
  const commands = data.recent_failed_commands ?? [];
  return (
    <TechnicalDetails summary={t("health.failedApprovals")}>
      {commands.length ? (
        <ul className="timeline-list">
          {commands.slice(0, 5).map((command, index) => (
            <li key={`${String(command.message_id ?? "command")}-${index}`}>
              <AlertTriangle aria-hidden="true" size={14} />
              <span>{String(command.label ?? t("health.approvalAction"))}</span>
              <small>{enumLabel(t, "status", String(command.status ?? "failed"))}</small>
            </li>
          ))}
        </ul>
      ) : (
        <div className="quiet-empty">
          <CheckCircle2 aria-hidden="true" size={18} />
          <span>{t("health.noFailedApprovals")}</span>
        </div>
      )}
    </TechnicalDetails>
  );
}

function FailedDispatchPanel({ data }: { data: HealthIssuesResponse }) {
  const { t } = useTranslation();
  const dispatchIssues = data.issues.filter((issue) => issue.category === "dispatch");
  const failedActions = data.recent_failed_dispatch_actions ?? [];
  const issueActionIds = new Set(
    dispatchIssues.flatMap((issue) => issue.links.filter((link) => link.type === "dispatch_action").map((link) => link.id))
  );
  const actionSummaries = failedActions.filter((action) => !issueActionIds.has(String(action.action_id)));
  return (
    <TechnicalDetails summary={t("health.failedDispatch")}>
      {actionSummaries.length ? (
        <ul className="timeline-list">
          {actionSummaries.slice(0, 6).map((action) => (
            <li key={`dispatch-action-summary-${action.action_id}`}>
              <a className="timeline-button" href={`#dispatch/${encodeURIComponent(String(action.action_id))}`}>
                <Send aria-hidden="true" size={14} />
                <span>{t("health.dispatchRecord", { id: action.action_id })}</span>
                <small>{enumLabel(t, "status", action.status)}</small>
              </a>
            </li>
          ))}
        </ul>
      ) : (
        <div className="quiet-empty">
          <CheckCircle2 aria-hidden="true" size={18} />
          <span>{t("health.noFailedDispatch")}</span>
        </div>
      )}
    </TechnicalDetails>
  );
}

function linkTarget(t: TFunction, link: HealthIssueLink, taskId?: string): { label: string; href: string | null } {
  if (link.type === "dispatch_action") {
    return { label: t("health.viewDispatch", { id: link.id }), href: `#dispatch/${encodeURIComponent(link.id)}` };
  }
  if (link.type === "approval") {
    return { label: t("health.viewApproval", { id: link.id }), href: `#approvals/${encodeURIComponent(link.id)}` };
  }
  if (link.type === "task") {
    return { label: t("health.viewTask", { id: link.id }), href: `#tasks/${encodeURIComponent(link.id)}` };
  }
  if (link.type === "policy") {
    return { label: t("health.openPolicy"), href: "#policy" };
  }
  if (link.type === "settings") {
    return { label: t("health.openSettings"), href: "#settings" };
  }
  return taskId
    ? { label: t("tasks.messageDetail"), href: `#tasks/${encodeURIComponent(taskId)}?message=${encodeURIComponent(link.id)}` }
    : { label: t("health.openMessageFromTask"), href: null };
}

function issueCopy(t: TFunction, issue: HealthIssue): { title: string; detail: string; localized: boolean } {
  const exact: Record<string, [string, string]> = {
    "store-read-error": ["health.issue.storeRead.title", "health.issue.storeRead.detail"],
    "daemon-not-started": ["health.issue.daemonNotStarted.title", "health.issue.daemonNotStarted.detail"],
    "daemon-startup-failed": ["health.issue.daemonStartupFailed.title", "health.issue.daemonStartupFailed.detail"],
    "daemon-stale": ["health.issue.daemonStale.title", "health.issue.daemonStale.detail"],
    "daemon-stopped": ["health.issue.daemonStopped.title", "health.issue.daemonStopped.detail"],
    "daemon-unknown": ["health.issue.daemonUnknown.title", "health.issue.daemonUnknown.detail"],
    "policy-uninitialized": ["health.issue.policyUninitialized.title", "health.issue.policyUninitialized.detail"],
    "policy-invalid": ["health.issue.policyInvalid.title", "health.issue.policyInvalid.detail"]
  };
  const keys = exact[issue.id] ??
    (issue.id.startsWith("store-") ? ["health.issue.storeUnavailable.title", "health.issue.storeUnavailable.detail"] as [string, string] : null) ??
    (issue.id.startsWith("dispatch-action-") ? ["health.issue.dispatch.title", "health.issue.dispatch.detail"] as [string, string] : null) ??
    (issue.id.startsWith("health-") ? ["health.issue.runtime.title", "health.issue.runtime.detail"] as [string, string] : null);
  return keys
    ? { title: t(keys[0]), detail: t(keys[1]), localized: true }
    : { title: issue.title, detail: issue.detail, localized: false };
}

function issueTone(severity: string | null | undefined): Tone {
  if (severity === "critical" || severity === "error") {
    return "danger";
  }
  if (severity === "warning") {
    return "warning";
  }
  if (severity === "info") {
    return "info";
  }
  return "neutral";
}

function runtimeTone(status: unknown): Tone {
  if (status === "live" || status === "available") {
    return "success";
  }
  if (status === "not_started" || status === "stopped") {
    return "warning";
  }
  if (status === "stale" || status === "missing" || status === "unreadable") {
    return "danger";
  }
  return "muted";
}

function formatSeconds(t: TFunction, value: unknown): string {
  return typeof value === "number" ? t("common.seconds", { count: value }) : t("common.notRecorded");
}
