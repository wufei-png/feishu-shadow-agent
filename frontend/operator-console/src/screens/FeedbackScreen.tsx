import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { getFeedbackOverview } from "../api";
import {
  Fact,
  Badge,
  Button,
  CopyValue,
  EmptyState,
  ErrorState,
  FieldList,
  formatDate,
  ListRow,
  LoadingState,
  QueueControls,
  SectionHeader,
  SegmentedControl,
  shortText,
  TechnicalDetails
} from "../components/Primitives";
import { queryKeys } from "../queryKeys";
import { enumLabel } from "../presentation";
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

export function FeedbackScreen({ token, runtimeMode = "production" }: { token: string; runtimeMode?: "production" | "dry_run" }) {
  const { t } = useTranslation();
  const [executionMode, setExecutionMode] = useState<FeedbackExecutionMode>("production");
  const effectiveMode = runtimeMode === "dry_run" ? "dry_run" : executionMode;
  const [windowDays, setWindowDays] = useState<WindowDays>(30);
  const [outcomeFilter, setOutcomeFilter] = useState<OutcomeFilter>("all");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const overview = useQuery({
    queryKey: queryKeys.feedbackOverview(effectiveMode, windowDays),
    queryFn: () => getFeedbackOverview(token, effectiveMode, windowDays),
    enabled: Boolean(token),
    refetchInterval: 30_000
  });
  const metrics = overview.data?.windows.find((item) => item.days === windowDays) ?? null;
  const records = (overview.data?.recent ?? []).filter(
    (item) => outcomeFilter === "all" || item.outcome === outcomeFilter
  );
  const selected = records.find((item) => item.id === selectedId) ?? records[0];

  return (
    <section className="work-grid feedback-layout" aria-label={t("feedback.aria")}>
      <div className="work-main">
        <div className="queue-panel">
          <SectionHeader
            title={t("feedback.title")}
            badge={runtimeMode === "dry_run" ? <Badge tone="info">{t("feedback.dryRun")}</Badge> : null}
          />
          <div className="feedback-controls">
            {runtimeMode !== "dry_run" ? <SegmentedControl
              label={t("feedback.executionMode")}
              value={executionMode}
              onChange={setExecutionMode}
              options={[
                { value: "production", label: t("feedback.production") },
                { value: "dry_run", label: t("feedback.dryRun") },
                { value: "all", label: t("feedback.all") }
              ]}
            /> : null}
            <SegmentedControl
              label={t("feedback.period")}
              value={String(windowDays)}
              onChange={(value) => setWindowDays(value === "7" ? 7 : 30)}
              options={[
                { value: "7", label: t("feedback.last7") },
                { value: "30", label: t("feedback.last30") }
              ]}
            />
          </div>
          {overview.isLoading ? <LoadingState title={t("feedback.loading")} /> : null}
          {overview.error && !overview.data ? (
            <div>
              <ErrorState title={t("feedback.error")} error={overview.error} />
              <Button disabled={overview.isFetching} onClick={() => void overview.refetch()}>{t("common.refresh")}</Button>
            </div>
          ) : null}
          {metrics ? <MetricSummary metrics={metrics} /> : null}
          {metrics && (metrics.by_decision_reason.length > 0 || metrics.by_feedback_reason.length > 0) ? (
            <TechnicalDetails summary={t("feedback.distribution")}>
              <div className="split-panels feedback-breakdowns">
                <Breakdown title={t("feedback.decisionReasons")} values={metrics.by_decision_reason} />
                <Breakdown title={t("feedback.feedbackReasons")} values={metrics.by_feedback_reason} />
              </div>
            </TechnicalDetails>
          ) : null}
        </div>

        {overview.data ? <div className="queue-panel">
          <SectionHeader as="h2" title={t("feedback.records")} badge={<Badge tone="muted">{records.length}</Badge>} />
          <SegmentedControl
            label={t("feedback.outcomeFilter")}
            value={outcomeFilter}
            onChange={setOutcomeFilter}
            options={[
              { value: "all", label: t("feedback.all") },
              { value: "suggestion_sent", label: enumLabel(t, "outcome", "suggestion_sent") },
              { value: "edited_sent", label: enumLabel(t, "outcome", "edited_sent") },
              { value: "no_send_keep_watching", label: t("feedback.continueWatching") },
              { value: "no_send_end_task", label: t("feedback.endTask") }
            ]}
          />
          <QueueControls
            error={overview.error}
            isFetching={overview.isFetching}
            onRefresh={() => void overview.refetch()}
            updatedAt={overview.dataUpdatedAt}
          />
          {records.length ? (
            <div className="list-stack">
              {records.map((record) => (
                <ListRow
                  badge={<Badge tone={outcomeTone(record.outcome)}>{outcomeLabel(t, record.outcome)}</Badge>}
                  key={record.id}
                  meta={`${record.task_id ?? t("common.notLinked")} · ${formatDate(record.created_at)}`}
                  onClick={() => setSelectedId(record.id)}
                  selected={record.id === selected?.id}
                  title={shortText(record.reply_comparison.final_reply ?? record.reply_comparison.suggested_reply, outcomeLabel(t, record.outcome))}
                />
              ))}
            </div>
          ) : (
            <EmptyState title={t("feedback.empty")} detail={t("feedback.emptyDetail")} />
          )}
        </div> : null}
      </div>

      {selected ? <aside className="work-detail"><FeedbackDetail record={selected} /></aside> : null}
    </section>
  );
}

function MetricSummary({ metrics }: { metrics: FeedbackMetrics }) {
  const { t } = useTranslation();
  return (
    <div className="metric-row feedback-metrics">
      <Metric label={t("feedback.processed")} value={String(metrics.total)} />
      <Metric label={t("feedback.directSendRate")} value={formatRate(metrics.sent_without_edit_rate)} tone="success" />
      <Metric label={t("feedback.editRate")} value={formatRate(metrics.edit_rate_among_sends)} tone="info" />
      <Metric label={t("feedback.noSendRate")} value={formatRate(metrics.no_send_rate)} tone="warning" />
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
  const { t } = useTranslation();
  const maximum = Math.max(...values.map((item) => item.count), 1);
  return (
    <div className="feedback-breakdown">
      <h3>{title}</h3>
      {values.length ? (
        <ul className="breakdown-list">
          {values.map((item) => (
            <li key={item.value}>
              <div>
                <span>{reasonLabel(t, item.value)}</span>
                <strong>{item.count}</strong>
              </div>
              <span className="breakdown-track" aria-label={t("feedback.recordCount", { count: item.count })}>
                <span style={{ width: `${(item.count / maximum) * 100}%` }} />
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="quiet-empty">
          <span>{t("feedback.noClassified")}</span>
        </div>
      )}
    </div>
  );
}

function FeedbackDetail({ record }: { record: FeedbackRecord }) {
  const { t } = useTranslation();
  return (
    <div className="detail-panel feedback-detail">
      <h2>{outcomeLabel(t, record.outcome)}</h2>
      <ReplyComparison record={record} />
      {record.note ? <p className="detail-note">{record.note}</p> : null}
      <TechnicalDetails summary={t("feedback.detail")}>
        <FieldList>
          <Fact label={t("approvals.approvalId")} value={<CopyValue label={t("approvals.approvalId")} value={record.approval_id} />} />
          <Fact label={t("feedback.task")} value={record.task_id ?? t("common.notRecorded")} />
          <Fact label={t("feedback.decisionReasons")} value={reasonLabel(t, record.decision_reason)} />
          <Fact label={t("feedback.feedbackReasons")} value={reasonLabel(t, record.feedback_reason)} />
          <Fact label={t("common.actor")} value={record.actor} />
          <Fact label={t("feedback.recordedAt")} value={formatDate(record.created_at)} />
        </FieldList>
      </TechnicalDetails>
    </div>
  );
}

function ReplyComparison({ record }: { record: FeedbackRecord }) {
  const { t } = useTranslation();
  const comparison = record.reply_comparison;
  if (comparison.status === "expired") {
    return <div className="quiet-empty"><span>{t("feedback.contentExpired")}</span></div>;
  }
  if (comparison.status === "not_applicable") {
    return <div className="quiet-empty"><span>{t("feedback.noReply")}</span></div>;
  }
  if (comparison.status === "unavailable") {
    return <div className="quiet-empty"><span>{t("feedback.comparisonUnavailable")}</span></div>;
  }
  return (
    <div className="reply-comparison">
      <div className="subsection-title">
        <h3>{t("feedback.comparison")}</h3>
        <Badge tone={comparison.status === "changed" ? "info" : "success"}>{enumLabel(t, "status", comparison.status)}</Badge>
      </div>
      {comparison.status === "changed" ? <InlineDiff parts={comparison.diff} /> : null}
      <div className="reply-columns">
        <ReplyText label={t("feedback.suggested")} value={comparison.suggested_reply ?? ""} tone="before" />
        <ReplyText label={t("feedback.final")} value={comparison.final_reply ?? ""} tone="after" />
      </div>
    </div>
  );
}

function InlineDiff({ parts }: { parts: ReplyDiffPart[] }) {
  const { t } = useTranslation();
  return (
    <div className="inline-diff" aria-label={t("feedback.diffAria")}>
      <strong>{t("feedback.changedText")}</strong>
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


function formatRate(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

function outcomeLabel(t: TFunction, value: FeedbackRecord["outcome"]): string {
  return enumLabel(t, "outcome", value);
}

function outcomeTone(value: FeedbackRecord["outcome"]): Tone {
  if (value === "suggestion_sent") return "success";
  if (value === "edited_sent") return "info";
  return value === "no_send_keep_watching" ? "warning" : "muted";
}

function reasonLabel(t: TFunction, value: string | null): string {
  return enumLabel(t, "reason", value);
}
