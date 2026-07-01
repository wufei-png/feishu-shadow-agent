import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, AlertTriangle, CheckCircle2, Database, ExternalLink, HeartPulse, Send } from "lucide-react";
import { getHealthIssues } from "../api";
import {
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
import type { HealthIssue, HealthIssueLink, HealthIssuesResponse, Tone } from "../types";

export function HealthScreen({ token }: { token: string }) {
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
    return <LoadingState title="Loading health issues" />;
  }
  if (health.error) {
    return <ErrorState title="Logs / Health unavailable" error={health.error} />;
  }
  if (!health.data) {
    return <EmptyState title="Health unavailable" detail="The local console did not return runtime health state." />;
  }

  return (
    <section className="work-grid health-layout" aria-label="Logs / Health">
      <div className="work-main">
        <HealthSummaryPanel data={health.data} refreshing={health.isFetching && !health.isLoading} />
        <div className="queue-panel">
          <SectionHeader
            eyebrow="Current Issues"
            title="What needs inspection"
            badge={<Badge tone={issues.length ? issueTone(health.data.summary.highest_severity) : "success"}>{issues.length}</Badge>}
          >
            <p className="section-note">Normalized runtime, policy, store, command, and dispatch issues from the local query boundary.</p>
          </SectionHeader>
          {issues.length ? (
            <div className="list-stack">
              {issues.map((issue) => (
                <ListRow
                  badge={<Badge tone={issueTone(issue.severity)}>{issue.severity}</Badge>}
                  key={issue.id}
                  meta={`${issue.category} · ${formatDate(issue.detected_at)}`}
                  onClick={() => setSelectedIssueId(issue.id)}
                  selected={issue.id === selectedIssue?.id}
                  title={issue.title}
                >
                  <span className="row-preview">{shortText(issue.detail, "No detail recorded")}</span>
                </ListRow>
              ))}
            </div>
          ) : (
            <EmptyState title="No open health issues" detail="Runtime liveness and store state remain visible for release checks." />
          )}
        </div>
      </div>

      <aside className="work-detail">
        <IssueDetailPanel issue={selectedIssue} />
        <RuntimePanel data={health.data} />
        <FailedCommandsPanel data={health.data} />
        <FailedDispatchPanel data={health.data} onSelect={setSelectedIssueId} />
      </aside>
    </section>
  );
}

function HealthSummaryPanel({ data, refreshing }: { data: HealthIssuesResponse; refreshing: boolean }) {
  return (
    <div className="queue-panel">
      <SectionHeader
        eyebrow="Logs / Health"
        title="Runtime diagnostics"
        badge={<Badge tone={refreshing ? "info" : data.summary.open_issue_count ? issueTone(data.summary.highest_severity) : "success"}>{refreshing ? "Refreshing" : data.summary.highest_severity}</Badge>}
      >
        <p className="section-note">This screen surfaces actionable health state. It does not expose raw logs or filesystem paths.</p>
      </SectionHeader>
      <div className="metric-row health-metrics">
        <Metric label="Open issues" value={String(data.summary.open_issue_count)} tone={data.summary.open_issue_count ? issueTone(data.summary.highest_severity) : "success"} />
        <Metric label="Store" value={String(data.runtime.store?.status ?? "unknown")} tone={data.runtime.store?.status === "available" ? "success" : "danger"} />
        <Metric label="Daemon" value={String(data.runtime.daemon_liveness?.status ?? "unknown")} tone={runtimeTone(data.runtime.daemon_liveness?.status)} />
        <Metric label="Generated" value={formatDate(data.generated_at)} tone="neutral" />
      </div>
    </div>
  );
}

function IssueDetailPanel({ issue }: { issue: HealthIssue | null }) {
  if (!issue) {
    return <EmptyState title="Select a health issue" detail="Details, object links, and available operator actions will appear here." />;
  }
  return (
    <div className="detail-panel health-issue-detail">
      <p className="eyebrow">Issue Detail</p>
      <div className="detail-title-row">
        <h2>{issue.title}</h2>
        <Badge tone={issueTone(issue.severity)}>{issue.severity}</Badge>
      </div>
      <FieldList>
        <FactRow label="Category" value={issue.category} />
        <FactRow label="Detected" value={formatDate(issue.detected_at)} />
      </FieldList>
      <p className="detail-note">{issue.detail}</p>
      {issue.recommended_actions.length ? (
        <div className="inline-badges" aria-label="Recommended actions">
          {issue.recommended_actions.map((action) => (
            <Badge key={action} tone="info">
              {action}
            </Badge>
          ))}
        </div>
      ) : null}
      <IssueLinks links={issue.links} />
    </div>
  );
}

function IssueLinks({ links }: { links: HealthIssueLink[] }) {
  if (!links.length) {
    return (
      <div className="quiet-empty">
        <HeartPulse aria-hidden="true" size={18} />
        <span>No linked console object.</span>
      </div>
    );
  }
  return (
    <div className="health-link-list">
      {links.map((link) => {
        const target = linkTarget(link);
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
  const liveness = data.runtime.daemon_liveness ?? {};
  const lastRun = data.runtime.last_run ?? {};
  return (
    <div className="detail-panel">
      <p className="eyebrow">Runtime Liveness</p>
      <h2>Daemon and store state</h2>
      <FieldList>
        <FactRow label="Daemon status" value={String(liveness.status ?? "unknown")} />
        <FactRow label="Heartbeat age" value={formatSeconds(liveness.heartbeat_age_seconds)} />
        <FactRow label="Run status" value={String(liveness.run_status ?? lastRun.status ?? "not recorded")} />
        <FactRow label="Last tick" value={formatDate(String(lastRun.last_tick_finished_at ?? ""))} />
        <FactRow label="Store available" value={data.runtime.store?.available ? "yes" : "no"} />
      </FieldList>
    </div>
  );
}

function FailedCommandsPanel({ data }: { data: HealthIssuesResponse }) {
  const commands = data.recent_failed_commands ?? [];
  return (
    <div className="detail-panel">
      <p className="eyebrow">Failed Commands</p>
      <h2>Recent approval command failures</h2>
      {commands.length ? (
        <ul className="timeline-list">
          {commands.slice(0, 5).map((command, index) => (
            <li key={`${String(command.message_id ?? "command")}-${index}`}>
              <AlertTriangle aria-hidden="true" size={14} />
              <span>{String(command.label ?? "approval command")}</span>
              <small>{String(command.status ?? "failed")}</small>
            </li>
          ))}
        </ul>
      ) : (
        <div className="quiet-empty">
          <CheckCircle2 aria-hidden="true" size={18} />
          <span>No recent failed approval commands.</span>
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
  const dispatchIssues = data.issues.filter((issue) => issue.category === "dispatch");
  const failedActions = data.recent_failed_dispatch_actions ?? [];
  const issueActionIds = new Set(
    dispatchIssues.flatMap((issue) => issue.links.filter((link) => link.type === "dispatch_action").map((link) => link.id))
  );
  const actionSummaries = failedActions.filter((action) => !issueActionIds.has(String(action.action_id)));
  return (
    <div className="detail-panel">
      <p className="eyebrow">Dispatch Recovery</p>
      <h2>Failed action summaries</h2>
      {dispatchIssues.length || actionSummaries.length ? (
        <ul className="timeline-list">
          {dispatchIssues.slice(0, 6).map((issue) => (
            <li key={issue.id}>
              <button className="timeline-button" onClick={() => onSelect(issue.id)} type="button">
                <Send aria-hidden="true" size={14} />
                <span>{issue.title}</span>
                <small>{issue.severity}</small>
              </button>
            </li>
          ))}
          {actionSummaries.slice(0, 6).map((action) => (
            <li key={`dispatch-action-summary-${action.action_id}`}>
              <a className="timeline-button" href={`#dispatch/${encodeURIComponent(String(action.action_id))}`}>
                <Send aria-hidden="true" size={14} />
                <span>{`Action ${action.action_id}`}</span>
                <small>{action.status}</small>
              </a>
            </li>
          ))}
        </ul>
      ) : (
        <div className="quiet-empty">
          <CheckCircle2 aria-hidden="true" size={18} />
          <span>No failed dispatch actions.</span>
        </div>
      )}
    </div>
  );
}

function Metric({ label, value, tone }: { label: string; value: string; tone: Tone }) {
  return (
    <div className={`metric-card ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function FactRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function linkTarget(link: HealthIssueLink): { label: string; href: string | null } {
  if (link.type === "dispatch_action") {
    return { label: `Open action ${link.id}`, href: `#dispatch/${encodeURIComponent(link.id)}` };
  }
  if (link.type === "approval") {
    return { label: `Open approval ${link.id}`, href: `#approvals/${encodeURIComponent(link.id)}` };
  }
  if (link.type === "task") {
    return { label: `Open task ${link.id}`, href: `#tasks/${encodeURIComponent(link.id)}` };
  }
  if (link.type === "policy") {
    return { label: "Open Policy", href: "#policy" };
  }
  if (link.type === "settings") {
    return { label: "Open Settings", href: "#settings" };
  }
  return { label: "Message detail opens from Tasks", href: null };
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

function formatSeconds(value: unknown): string {
  return typeof value === "number" ? `${value}s` : "not recorded";
}
