import { useMutation } from "@tanstack/react-query";
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Bot, FileText, GitBranch, PackageSearch, RotateCcw, Send } from "lucide-react";
import { getMessageDetail, replayMessage, retryMessageProcessing } from "../api";
import {
  Badge,
  Button,
  CommandResultPanel,
  EmptyState,
  ErrorState,
  FieldList,
  formatDate,
  JsonBlock,
  LoadingState,
  shortText,
  statusTone
} from "../components/Primitives";
import { queryKeys } from "../queryKeys";
import type { CommandResult } from "../types";
import { AgentAuditList } from "./AgentAuditList";

export function MessageDetailPanel({ token, messageId }: { token: string; messageId: string | null }) {
  const detail = useQuery({
    queryKey: queryKeys.messageDetail(messageId),
    queryFn: () => getMessageDetail(token, messageId ?? ""),
    enabled: Boolean(token && messageId)
  });
  const replay = useMutation({
    mutationFn: (targetMessageId: string) => replayMessage(token, targetMessageId),
    onError: (error) => errorResult("message.replay_dry_run", error)
  });
  const retryProcessing = useMutation({
    mutationFn: ({ targetMessageId, stage }: { targetMessageId: string; stage: string }) =>
      retryMessageProcessing(token, targetMessageId, stage, {}),
    onSuccess: async () => {
      await detail.refetch();
    }
  });
  const replayResult =
    replay.variables === messageId
      ? (replay.data as CommandResult | undefined) ?? (replay.error ? errorResult("message.replay_dry_run", replay.error) : null)
      : null;
  const retryResult =
    retryProcessing.variables?.targetMessageId === messageId
      ? (retryProcessing.data as CommandResult | undefined) ??
        (retryProcessing.error ? errorResult("processing.retry", retryProcessing.error) : null)
      : null;

  if (!messageId) {
    return <EmptyState title="选择一条消息" detail="此处会显示消息的处理上下文。" />;
  }
  if (detail.isLoading) {
    return <LoadingState title="正在读取消息详情" />;
  }
  if (detail.error) {
    return <ErrorState title="无法读取消息详情" error={detail.error} />;
  }
  if (!detail.data) {
    return <EmptyState title="找不到消息" detail="本地存储中没有这条消息的详情。" />;
  }

  return (
    <div className="message-detail-stack">
      <div className="detail-panel">
        <p className="eyebrow">消息详情</p>
        <div className="detail-title-row">
          <h2>{detail.data.message.message_id}</h2>
          <Badge tone={statusTone(detail.data.message.sender_role)}>{detail.data.message.sender_role ?? "unknown"}</Badge>
        </div>
        <p className="preview-copy">{shortText(detail.data.message.text, "无消息正文")}</p>
        <FieldList>
          <FactRow label="会话" value={detail.data.message.chat_id ?? "未记录"} />
          <FactRow label="发送时间" value={formatDate(detail.data.message.sent_at)} />
          <FactRow label="话题" value={detail.data.message.thread_id ?? "无"} />
          <FactRow label="回复消息" value={detail.data.message.reply_to_message_id ?? "无"} />
        </FieldList>
      </div>

      <div className="detail-panel">
        <div className="subsection-title">
          <GitBranch aria-hidden="true" size={16} />
          <h2>路由与任务</h2>
        </div>
        <ul className="timeline-list">
          {detail.data.routing_audits.map((audit, index) => (
            <li key={`${String(audit.id ?? index)}`}>
              <GitBranch aria-hidden="true" size={14} />
              <span>{String(audit.route ?? "route")}</span>
              <small>{String(audit.route_reason ?? "")}</small>
            </li>
          ))}
        </ul>
        {detail.data.task_summaries.length ? (
          <div className="inline-badges">
            {detail.data.task_summaries.map((task) => (
              <Badge key={task.task_id} tone={statusTone(task.status)}>
                {task.task_id}
              </Badge>
            ))}
          </div>
        ) : null}
      </div>

      <div className="detail-panel">
        <div className="subsection-title">
          <RotateCcw aria-hidden="true" size={16} />
          <h2>消息重放预演</h2>
        </div>
        <Button disabled={replay.isPending} onClick={() => replay.mutate(messageId)} tone="info">
          <RotateCcw aria-hidden="true" size={15} />
          预演重放
        </Button>
        <CommandResultPanel result={replayResult} />
      </div>

      <div className="detail-panel">
        <div className="subsection-title">
          <PackageSearch aria-hidden="true" size={16} />
          <h2>处理阶段</h2>
        </div>
        {detail.data.processing.length ? (
          <ul className="timeline-list">
            {detail.data.processing.map((item) => {
              const activeRetry = item.latest_retry?.status === "queued" || item.latest_retry?.status === "claimed";
              return (
                <li key={item.id}>
                  <PackageSearch aria-hidden="true" size={14} />
                  <span>{item.stage}</span>
                  <small>{`${item.status}${item.terminal_reason ? ` · ${item.terminal_reason}` : ""}`}</small>
                  {["processing_failed_terminal", "blocked_waiting_external"].includes(item.status) ? (
                    <Button
                      disabled={retryProcessing.isPending || activeRetry}
                      onClick={() => retryProcessing.mutate({ targetMessageId: messageId, stage: item.stage })}
                      tone="warning"
                    >
                      {activeRetry ? "已排队" : "重试"}
                    </Button>
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="detail-note">这条消息没有处理阶段记录。</p>
        )}
        <CommandResultPanel result={retryResult} />
      </div>

      <div className="detail-panel">
        <div className="subsection-title">
          <PackageSearch aria-hidden="true" size={16} />
          <h2>资源</h2>
        </div>
        {detail.data.resources.length ? (
          <ul className="audit-list">
            {detail.data.resources.map((resource) => (
              <li key={resource.id}>
                <div className="audit-row-head">
                  <Badge tone={statusTone(resource.download_status)}>{resource.download_status}</Badge>
                  <strong>{resource.resource_type}</strong>
                  <span>{resource.file_key}</span>
                </div>
                <FieldList>
                  <FactRow label="路径" value={resource.path ?? "未记录"} />
                  <FactRow label="路径存在" value={resource.path_exists === null ? "未检查" : resource.path_exists ? "是" : "否"} />
                  <FactRow label="SHA-256" value={resource.sha256_short ?? "未记录"} />
                </FieldList>
                {Object.keys(resource.raw_summary).length ? (
                  <details>
                    <summary>原始摘要</summary>
                    <JsonBlock value={resource.raw_summary} />
                  </details>
                ) : null}
                <details>
                  <summary>原始 JSON</summary>
                  <JsonBlock value={resource.raw} />
                </details>
              </li>
            ))}
          </ul>
        ) : (
          <p className="detail-note">这条消息没有可下载资源记录。</p>
        )}
      </div>

      <div className="detail-panel">
        <div className="subsection-title">
          <Bot aria-hidden="true" size={16} />
          <h2>Agent 审计</h2>
        </div>
        <AgentAuditList audits={detail.data.agent_audits} compact />
      </div>

      <div className="detail-panel">
        <div className="subsection-title">
          <FileText aria-hidden="true" size={16} />
          <h2>审批</h2>
        </div>
        {detail.data.approvals.length ? (
          <ul className="timeline-list">
            {detail.data.approvals.map((approval) => (
              <li key={approval.approval_id}>
                <FileText aria-hidden="true" size={14} />
                <span>{approval.approval_id}</span>
                <small>{approval.status}</small>
              </li>
            ))}
          </ul>
        ) : (
          <p className="detail-note">这条消息没有关联审批记录。</p>
        )}
      </div>

      <div className="detail-panel">
        <div className="subsection-title">
          <Send aria-hidden="true" size={16} />
          <h2>发送结果</h2>
        </div>
        {detail.data.recorded_dispatch_outcomes.length ? (
          <JsonBlock value={detail.data.recorded_dispatch_outcomes} />
        ) : (
          <p className="detail-note">没有已记录的发送结果。</p>
        )}
      </div>
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

function errorResult(command: string, error: unknown): CommandResult {
  return {
    status: "failed",
    command,
    actor: "local_console",
    reason: null,
    target: {},
    changed: false,
    result: { error: error instanceof Error ? error.message : "Request failed." },
    warnings: [],
    next_actions: []
  };
}
