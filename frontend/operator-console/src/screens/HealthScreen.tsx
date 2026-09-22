import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, ExternalLink, HeartPulse, Send } from "lucide-react";
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
    return <LoadingState title="正在读取健康问题" />;
  }
  if (health.error) {
    return <ErrorState title="无法读取健康状态" error={health.error} />;
  }
  if (!health.data) {
    return <EmptyState title="健康状态暂不可用" detail="本地控制台没有返回运行健康状态。" />;
  }

  return (
    <section className="work-grid health-layout" aria-label="健康">
      <div className="work-main">
        <HealthSummaryPanel data={health.data} refreshing={health.isFetching && !health.isLoading} />
        <div className="queue-panel">
          <SectionHeader
            eyebrow="当前问题"
            title="需要检查的事项"
            badge={<Badge tone={issues.length ? issueTone(health.data.summary.highest_severity) : "success"}>{issues.length}</Badge>}
          >
            <p className="section-note">汇总运行、策略、存储和发送问题，便于逐项检查。</p>
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
                  <span className="row-preview">{shortText(issue.detail, "未记录详情")}</span>
                </ListRow>
              ))}
            </div>
          ) : (
            <EmptyState title="当前没有健康问题" detail="仍可在右侧查看守护进程和存储状态。" />
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
        eyebrow="健康状态"
        title="运行诊断"
        badge={<Badge tone={refreshing ? "info" : data.summary.open_issue_count ? issueTone(data.summary.highest_severity) : "success"}>{refreshing ? "刷新中" : data.summary.highest_severity}</Badge>}
      >
        <p className="section-note">这里显示可处理的健康问题和运行状态。</p>
      </SectionHeader>
      <div className="metric-row health-metrics">
        <Metric label="未解决问题" value={String(data.summary.open_issue_count)} tone={data.summary.open_issue_count ? issueTone(data.summary.highest_severity) : "success"} />
        <Metric label="存储" value={String(data.runtime.store?.status ?? "unknown")} tone={data.runtime.store?.status === "available" ? "success" : "danger"} />
        <Metric label="守护进程" value={String(data.runtime.daemon_liveness?.status ?? "unknown")} tone={runtimeTone(data.runtime.daemon_liveness?.status)} />
        <Metric label="生成时间" value={formatDate(data.generated_at)} tone="neutral" />
      </div>
    </div>
  );
}

function IssueDetailPanel({ issue }: { issue: HealthIssue | null }) {
  if (!issue) {
    return <EmptyState title="选择一个健康问题" detail="此处会显示详情、关联对象和可用操作。" />;
  }
  return (
    <div className="detail-panel health-issue-detail">
      <p className="eyebrow">问题详情</p>
      <div className="detail-title-row">
        <h2>{issue.title}</h2>
        <Badge tone={issueTone(issue.severity)}>{issue.severity}</Badge>
      </div>
      <FieldList>
        <FactRow label="类别" value={issue.category} />
        <FactRow label="发现时间" value={formatDate(issue.detected_at)} />
      </FieldList>
      <p className="detail-note">{issue.detail}</p>
      {issue.recommended_actions.length ? (
        <div className="inline-badges" aria-label="建议操作">
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
        <span>没有关联的控制台对象。</span>
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
      <p className="eyebrow">运行活性</p>
      <h2>守护进程与存储</h2>
      <FieldList>
        <FactRow label="守护进程状态" value={String(liveness.status ?? "unknown")} />
        <FactRow label="上次心跳" value={formatSeconds(liveness.heartbeat_age_seconds)} />
        <FactRow label="运行状态" value={String(liveness.run_status ?? lastRun.status ?? "未记录")} />
        <FactRow label="最近 tick" value={formatDate(String(lastRun.last_tick_finished_at ?? ""))} />
        <FactRow label="存储可用" value={data.runtime.store?.available ? "是" : "否"} />
      </FieldList>
    </div>
  );
}

function FailedCommandsPanel({ data }: { data: HealthIssuesResponse }) {
  const commands = data.recent_failed_commands ?? [];
  return (
    <div className="detail-panel">
      <p className="eyebrow">失败命令</p>
      <h2>最近失败的审批操作</h2>
      {commands.length ? (
        <ul className="timeline-list">
          {commands.slice(0, 5).map((command, index) => (
            <li key={`${String(command.message_id ?? "command")}-${index}`}>
              <AlertTriangle aria-hidden="true" size={14} />
              <span>{String(command.label ?? "审批操作")}</span>
              <small>{String(command.status ?? "failed")}</small>
            </li>
          ))}
        </ul>
      ) : (
        <div className="quiet-empty">
          <CheckCircle2 aria-hidden="true" size={18} />
          <span>近期没有失败的审批操作。</span>
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
      <p className="eyebrow">发送恢复</p>
      <h2>失败发送摘要</h2>
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
                <span>{`发送记录 ${action.action_id}`}</span>
                <small>{action.status}</small>
              </a>
            </li>
          ))}
        </ul>
      ) : (
        <div className="quiet-empty">
          <CheckCircle2 aria-hidden="true" size={18} />
          <span>没有失败的发送记录。</span>
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
    return { label: `查看发送记录 ${link.id}`, href: `#dispatch/${encodeURIComponent(link.id)}` };
  }
  if (link.type === "approval") {
    return { label: `查看审批 ${link.id}`, href: `#approvals/${encodeURIComponent(link.id)}` };
  }
  if (link.type === "task") {
    return { label: `查看任务 ${link.id}`, href: `#tasks/${encodeURIComponent(link.id)}` };
  }
  if (link.type === "policy") {
    return { label: "打开策略", href: "#policy" };
  }
  if (link.type === "settings") {
    return { label: "打开设置", href: "#settings" };
  }
  return { label: "请从任务页打开消息详情", href: null };
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
  return typeof value === "number" ? `${value} 秒` : "未记录";
}
