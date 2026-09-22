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
    return <LoadingState title="正在读取反馈" />;
  }
  if (overview.error) {
    return <ErrorState title="无法读取反馈" error={overview.error} />;
  }
  if (!overview.data) {
    return <EmptyState title="反馈暂不可用" detail="本地控制台没有返回反馈指标。" />;
  }

  return (
    <section className="work-grid feedback-layout" aria-label="反馈">
      <div className="work-main">
        <div className="queue-panel">
          <SectionHeader
            eyebrow="人工处理反馈"
            title="处理结果"
            badge={<Badge tone={executionMode === "production" ? "success" : "info"}>{executionMode === "production" ? "生产" : executionMode === "dry_run" ? "预演" : "全部"}</Badge>}
          >
            <p className="section-note">
              指标来自已记录的 Owner 处理结果。默认只看生产数据，预演不会计入采纳率。
            </p>
          </SectionHeader>
          <div className="feedback-controls">
            <SegmentedControl
              label="执行模式"
              value={executionMode}
              onChange={setExecutionMode}
              options={[
                { value: "production", label: "生产" },
                { value: "dry_run", label: "预演" },
                { value: "all", label: "全部" }
              ]}
            />
            <SegmentedControl
              label="统计时段"
              value={String(windowDays)}
              onChange={(value) => setWindowDays(value === "7" ? 7 : 30)}
              options={[
                { value: "7", label: "近 7 天" },
                { value: "30", label: "近 30 天" }
              ]}
            />
          </div>
          {metrics ? <MetricSummary metrics={metrics} /> : null}
        </div>

        <div className="split-panels feedback-breakdowns">
          <Breakdown title="决策原因" values={metrics?.by_decision_reason ?? []} />
          <Breakdown title="反馈原因" values={metrics?.by_feedback_reason ?? []} />
        </div>

        <div className="queue-panel">
          <SectionHeader
            eyebrow="近期处理"
            title="结果记录"
            badge={<Badge tone="muted">{records.length}</Badge>}
          />
          <SegmentedControl
            label="结果筛选"
            value={outcomeFilter}
            onChange={setOutcomeFilter}
            options={[
              { value: "all", label: "全部" },
              { value: "suggestion_sent", label: "直接发送" },
              { value: "edited_sent", label: "编辑后发送" },
              { value: "no_send_keep_watching", label: "继续观察" },
              { value: "no_send_end_task", label: "结束任务" }
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
                  title={record.task_id ?? "未关联任务"}
                >
                  <span className="row-preview">
                    {shortText(record.reply_comparison.final_reply ?? record.reply_comparison.suggested_reply, outcomeLabel(record.outcome))}
                  </span>
                </ListRow>
              ))}
            </div>
          ) : (
            <EmptyState title="当前筛选下没有反馈" detail="Owner 记录审批处理结果后，会显示在这里。" />
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
      <Metric label="已处理" value={String(metrics.total)} />
      <Metric label="直接发送率" value={formatRate(metrics.sent_without_edit_rate)} tone="success" />
      <Metric label="发送中编辑率" value={formatRate(metrics.edit_rate_among_sends)} tone="info" />
      <Metric label="未发送率" value={formatRate(metrics.no_send_rate)} tone="warning" />
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
      <p className="eyebrow">原因分布</p>
      <h2>{title}</h2>
      {values.length ? (
        <ul className="breakdown-list">
          {values.map((item) => (
            <li key={item.value}>
              <div>
                <span>{reasonLabel(item.value)}</span>
                <strong>{item.count}</strong>
              </div>
              <span className="breakdown-track" aria-label={`${item.count} 条记录`}>
                <span style={{ width: `${(item.count / maximum) * 100}%` }} />
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="quiet-empty">
          <MessageSquareDiff aria-hidden="true" size={18} />
          <span>暂无已分类的反馈。</span>
        </div>
      )}
    </div>
  );
}

function FeedbackDetail({ record }: { record: FeedbackRecord | null }) {
  if (!record) {
    return <EmptyState title="选择一条处理记录" detail="此处会显示原因、反馈和回复对比。" />;
  }
  return (
    <div className="detail-panel feedback-detail">
      <p className="eyebrow">处理详情</p>
      <div className="detail-title-row">
        <h2>{record.approval_id}</h2>
        <Badge tone={outcomeTone(record.outcome)}>{outcomeLabel(record.outcome)}</Badge>
      </div>
      <FieldList>
        <FactRow label="任务" value={record.task_id ?? "未记录"} />
        <FactRow label="决策原因" value={reasonLabel(record.decision_reason)} />
        <FactRow label="反馈原因" value={reasonLabel(record.feedback_reason)} />
        <FactRow label="执行者" value={record.actor} />
        <FactRow label="记录时间" value={formatDate(record.created_at)} />
      </FieldList>
      {record.note ? <p className="detail-note">{record.note}</p> : null}
      <ReplyComparison record={record} />
    </div>
  );
}

function ReplyComparison({ record }: { record: FeedbackRecord }) {
  const comparison = record.reply_comparison;
  if (comparison.status === "expired") {
    return <div className="quiet-empty"><span>回复内容已按留存策略过期。</span></div>;
  }
  if (comparison.status === "not_applicable") {
    return <div className="quiet-empty"><span>这次处理没有发送回复。</span></div>;
  }
  if (comparison.status === "unavailable") {
    return <div className="quiet-empty"><span>未记录回复对比。</span></div>;
  }
  return (
    <div className="reply-comparison">
      <div className="subsection-title">
        <h2>建议回复与最终回复</h2>
        <Badge tone={comparison.status === "changed" ? "info" : "success"}>{comparison.status}</Badge>
      </div>
      <div className="reply-columns">
        <ReplyText label="建议回复" value={comparison.suggested_reply ?? ""} tone="before" />
        <ReplyText label="最终回复" value={comparison.final_reply ?? ""} tone="after" />
      </div>
      {comparison.status === "changed" ? <InlineDiff parts={comparison.diff} /> : null}
    </div>
  );
}

function InlineDiff({ parts }: { parts: ReplyDiffPart[] }) {
  return (
    <div className="inline-diff" aria-label="回复文字变更">
      <strong>变更内容</strong>
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
    suggestion_sent: "直接发送",
    edited_sent: "编辑后发送",
    no_send_keep_watching: "未发送 · 继续观察",
    no_send_end_task: "未发送 · 结束任务"
  }[value];
}

function outcomeTone(value: FeedbackRecord["outcome"]): Tone {
  if (value === "suggestion_sent") return "success";
  if (value === "edited_sent") return "info";
  return value === "no_send_keep_watching" ? "warning" : "muted";
}

function reasonLabel(value: string | null): string {
  return value ? value.replace(/_/g, " ") : "未记录";
}
