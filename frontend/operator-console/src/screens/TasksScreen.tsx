import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, Bot, ClipboardList, MessageSquare, RotateCcw, Send, XCircle } from "lucide-react";
import { closeTask, getTask, listTasks, reopenTask } from "../api";
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
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(selectedId);
  const [messageId, setMessageId] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [commandResult, setCommandResult] = useState<CommandResult | null>(null);
  const tasks = useQuery({
    queryKey: queryKeys.tasks({ status: filter, limit: 50, offset: 0 }),
    queryFn: () => listTasks(token, { status: filter === "all" ? undefined : filter, limit: 50, offset: 0 }),
    enabled: Boolean(token)
  });
  const rows = useMemo(() => tasks.data ?? [], [tasks.data]);
  const detail = useQuery({
    queryKey: queryKeys.task(selectedTaskId),
    queryFn: () => getTask(token, selectedTaskId ?? ""),
    enabled: Boolean(token && selectedTaskId)
  });

  useEffect(() => {
    setSelectedTaskId(selectedId);
  }, [selectedId]);

  useEffect(() => {
    if (!selectedTaskId && rows[0]) {
      setSelectedTaskId(rows[0].task_id);
    }
    if (selectedTaskId && rows.length && !rows.some((task) => task.task_id === selectedTaskId)) {
      setSelectedTaskId(rows[0].task_id);
    }
  }, [rows, selectedTaskId]);

  useEffect(() => {
    setMessageId(null);
    setCommandResult(null);
  }, [selectedTaskId]);

  const close = useMutation({
    mutationFn: () => closeTask(token, selectedTaskId ?? "", { reason: clean(reason) }),
    onSuccess: async (result) => {
      setCommandResult(result);
      await invalidateAfterTaskCommand(queryClient);
    },
    onError: (error) => setCommandResult(errorResult("task.close", error))
  });
  const reopen = useMutation({
    mutationFn: () => reopenTask(token, selectedTaskId ?? "", { reason: clean(reason) }),
    onSuccess: async (result) => {
      setCommandResult(result);
      await invalidateAfterTaskCommand(queryClient);
    },
    onError: (error) => setCommandResult(errorResult("task.reopen", error))
  });
  const canClose = detail.data?.status === "watching";
  const canReopen = detail.data ? ["closed", "closed_by_owner", "human_taken_over"].includes(detail.data.status) : false;

  if (tasks.isLoading) {
    return <LoadingState title="Loading tasks" />;
  }
  if (tasks.error) {
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
          <SegmentedControl label="Task status filter" onChange={setFilter} options={taskFilters} value={filter} />
          {rows.length ? (
            <div className="list-stack">
              {rows.map((task) => (
                <ListRow
                  badge={<Badge tone={statusTone(task.status)}>{task.status}</Badge>}
                  key={task.task_id}
                  meta={`${task.chat_id ?? "no chat"} · ${task.message_count} messages · ${formatDate(task.updated_at)}`}
                  onClick={() => setSelectedTaskId(task.task_id)}
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
              <p className="eyebrow">Commands</p>
              <h2>Task lifecycle</h2>
              <TextareaField label="Reason" onChange={setReason} placeholder="Optional operator note" rows={2} value={reason} />
              <div className="command-buttons">
                <Button disabled={!canClose || close.isPending} onClick={() => close.mutate()} tone="danger">
                  <XCircle aria-hidden="true" size={15} />
                  Close
                </Button>
                <Button disabled={!canReopen || reopen.isPending} onClick={() => reopen.mutate()} tone="info">
                  <RotateCcw aria-hidden="true" size={15} />
                  Reopen
                </Button>
              </div>
              <CommandResultPanel result={commandResult} />
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
