import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, Bot, MessageSquare, RotateCcw, Send, XCircle } from "lucide-react";
import { closeTask, getTask, listTasks, reopenTask, retryMessageProcessing, updateTaskBackground } from "../api";
import {
  Badge,
  Button,
  CommandResultPanel,
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
  statusTone,
  TextareaField
} from "../components/Primitives";
import { invalidateAfterTaskCommand, queryKeys } from "../queryKeys";
import type { CommandResult, TaskStatus } from "../types";
import { AgentAuditList } from "./AgentAuditList";
import { MessageDetailPanel } from "./MessageDetailPanel";

type TaskFilter = TaskStatus | "all";

type TaskCommandInput = {
  kind: "close" | "reopen" | "retry-processing" | "update-background";
  taskId: string;
  reason?: string;
  messageId?: string;
  stage?: string;
  content?: string | null;
};

const pageSize = 50;

const taskFilters: Array<{ value: TaskFilter; label: string }> = [
  { value: "watching", label: "观察中" },
  { value: "closed", label: "已关闭" },
  { value: "closed_by_owner", label: "Owner 已关闭" },
  { value: "human_taken_over", label: "人工接管" },
  { value: "all", label: "全部" }
];

export function TasksScreen({ token, selectedId, initialFilter }: { token: string; selectedId: string | null; initialFilter?: TaskFilter }) {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<TaskFilter>(initialFilter ?? "watching");
  const [page, setPage] = useState(0);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(selectedId);
  const [messageId, setMessageId] = useState<string | null>(null);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [backgroundDrafts, setBackgroundDrafts] = useState<Record<string, string>>({});
  const [commandResults, setCommandResults] = useState<Record<string, CommandResult>>({});
  const busyTaskIdsRef = useRef(new Set<string>());
  const [busyTaskIds, setBusyTaskIds] = useState<Set<string>>(new Set());
  const tasks = useQuery({
    queryKey: queryKeys.tasks({ status: filter, limit: pageSize + 1, offset: page * pageSize }),
    queryFn: () => listTasks(token, { status: filter === "all" ? undefined : filter, limit: pageSize + 1, offset: page * pageSize }),
    enabled: Boolean(token),
    refetchInterval: 15_000
  });
  const rows = useMemo(() => (tasks.data ?? []).slice(0, pageSize), [tasks.data]);
  const hasNextPage = (tasks.data?.length ?? 0) > pageSize;
  const detail = useQuery({
    queryKey: queryKeys.task(selectedTaskId),
    queryFn: () => getTask(token, selectedTaskId ?? ""),
    enabled: Boolean(token && selectedTaskId),
    refetchInterval: 15_000
  });

  useEffect(() => {
    setFilter(initialFilter ?? "watching");
    setPage(0);
  }, [initialFilter]);

  useEffect(() => {
    setSelectedTaskId(selectedId);
  }, [selectedId]);

  useEffect(() => {
    if (selectedId) {
      return;
    }
    if (!selectedTaskId && rows[0]) {
      setSelectedTaskId(rows[0].task_id);
    }
    if (selectedTaskId && rows.length && !rows.some((task) => task.task_id === selectedTaskId)) {
      setSelectedTaskId(rows[0].task_id);
    }
  }, [rows, selectedId, selectedTaskId]);

  useEffect(() => {
    setMessageId(null);
  }, [selectedTaskId]);

  const taskCommand = useMutation({
    mutationFn: (input: TaskCommandInput) =>
      input.kind === "close"
        ? closeTask(token, input.taskId, { reason: input.reason })
        : input.kind === "reopen"
          ? reopenTask(token, input.taskId, { reason: input.reason })
          : input.kind === "retry-processing"
            ? retryMessageProcessing(token, input.messageId ?? "", input.stage ?? "", { reason: input.reason })
            : updateTaskBackground(token, input.taskId, { content: input.content ?? null, reason: input.reason }),
    onSuccess: async (result, input) => {
      setCommandResults((current) => ({ ...current, [input.taskId]: result }));
      await invalidateAfterTaskCommand(queryClient);
    },
    onError: (error, input) => {
      setCommandResults((current) => ({
        ...current,
        [input.taskId]: errorResult(`task.${input.kind}`, error)
      }));
    },
    onSettled: (_result, _error, input) => {
      busyTaskIdsRef.current.delete(input.taskId);
      setBusyTaskIds(new Set(busyTaskIdsRef.current));
    }
  });
  const reason = selectedTaskId ? (reasons[selectedTaskId] ?? "") : "";
  const commandResult = selectedTaskId ? (commandResults[selectedTaskId] ?? null) : null;
  const selectedTaskBusy = selectedTaskId ? busyTaskIds.has(selectedTaskId) : false;
  const canClose = detail.data?.status === "watching";
  const canReopen = detail.data ? ["closed", "closed_by_owner", "human_taken_over"].includes(detail.data.status) : false;
  const backgroundDraft = selectedTaskId ? (backgroundDrafts[selectedTaskId] ?? detail.data?.task_background?.content ?? "") : "";

  function setReason(reason: string): void {
    if (selectedTaskId) {
      setReasons((current) => ({ ...current, [selectedTaskId]: reason }));
    }
  }

  function runTaskCommand(kind: TaskCommandInput["kind"]): void {
    const taskId = detail.data?.task_id;
    if (!taskId || busyTaskIdsRef.current.has(taskId)) {
      return;
    }
    busyTaskIdsRef.current.add(taskId);
    setBusyTaskIds(new Set(busyTaskIdsRef.current));
    taskCommand.mutate({ kind, taskId, reason: clean(reason) });
  }

  function retryProcessing(messageId: string, stage: string): void {
    const taskId = detail.data?.task_id;
    if (!taskId || busyTaskIdsRef.current.has(taskId)) {
      return;
    }
    busyTaskIdsRef.current.add(taskId);
    setBusyTaskIds(new Set(busyTaskIdsRef.current));
    taskCommand.mutate({
      kind: "retry-processing",
      taskId,
      messageId,
      stage,
      reason: clean(reason)
    });
  }

  function updateBackground(content: string | null): void {
    const taskId = detail.data?.task_id;
    if (!taskId || busyTaskIdsRef.current.has(taskId)) {
      return;
    }
    busyTaskIdsRef.current.add(taskId);
    setBusyTaskIds(new Set(busyTaskIdsRef.current));
    taskCommand.mutate({
      kind: "update-background",
      taskId,
      content,
      reason: clean(reason)
    });
  }

  if (tasks.isLoading) {
    return <LoadingState title="正在读取任务" />;
  }
  if (tasks.error && !tasks.data) {
    return <ErrorState title="无法读取任务列表" error={tasks.error} />;
  }

  return (
    <section className="work-grid tasks-layout" aria-label="任务">
      <div className="work-main">
        <div className="queue-panel">
          <SectionHeader
            eyebrow="会话任务"
            title="任务列表"
            badge={<Badge tone={rows.length ? "info" : "muted"}>{rows.length}</Badge>}
          />
          <SegmentedControl
            label="任务状态筛选"
            onChange={(nextFilter) => {
              setFilter(nextFilter);
              setPage(0);
            }}
            options={taskFilters}
            value={filter}
          />
          <QueueControls
            error={tasks.error}
            hasNext={hasNextPage}
            isFetching={tasks.isFetching}
            onNext={() => setPage((current) => current + 1)}
            onPrevious={() => setPage((current) => Math.max(0, current - 1))}
            onRefresh={() => void tasks.refetch()}
            page={page}
            updatedAt={tasks.dataUpdatedAt}
          />
          {rows.length ? (
            <div className="list-stack">
              {rows.map((task) => (
                <ListRow
                  badge={<Badge tone={statusTone(task.status)}>{task.status}</Badge>}
                  key={task.task_id}
                  meta={`${task.chat_id ?? "未记录会话"} · ${task.message_count} 条消息 · ${formatDate(task.updated_at)}`}
                  onClick={() => selectTask(task.task_id, setSelectedTaskId)}
                  selected={task.task_id === selectedTaskId}
                  title={task.task_label || task.task_id}
                >
                  <span className="row-preview">{task.root_message_id ?? task.task_id}</span>
                  {(task.recommended_actions ?? []).length ? (
                    <span className="inline-badges row-actions">
                      {(task.recommended_actions ?? []).slice(0, 2).map((action) => (
                        <Badge key={action} tone="warning">
                          {action}
                        </Badge>
                      ))}
                    </span>
                  ) : null}
                </ListRow>
              ))}
            </div>
          ) : (
            <EmptyState title="当前筛选下没有任务" detail="消息归入会话任务后会显示在这里。" />
          )}
        </div>
      </div>

      <aside className="work-detail">
        {selectedTaskId && detail.isLoading ? <LoadingState title="正在读取任务详情" /> : null}
        {detail.error ? <ErrorState title="无法读取任务详情" error={detail.error} /> : null}
        {detail.data ? (
          <>
            <div className="detail-panel">
              <p className="eyebrow">任务详情</p>
              <div className="detail-title-row">
                <h2>{detail.data.task_label || detail.data.task_id}</h2>
                <Badge tone={statusTone(detail.data.status)}>{detail.data.status}</Badge>
              </div>
              <FieldList>
                <FactRow label="任务 ID" value={detail.data.task_id} />
                <FactRow label="会话" value={detail.data.chat_id ?? "未记录"} />
                <FactRow label="观察截止" value={formatDate(detail.data.watch_until)} />
                <FactRow label="Agent 工作目录" value={detail.data.agent_working_dir ?? "未记录"} />
                <FactRow label="策略来源" value={detail.data.effective_policy.policy_source} />
              </FieldList>
              {detail.data.recommended_actions.length ? (
                <div className="inline-badges">
                  {detail.data.recommended_actions.map((action) => (
                    <Badge key={action} tone="warning">
                      {action}
                    </Badge>
                  ))}
                </div>
              ) : null}
            </div>

            <div className="detail-panel">
              <div className="subsection-title">
                <Bot aria-hidden="true" size={16} />
                <h2>任务背景</h2>
              </div>
              <TextareaField
                label="Owner 补充背景"
                onChange={(value) => {
                  if (selectedTaskId) {
                    setBackgroundDrafts((current) => ({ ...current, [selectedTaskId]: value }));
                  }
                }}
                placeholder="仅写入这个任务需要长期参考的事实或约束"
                rows={5}
                value={backgroundDraft}
              />
              <p className="detail-note">
                下次 fresh 重建生效；不会注入或重置当前 live provider session。
                {detail.data.task_background ? ` 当前版本 v${detail.data.task_background.version}。` : " 尚无背景。"}
              </p>
              <div className="command-buttons">
                <Button disabled={selectedTaskBusy || !backgroundDraft.trim()} onClick={() => updateBackground(backgroundDraft)} tone="info">
                  保存背景
                </Button>
                <Button
                  disabled={selectedTaskBusy || (!detail.data.task_background?.content && !backgroundDraft)}
                  onClick={() => {
                    if (selectedTaskId) {
                      setBackgroundDrafts((current) => ({ ...current, [selectedTaskId]: "" }));
                    }
                    updateBackground(null);
                  }}
                  tone="danger"
                >
                  清空背景
                </Button>
              </div>
            </div>

            <div className="detail-panel">
              <p className="eyebrow">任务操作</p>
              <h2>任务状态</h2>
              <TextareaField label="原因" onChange={setReason} placeholder="可选；记录本次操作的原因" rows={2} value={reason} />
              <div className="command-buttons">
                <Button disabled={!canClose || selectedTaskBusy} onClick={() => runTaskCommand("close")} tone="danger">
                  <XCircle aria-hidden="true" size={15} />
                  关闭任务
                </Button>
                <Button disabled={!canReopen || selectedTaskBusy} onClick={() => runTaskCommand("reopen")} tone="info">
                  <RotateCcw aria-hidden="true" size={15} />
                  重新打开
                </Button>
              </div>
              <CommandResultPanel result={commandResult} />
            </div>

            <div className="detail-panel">
              <div className="subsection-title">
                <RotateCcw aria-hidden="true" size={16} />
                <h2>处理恢复</h2>
              </div>
              {(detail.data.processing ?? []).filter((item) => ["processing_failed_terminal", "blocked_waiting_external"].includes(item.status)).length ? (
                <ul className="timeline-list">
                  {(detail.data.processing ?? []).filter((item) => ["processing_failed_terminal", "blocked_waiting_external"].includes(item.status)).map((item) => {
                    const activeRetry = item.latest_retry?.status === "queued" || item.latest_retry?.status === "claimed";
                    return (
                      <li key={`${item.message_id}-${item.revision}-${item.stage}`}>
                        <RotateCcw aria-hidden="true" size={14} />
                        <span>{item.stage} · {item.message_id} r{item.revision}</span>
                        <small>{item.status}{item.terminal_reason ? ` · ${item.terminal_reason}` : ""}</small>
                        <Button disabled={selectedTaskBusy || activeRetry} onClick={() => retryProcessing(item.message_id, item.stage)} tone="warning">
                          {activeRetry ? "已排队" : "重试"}
                        </Button>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <p className="detail-note">没有可人工重试的处理阶段。</p>
              )}
              <p className="detail-note">仅终态或外部阻塞可重试；发送结果不确定仍需在发送页人工核实。</p>
            </div>

            <div className="detail-panel">
              <div className="subsection-title">
                <MessageSquare aria-hidden="true" size={16} />
                <h2>消息时间线</h2>
              </div>
              {detail.data.recent_messages.length ? (
                <ul className="timeline-list">
                  {detail.data.recent_messages.map((message) => (
                    <li key={message.message_id}>
                      <button className="timeline-button" onClick={() => setMessageId(message.message_id)} type="button">
                        <MessageSquare aria-hidden="true" size={14} />
                        <span>{shortText(message.text, message.message_id)}</span>
                        <small>{message.sender_role ?? message.role}</small>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="detail-note">此任务尚无已记录消息。</p>
              )}
            </div>

            <div className="detail-panel">
              <div className="subsection-title">
                <Bell aria-hidden="true" size={16} />
                <h2>关联审批</h2>
              </div>
              {detail.data.pending_approvals.length ? (
                <ul className="timeline-list">
                  {detail.data.pending_approvals.map((approval) => (
                    <li key={approval.approval_id}>
                      <Bell aria-hidden="true" size={14} />
                      <span>{approval.approval_id}</span>
                      <small>{approval.is_overdue ? "已逾期" : approval.status}</small>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="detail-note">此任务没有待处理审批。</p>
              )}
            </div>

            <div className="detail-panel">
              <div className="subsection-title">
                <Send aria-hidden="true" size={16} />
                <h2>发送记录</h2>
              </div>
              {detail.data.actions.length ? (
                <ul className="timeline-list">
                  {detail.data.actions.map((action) => (
                    <li key={action.action_id}>
                      <Send aria-hidden="true" size={14} />
                      <span>发送记录 {action.action_id}</span>
                      <small>{action.status}</small>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="detail-note">此任务没有发送记录。</p>
              )}
            </div>

            <div className="detail-panel">
              <div className="subsection-title">
                <Bot aria-hidden="true" size={16} />
                <h2>Agent 审计</h2>
              </div>
              <AgentAuditList audits={detail.data.agent_audits} />
            </div>
          </>
        ) : (
          <EmptyState title="选择一个任务" detail="此处会显示时间线、策略、审批和发送记录。" />
        )}
      </aside>

      <aside className="message-drawer" aria-label="消息详情">
        <MessageDetailPanel messageId={messageId} token={token} />
      </aside>
    </section>
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

function clean(value: string): string | undefined {
  const trimmed = value.trim();
  return trimmed || undefined;
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

function selectTask(taskId: string, setSelectedTaskId: (taskId: string) => void): void {
  setSelectedTaskId(taskId);
  window.location.hash = `tasks/${encodeURIComponent(taskId)}`;
}
