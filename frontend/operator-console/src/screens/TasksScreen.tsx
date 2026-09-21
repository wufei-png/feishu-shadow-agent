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
  { value: "watching", label: "Watching" },
  { value: "closed", label: "Closed" },
  { value: "closed_by_owner", label: "Owner closed" },
  { value: "human_taken_over", label: "Taken over" },
  { value: "all", label: "All" }
];

export function TasksScreen({ token, selectedId }: { token: string; selectedId: string | null }) {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<TaskFilter>("watching");
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
    return <LoadingState title="Loading tasks" />;
  }
  if (tasks.error && !tasks.data) {
    return <ErrorState title="Tasks unavailable" error={tasks.error} />;
  }

  return (
    <section className="work-grid tasks-layout" aria-label="Tasks">
      <div className="work-main">
        <div className="queue-panel">
          <SectionHeader
            eyebrow="Tasks"
            title="Conversation context"
            badge={<Badge tone={rows.length ? "info" : "muted"}>{rows.length}</Badge>}
          />
          <SegmentedControl
            label="Task status filter"
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
                  meta={`${task.chat_id ?? "no chat"} · ${task.message_count} messages · ${formatDate(task.updated_at)}`}
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
            <EmptyState title="No tasks in this view" detail="Task context appears after messages are routed into a conversation task." />
          )}
        </div>
      </div>

      <aside className="work-detail">
        {selectedTaskId && detail.isLoading ? <LoadingState title="Loading task detail" /> : null}
        {detail.error ? <ErrorState title="Task detail unavailable" error={detail.error} /> : null}
        {detail.data ? (
          <>
            <div className="detail-panel">
              <p className="eyebrow">Task Detail</p>
              <div className="detail-title-row">
                <h2>{detail.data.task_label || detail.data.task_id}</h2>
                <Badge tone={statusTone(detail.data.status)}>{detail.data.status}</Badge>
              </div>
              <FieldList>
                <FactRow label="Task" value={detail.data.task_id} />
                <FactRow label="Chat" value={detail.data.chat_id ?? "not recorded"} />
                <FactRow label="Watch until" value={formatDate(detail.data.watch_until)} />
                <FactRow label="Agent cwd" value={detail.data.agent_working_dir ?? "not recorded"} />
                <FactRow label="Policy" value={detail.data.effective_policy.policy_source} />
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
              <p className="eyebrow">Commands</p>
              <h2>Task lifecycle</h2>
              <TextareaField label="Reason" onChange={setReason} placeholder="Optional operator note" rows={2} value={reason} />
              <div className="command-buttons">
                <Button disabled={!canClose || selectedTaskBusy} onClick={() => runTaskCommand("close")} tone="danger">
                  <XCircle aria-hidden="true" size={15} />
                  Close
                </Button>
                <Button disabled={!canReopen || selectedTaskBusy} onClick={() => runTaskCommand("reopen")} tone="info">
                  <RotateCcw aria-hidden="true" size={15} />
                  Reopen
                </Button>
              </div>
              <CommandResultPanel result={commandResult} />
            </div>

            <div className="detail-panel">
              <div className="subsection-title">
                <RotateCcw aria-hidden="true" size={16} />
                <h2>Processing recovery</h2>
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
                <h2>Timeline</h2>
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
                <p className="detail-note">No task messages recorded.</p>
              )}
            </div>

            <div className="detail-panel">
              <div className="subsection-title">
                <Bell aria-hidden="true" size={16} />
                <h2>Related approvals</h2>
              </div>
              {detail.data.pending_approvals.length ? (
                <ul className="timeline-list">
                  {detail.data.pending_approvals.map((approval) => (
                    <li key={approval.approval_id}>
                      <Bell aria-hidden="true" size={14} />
                      <span>{approval.approval_id}</span>
                      <small>{approval.is_overdue ? "overdue" : approval.status}</small>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="detail-note">No pending approvals for this task.</p>
              )}
            </div>

            <div className="detail-panel">
              <div className="subsection-title">
                <Send aria-hidden="true" size={16} />
                <h2>Dispatch actions</h2>
              </div>
              {detail.data.actions.length ? (
                <ul className="timeline-list">
                  {detail.data.actions.map((action) => (
                    <li key={action.action_id}>
                      <Send aria-hidden="true" size={14} />
                      <span>Action {action.action_id}</span>
                      <small>{action.status}</small>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="detail-note">No dispatch actions for this task.</p>
              )}
            </div>

            <div className="detail-panel">
              <div className="subsection-title">
                <Bot aria-hidden="true" size={16} />
                <h2>Agent audits</h2>
              </div>
              <AgentAuditList audits={detail.data.agent_audits} />
            </div>
          </>
        ) : (
          <EmptyState title="Select a task" detail="Timeline, policy, approvals, and dispatch actions will appear here." />
        )}
      </aside>

      <aside className="message-drawer" aria-label="Message Detail">
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
