import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { MessageSquareDiff } from "lucide-react";
import { getFeedbackOverview } from "../api";
import {
  Badge,
  EmptyState,
  ErrorState,
  FieldList,
  formatDate,
  ListRow,
  LoadingState,
  SectionHeader,
  SegmentedControl,
  shortText
} from "../components/Primitives";
import { queryKeys } from "../queryKeys";
import type {
  FeedbackCount,
  FeedbackExecutionMode,
  FeedbackMetrics,
  FeedbackRecord,
  ReplyDiffPart,
  Tone
} from "../types";

type WindowDays = 7 | 30;
type OutcomeFilter = "all" | FeedbackRecord["outcome"];

export function FeedbackScreen({ token }: { token: string }) {
  const [executionMode, setExecutionMode] = useState<FeedbackExecutionMode>("production");
  const [windowDays, setWindowDays] = useState<WindowDays>(30);
  const [outcomeFilter, setOutcomeFilter] = useState<OutcomeFilter>("all");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const overview = useQuery({
    queryKey: queryKeys.feedbackOverview(executionMode),
    queryFn: () => getFeedbackOverview(token, executionMode),
    enabled: Boolean(token),
    refetchInterval: 30_000
  });
  const metrics = overview.data?.windows.find((item) => item.days === windowDays) ?? null;
  const records = useMemo(
    () =>
      (overview.data?.recent ?? []).filter(
        (item) => outcomeFilter === "all" || item.outcome === outcomeFilter
      ),
    [overview.data?.recent, outcomeFilter]
  );
  const selected = records.find((item) => item.id === selectedId) ?? records[0] ?? null;

  useEffect(() => {
    if (!records.length) {
      setSelectedId(null);
      return;
    }
    if (selectedId === null || !records.some((item) => item.id === selectedId)) {
      setSelectedId(records[0].id);
    }
  }, [records, selectedId]);

  if (overview.isLoading) {
    return <LoadingState title="Loading feedback" />;
  }
  if (overview.error) {
    return <ErrorState title="Feedback unavailable" error={overview.error} />;
  }
  if (!overview.data) {
    return <EmptyState title="Feedback unavailable" detail="The local console did not return feedback metrics." />;
  }

  return (
    <section className="work-grid feedback-layout" aria-label="Feedback">
      <div className="work-main">
        <div className="queue-panel">
          <SectionHeader
            eyebrow="Feedback"
            title="Human resolution outcomes"
            badge={<Badge tone={executionMode === "production" ? "success" : "info"}>{executionMode}</Badge>}
          >
            <p className="section-note">
              Metrics are immutable owner outcomes. Production is the default so dry-run rehearsals never inflate adoption rates.
            </p>
          </SectionHeader>
          <div className="feedback-controls">
            <SegmentedControl
              label="Execution mode"
              value={executionMode}
              onChange={setExecutionMode}
              options={[
                { value: "production", label: "Production" },
                { value: "dry_run", label: "Dry run" },
                { value: "all", label: "All" }
              ]}
            />
            <SegmentedControl
              label="Metric window"
              value={String(windowDays)}
              onChange={(value) => setWindowDays(value === "7" ? 7 : 30)}
              options={[
                { value: "7", label: "7 days" },
                { value: "30", label: "30 days" }
              ]}
            />
          </div>
          {metrics ? <MetricSummary metrics={metrics} /> : null}
        </div>

        <div className="split-panels feedback-breakdowns">
          <Breakdown title="Decision reasons" values={metrics?.by_decision_reason ?? []} />
          <Breakdown title="Feedback reasons" values={metrics?.by_feedback_reason ?? []} />
        </div>

        <div className="queue-panel">
          <SectionHeader
            eyebrow="Recent resolutions"
            title="Outcome history"
            badge={<Badge tone="muted">{records.length}</Badge>}
          />
          <SegmentedControl
            label="Outcome filter"
            value={outcomeFilter}
            onChange={setOutcomeFilter}
            options={[
              { value: "all", label: "All" },
              { value: "suggestion_sent", label: "Sent" },
              { value: "edited_sent", label: "Edited" },
              { value: "no_send_keep_watching", label: "Keep watching" },
              { value: "no_send_end_task", label: "Ended" }
            ]}
          />
          {records.length ? (
            <div className="list-stack">
              {records.map((record) => (
                <ListRow
                  badge={<Badge tone={outcomeTone(record.outcome)}>{outcomeLabel(record.outcome)}</Badge>}
                  key={record.id}
                  meta={`${record.approval_id} · ${formatDate(record.created_at)}`}
                  onClick={() => setSelectedId(record.id)}
                  selected={record.id === selected?.id}
                  title={record.task_id ?? "Detached task"}
                >
                  <span className="row-preview">
                    {shortText(record.reply_comparison.final_reply ?? record.reply_comparison.suggested_reply, outcomeLabel(record.outcome))}
                  </span>
                </ListRow>
              ))}
            </div>
          ) : (
            <EmptyState title="No feedback in this view" detail="Resolved approvals appear here after the owner records an outcome." />
          )}
        </div>
      </div>

      <aside className="work-detail">
        <FeedbackDetail record={selected} />
      </aside>
    </section>
  );
}

function MetricSummary({ metrics }: { metrics: FeedbackMetrics }) {
  return (
    <div className="metric-row feedback-metrics">
      <Metric label="Resolved" value={String(metrics.total)} />
      <Metric label="Sent unchanged" value={formatRate(metrics.sent_without_edit_rate)} tone="success" />
      <Metric label="Edited among sends" value={formatRate(metrics.edit_rate_among_sends)} tone="info" />
      <Metric label="Not sent" value={formatRate(metrics.no_send_rate)} tone="warning" />
    </div>
  );
}

function Metric({ label, value, tone = "neutral" }: { label: string; value: string; tone?: Tone }) {
  return (
    <div className={`metric-card ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function Breakdown({ title, values }: { title: string; values: FeedbackCount[] }) {
  const maximum = Math.max(...values.map((item) => item.count), 1);
  return (
    <div className="queue-panel feedback-breakdown">
      <p className="eyebrow">Breakdown</p>
      <h2>{title}</h2>
      {values.length ? (
        <ul className="breakdown-list">
          {values.map((item) => (
            <li key={item.value}>
              <div>
                <span>{reasonLabel(item.value)}</span>
                <strong>{item.count}</strong>
              </div>
              <span className="breakdown-track" aria-label={`${item.count} records`}>
                <span style={{ width: `${(item.count / maximum) * 100}%` }} />
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="quiet-empty">
          <MessageSquareDiff aria-hidden="true" size={18} />
          <span>No classified feedback.</span>
        </div>
      )}
    </div>
  );
}

function FeedbackDetail({ record }: { record: FeedbackRecord | null }) {
  if (!record) {
    return <EmptyState title="Select a resolution" detail="Decision reason, operator feedback, and reply comparison will appear here." />;
  }
  return (
    <div className="detail-panel feedback-detail">
      <p className="eyebrow">Resolution detail</p>
      <div className="detail-title-row">
        <h2>{record.approval_id}</h2>
        <Badge tone={outcomeTone(record.outcome)}>{outcomeLabel(record.outcome)}</Badge>
      </div>
      <FieldList>
        <FactRow label="Task" value={record.task_id ?? "not recorded"} />
        <FactRow label="Decision reason" value={reasonLabel(record.decision_reason)} />
        <FactRow label="Feedback reason" value={reasonLabel(record.feedback_reason)} />
        <FactRow label="Actor" value={record.actor} />
        <FactRow label="Recorded" value={formatDate(record.created_at)} />
      </FieldList>
      {record.note ? <p className="detail-note">{record.note}</p> : null}
      <ReplyComparison record={record} />
    </div>
  );
}

function ReplyComparison({ record }: { record: FeedbackRecord }) {
  const comparison = record.reply_comparison;
  if (comparison.status === "expired") {
    return <div className="quiet-empty"><span>Reply content expired under the configured retention policy.</span></div>;
  }
  if (comparison.status === "not_applicable") {
    return <div className="quiet-empty"><span>No reply was sent for this resolution.</span></div>;
  }
  if (comparison.status === "unavailable") {
    return <div className="quiet-empty"><span>Reply comparison was not recorded.</span></div>;
  }
  return (
    <div className="reply-comparison">
      <div className="subsection-title">
        <h2>Suggested vs final</h2>
        <Badge tone={comparison.status === "changed" ? "info" : "success"}>{comparison.status}</Badge>
      </div>
      <div className="reply-columns">
        <ReplyText label="Suggested" value={comparison.suggested_reply ?? ""} tone="before" />
        <ReplyText label="Final" value={comparison.final_reply ?? ""} tone="after" />
      </div>
      {comparison.status === "changed" ? <InlineDiff parts={comparison.diff} /> : null}
    </div>
  );
}

function InlineDiff({ parts }: { parts: ReplyDiffPart[] }) {
  return (
    <div className="inline-diff" aria-label="Reply text changes">
      <strong>Changes</strong>
      <div>
        {parts.map((part, index) => {
          if (part.op === "equal") return <span key={index}>{part.after ?? part.before}</span>;
          if (part.op === "insert") return <ins key={index}>{part.after}</ins>;
          if (part.op === "delete") return <del key={index}>{part.before}</del>;
          return <span key={index}><del>{part.before}</del><ins>{part.after}</ins></span>;
        })}
      </div>
    </div>
  );
}

function ReplyText({ label, value, tone }: { label: string; value: string; tone: "before" | "after" }) {
  return (
    <div className={`reply-text ${tone}`}>
      <strong>{label}</strong>
      <pre>{value}</pre>
    </div>
  );
}

function FactRow({ label, value }: { label: string; value: string }) {
  return <div><dt>{label}</dt><dd>{value}</dd></div>;
}

function formatRate(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

function outcomeLabel(value: FeedbackRecord["outcome"]): string {
  return {
    suggestion_sent: "Sent unchanged",
    edited_sent: "Edited and sent",
    no_send_keep_watching: "No send · watching",
    no_send_end_task: "No send · ended"
  }[value];
}

function outcomeTone(value: FeedbackRecord["outcome"]): Tone {
  if (value === "suggestion_sent") return "success";
  if (value === "edited_sent") return "info";
  return value === "no_send_keep_watching" ? "warning" : "muted";
}

function reasonLabel(value: string | null): string {
  return value ? value.replace(/_/g, " ") : "not recorded";
}
