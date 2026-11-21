import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { AlertTriangle, CheckCircle2, ExternalLink, HeartPulse, Send } from "lucide-react";
import { useTranslation } from "react-i18next";
import { getHealthIssues } from "../api";
import {
  QueueControls,
  Fact,
  Badge,
  EmptyState,
  ErrorState,
  FieldList,
  formatDate,
  ListRow,
  LoadingState,
  SectionHeader,
  shortText
} from "../components/Primitives";
import { queryKeys } from "../queryKeys";
import { enumLabel } from "../presentation";
import type { HealthIssue, HealthIssueLink, HealthIssuesResponse, Tone } from "../types";

export function HealthScreen({ token }: { token: string }) {
  const { t } = useTranslation();
  const [selectedIssueId, setSelectedIssueId] = useState<string | null>(null);
  const health = useQuery({
    queryKey: queryKeys.healthIssues(),
    queryFn: () => getHealthIssues(token),
    enabled: Boolean(token),
    refetchInterval: 15_000
  });
  const issues = useMemo(() => health.data?.issues ?? [], [health.data]);
  const selectedIssue = issues.find((issue) => issue.id === selectedIssueId) ?? issues[0] ?? null;

  useEffect(() => {
    if (!issues.length) {
      setSelectedIssueId(null);
      return;
    }
    if (selectedIssueId === null || !issues.some((issue) => issue.id === selectedIssueId)) {
      setSelectedIssueId(issues[0].id);
    }
  }, [issues, selectedIssueId]);

  if (health.isLoading) {
    return <LoadingState title={t("health.loading")} />;
  }
  if (health.error) {
    return <ErrorState title={t("health.error")} error={health.error} />;
  }
  if (!health.data) {
    return <EmptyState title={t("health.unavailable")} detail={t("health.unavailableDetail")} />;
  }
  const { open_issue_count: openIssueCount, highest_severity: highestSeverity } = health.data.summary;

  return (
    <section className="work-grid health-layout" aria-label={t("health.aria")}>
      <div className="work-main">
        <div className="queue-panel">
          <SectionHeader
            title={t("health.diagnostics")}
            badge={openIssueCount ? (
              <Badge tone={issueTone(highestSeverity)}>{t("health.openIssueCount", { count: openIssueCount })}</Badge>
            ) : null}
          />
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
                  meta={`${enumLabel(t, "category", issue.category)} · ${formatDate(issue.detected_at)}`}
                  onClick={() => setSelectedIssueId(issue.id)}
                  selected={issue.id === selectedIssue?.id}
                  title={issueCopy(t, issue).title}
                >
                  <span className="row-preview">{shortText(issueCopy(t, issue).detail, t("health.noDetail"))}</span>
                </ListRow>
              ))}
            </div>
          ) : (
            <EmptyState title={t("health.noIssues")} />
          )}
        </div>
      </div>

      <aside className="work-detail">
        {selectedIssue ? <IssueDetailPanel issue={selectedIssue} /> : null}
        <RuntimePanel data={health.data} />
        <FailedCommandsPanel data={health.data} />
        <FailedDispatchPanel data={health.data} onSelect={setSelectedIssueId} />
      </aside>
    </section>
  );
}

function IssueDetailPanel({ issue }: { issue: HealthIssue }) {
  const { t } = useTranslation();
  const copy = issueCopy(t, issue);
  return (
    <div className="detail-panel health-issue-detail">
      <p className="eyebrow">{t("health.issueDetail")}</p>
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
      {issue.recommended_actions.length ? (
        <div className="inline-badges" aria-label={t("health.actions")}>
          {issue.recommended_actions.map((action) => (
            <Badge key={action} tone="info">
              {enumLabel(t, "action", action)}
            </Badge>
          ))}
        </div>
      ) : null}
      <IssueLinks links={issue.links} />
    </div>
  );
}

function IssueLinks({ links }: { links: HealthIssueLink[] }) {
  const { t } = useTranslation();
  if (!links.length) {
    return (
      <div className="quiet-empty">
        <HeartPulse aria-hidden="true" size={18} />
        <span>{t("health.noLinks")}</span>
      </div>
    );
  }
  return (
    <div className="health-link-list">
      {links.map((link) => {
        const target = linkTarget(t, link);
        if (!target.href) {
          return (
            <span className="button disabled-action" key={`${link.type}-${link.id}`}>
              {target.label}
            </span>
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
    <div className="detail-panel">
      <h2>{t("health.runtimeOverview")}</h2>
      <dl className="overview-facts">
        <Fact label={t("health.daemon")} value={<Badge tone={runtimeTone(daemonStatus)}>{enumLabel(t, "status", daemonStatus)}</Badge>} />
        <Fact label={t("health.storage")} value={<Badge tone={storageStatus === "available" ? "success" : storageStatus === "unknown" ? "muted" : "danger"}>{enumLabel(t, "status", storageStatus)}</Badge>} />
        <Fact label={t("health.lastHeartbeat")} value={formatSeconds(t, liveness.heartbeat_age_seconds)} />
        <Fact label={t("health.runStatus")} value={enumLabel(t, "status", String(liveness.run_status ?? lastRun.status ?? "unknown"))} />
        <Fact label={t("health.lastTick")} value={formatDate(String(lastRun.last_tick_finished_at ?? ""))} />
        <Fact label={t("health.generatedAt")} value={formatDate(data.generated_at)} />
      </dl>
    </div>
  );
}

function FailedCommandsPanel({ data }: { data: HealthIssuesResponse }) {
  const { t } = useTranslation();
  const commands = data.recent_failed_commands ?? [];
  return (
    <div className="detail-panel">
      <p className="eyebrow">{t("health.failedCommands")}</p>
      <h2>{t("health.failedApprovals")}</h2>
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
    </div>
  );
}

function FailedDispatchPanel({
  data,
  onSelect
}: {
  data: HealthIssuesResponse;
  onSelect: (issueId: string) => void;
}) {
  const { t } = useTranslation();
  const dispatchIssues = data.issues.filter((issue) => issue.category === "dispatch");
  const failedActions = data.recent_failed_dispatch_actions ?? [];
  const issueActionIds = new Set(
    dispatchIssues.flatMap((issue) => issue.links.filter((link) => link.type === "dispatch_action").map((link) => link.id))
  );
  const actionSummaries = failedActions.filter((action) => !issueActionIds.has(String(action.action_id)));
  return (
    <div className="detail-panel">
      <p className="eyebrow">{t("health.dispatchRecovery")}</p>
      <h2>{t("health.failedDispatch")}</h2>
      {dispatchIssues.length || actionSummaries.length ? (
        <ul className="timeline-list">
          {dispatchIssues.slice(0, 6).map((issue) => (
            <li key={issue.id}>
              <button className="timeline-button" onClick={() => onSelect(issue.id)} type="button">
                <Send aria-hidden="true" size={14} />
                <span>{issueCopy(t, issue).title}</span>
                <small>{enumLabel(t, "severity", issue.severity)}</small>
              </button>
            </li>
          ))}
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
    </div>
  );
}

function linkTarget(t: TFunction, link: HealthIssueLink): { label: string; href: string | null } {
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
  return { label: t("health.openMessageFromTask"), href: null };
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
