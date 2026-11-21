import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Bell, MessageSquare, RotateCcw, Send, XCircle } from "lucide-react";
import { m } from "motion/react";
import { useTranslation } from "react-i18next";
import { closeTask, getTask, listTasks, reopenTask, retryMessageProcessing, updateTaskBackground } from "../api";
import {
  Fact,
  Badge,
  Button,
  CommandResultPanel,
  ConfirmButton,
  CopyValue,
  EmptyState,
  ErrorState,
  FieldList,
  formatDate,
  ListRow,
  LoadingState,
  QueueControls,
  SegmentedControl,
  shortText,
  statusTone,
  TechnicalDetails,
  TextareaField
} from "../components/Primitives";
import { useWorkbenchNavigation } from "../components/useWorkbenchNavigation";
import { invalidateAfterTaskCommand, queryKeys } from "../queryKeys";
import { panelTransition, panelVariants } from "../motionTokens";
import { enumLabel } from "../presentation";
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

export function TasksScreen({ token, selectedId, selectedMessageId, initialFilter }: {
  token: string;
  selectedId: string | null;
  selectedMessageId?: string | null;
  initialFilter?: TaskFilter;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<TaskFilter>(initialFilter ?? "watching");
  const [page, setPage] = useState(0);
  const [openedTaskId, setOpenedTaskId] = useState(selectedId);
  const [messageId, setMessageId] = useState(selectedId ? selectedMessageId ?? null : null);
  const pane = messageId ? "evidence" : openedTaskId ? "detail" : "list";
  const { heading, rememberPosition } = useWorkbenchNavigation(pane);
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
  const selectedTaskId = openedTaskId ?? rows[0]?.task_id ?? null;
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
    setOpenedTaskId(selectedId);
    setMessageId(selectedId ? selectedMessageId ?? null : null);
  }, [selectedId, selectedMessageId]);

  function openTask(taskId: string | null, message: string | null = null): void {
    setOpenedTaskId(taskId);
    setMessageId(message);
    const params = new URLSearchParams(window.location.hash.split("?")[1]);
    if (message) params.set("message", message);
    else params.delete("message");
    const path = taskId ? `tasks/${encodeURIComponent(taskId)}` : "tasks";
    window.location.hash = params.size ? `${path}?${params}` : path;
  }

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
        [input.taskId]: errorResult(input.kind === "retry-processing" ? "processing.retry" : `task.${input.kind}`, error)
      }));
    },
    onSettled: (_result, _error, input) => {
      busyTaskIdsRef.current.delete(input.taskId);
      setBusyTaskIds(new Set(busyTaskIdsRef.current));
    }
  });
  const reason = selectedTaskId ? (reasons[selectedTaskId] ?? "") : "";
  const commandResult = selectedTaskId ? (commandResults[selectedTaskId] ?? null) : null;
  const recoveryResult = commandResult?.command === "processing.retry" ? commandResult : null;
  const recoverableProcessing = (detail.data?.processing ?? []).filter(
    (item) => ["processing_failed_terminal", "blocked_waiting_external"].includes(item.status)
  );
  const selectedTaskBusy = selectedTaskId ? busyTaskIds.has(selectedTaskId) : false;
  const canClose = detail.data?.status === "watching";
  const canReopen = detail.data ? Boolean(detail.data.policy_held) || ["closed", "closed_by_owner", "human_taken_over"].includes(detail.data.status) : false;
  const backgroundDraft = selectedTaskId ? (backgroundDrafts[selectedTaskId] ?? detail.data?.task_background?.content ?? "") : "";
  const taskFilters: Array<{ value: TaskFilter; label: string }> = [
    { value: "watching", label: t("tasks.filters.watching") },
    { value: "closed", label: t("tasks.filters.closed") },
    { value: "closed_by_owner", label: t("tasks.filters.closed_by_owner") },
    { value: "human_taken_over", label: t("tasks.filters.human_taken_over") },
    { value: "all", label: t("tasks.filters.all") }
  ];

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

  return (
    <section className="workbench" data-pane={pane} aria-label={t("tasks.aria")}>
      <header className="workbench-header">
        <h1 ref={heading} tabIndex={-1}>{pane === "evidence" ? t("tasks.messageDetail") : t("tasks.title")}</h1>
        {pane !== "list" ? (
          <button
            className={`button workbench-back${messageId ? " workbench-back-always" : ""}`}
            onClick={() => openTask(messageId ? selectedTaskId : null)}
            type="button"
          >
            <ArrowLeft aria-hidden="true" size={16} />
            {messageId ? t("tasks.backToTask") : t("tasks.backToList")}
          </button>
        ) : null}
      </header>

      <div className="work-grid tasks-layout">
        <div className="work-main workbench-pane" data-pane="list">
          <div className="queue-panel">
            <SegmentedControl
              label={t("tasks.filterLabel")}
              onChange={(nextFilter) => {
                setFilter(nextFilter);
                setPage(0);
              }}
              options={taskFilters}
              value={filter}
            />
            <QueueControls
              error={tasks.error}
              isFetching={tasks.isFetching}
              onRefresh={() => void tasks.refetch()}
              pagination={{
                page,
                hasNext: hasNextPage,
                onNext: () => setPage((current) => current + 1),
                onPrevious: () => setPage((current) => Math.max(0, current - 1))
              }}
              updatedAt={tasks.dataUpdatedAt}
            />
            {tasks.isLoading ? <LoadingState title={t("tasks.loading")} /> : null}
            {tasks.error && !tasks.data ? <ErrorState title={t("tasks.unavailable")} error={tasks.error} /> : null}
            {rows.length ? (
              <div className="list-stack">
                {rows.map((task) => (
                  <ListRow
                    badge={<Badge tone={statusTone(task.status)}>{enumLabel(t, "status", task.status)}</Badge>}
                    key={task.task_id}
                    meta={`${t("common.messages", { count: task.message_count })} · ${formatDate(task.updated_at)}`}
                    onClick={(event) => {
                      rememberPosition(event.currentTarget, "list");
                      openTask(task.task_id);
                    }}
                    selected={task.task_id === selectedTaskId}
                    title={task.task_label || task.task_id}
                  >
                    {(task.recommended_actions ?? []).length ? (
                      <span className="inline-badges row-actions">
                        {(task.recommended_actions ?? []).slice(0, 2).map((action) => (
                          <Badge key={action} tone="warning">
                            {enumLabel(t, "action", action)}
                          </Badge>
                        ))}
                      </span>
                    ) : null}
                  </ListRow>
                ))}
              </div>
            ) : tasks.data ? (
              <EmptyState title={t("tasks.noItems")} detail={t("tasks.noItemsDetail")} />
            ) : null}
          </div>
        </div>

        {/*
          Keyed on the selection so switching rows reads as the detail being
          replaced rather than as every field changing at once.
        */}
        <m.aside
          animate="visible"
          className="work-detail workbench-pane"
          data-pane="detail"
          hidden={pane === "evidence"}
          key={selectedTaskId ?? "none"}
          initial="hidden"
          transition={panelTransition}
          variants={panelVariants}
        >
          {selectedTaskId && detail.isLoading ? <LoadingState title={t("tasks.detailLoading")} /> : null}
          {detail.error ? (
            <div>
              <ErrorState title={t("tasks.detailUnavailable")} error={detail.error} />
              <Button onClick={() => void detail.refetch()}>{t("common.refresh")}</Button>
            </div>
          ) : null}
          {detail.data ? (
            <>
              <div className="detail-panel">
                <div className="detail-title-row">
                  <h2>{detail.data.task_label || detail.data.task_id}</h2>
                  <Badge tone={statusTone(detail.data.status)}>{enumLabel(t, "status", detail.data.status)}</Badge>
                </div>
                {detail.data.policy_held ? <p className="detail-note">{t("tasks.policyHeld")}</p> : null}
                {detail.data.watch_until ? <p className="detail-note">{t("tasks.watchingUntil", { time: formatDate(detail.data.watch_until) })}</p> : null}
                {recoverableProcessing[0] ? (
                  <p className="detail-note danger">{enumLabel(t, "status", recoverableProcessing[0].status)}{recoverableProcessing[0].terminal_reason ? ` · ${recoverableProcessing[0].terminal_reason}` : ""}</p>
                ) : null}
                <TechnicalDetails>
                  <FieldList>
                    <Fact label={t("tasks.rootMessage")} value={detail.data.root_message_id ? <CopyValue label={t("tasks.rootMessage")} value={detail.data.root_message_id} /> : t("common.notRecorded")} />
                    <Fact label={t("tasks.taskId")} value={<CopyValue label={t("tasks.taskId")} value={detail.data.task_id} />} />
                    <Fact label={t("tasks.chat")} value={detail.data.chat_id ? <CopyValue label={t("tasks.chat")} value={detail.data.chat_id} /> : t("common.notRecorded")} />
                    <Fact label={t("tasks.watchUntil")} value={formatDate(detail.data.watch_until)} />
                    <Fact label={t("tasks.agentWorkingDir")} value={detail.data.agent_working_dir ?? t("common.notRecorded")} />
                    <Fact label={t("tasks.policySource")} value={enumLabel(t, "source", detail.data.effective_policy.policy_source)} />
                  </FieldList>
                </TechnicalDetails>
                {detail.data.recommended_actions.length ? (
                  <div className="inline-badges">
                    {detail.data.recommended_actions.map((action) => (
                      <Badge key={action} tone="warning">
                        {enumLabel(t, "action", action)}
                      </Badge>
                    ))}
                  </div>
                ) : null}
              </div>

              <div className="detail-panel">
                <div className="subsection-title">
                  <MessageSquare aria-hidden="true" size={16} />
                  <h2>{t("tasks.timeline")}</h2>
                </div>
                {detail.data.recent_messages.length ? (
                  <ul className="timeline-list">
                    {detail.data.recent_messages.map((message) => (
                      <li key={message.message_id}>
                        <button className="timeline-button" onClick={(event) => {
                          rememberPosition(event.currentTarget, "detail");
                          openTask(selectedTaskId, message.message_id);
                        }} type="button">
                          <MessageSquare aria-hidden="true" size={14} />
                          <span>{shortText(message.text, message.message_id)}</span>
                          <small>{enumLabel(t, "role", message.sender_role ?? message.role)}</small>
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="detail-note">{t("tasks.noMessages")}</p>
                )}
              </div>

              <div className="detail-panel">
                <h2>{t("tasks.state")}</h2>
                <TextareaField label={t("common.reason")} onChange={setReason} placeholder={t("tasks.reasonPlaceholder")} rows={2} value={reason} />
                <div className="command-buttons">
                  <Button disabled={!canClose || selectedTaskBusy} onClick={() => runTaskCommand("close")} tone="danger">
                    <XCircle aria-hidden="true" size={15} />
                    {t("tasks.close")}
                  </Button>
                  <Button disabled={!canReopen || selectedTaskBusy} onClick={() => runTaskCommand("reopen")} tone="info">
                    <RotateCcw aria-hidden="true" size={15} />
                    {t("tasks.reopen")}
                  </Button>
                </div>
                <TechnicalDetails summary={t("tasks.background")}>
                  <TextareaField
                    label={t("tasks.ownerBackground")}
                    onChange={(value) => {
                      if (selectedTaskId) {
                        setBackgroundDrafts((current) => ({ ...current, [selectedTaskId]: value }));
                      }
                    }}
                    placeholder={t("tasks.backgroundPlaceholder")}
                    rows={5}
                    value={backgroundDraft}
                  />
                  <p className="detail-note">
                    {t("tasks.backgroundNote")} {detail.data.task_background ? t("tasks.currentVersion", { version: detail.data.task_background.version }) : t("tasks.noBackground")}
                  </p>
                  <div className="command-buttons">
                    <Button disabled={selectedTaskBusy || !backgroundDraft.trim()} onClick={() => updateBackground(backgroundDraft)} tone="info">
                      {t("tasks.saveBackground")}
                    </Button>
                    <ConfirmButton
                      confirmLabel={t("tasks.confirmClearBackground")}
                      disabled={selectedTaskBusy || (!detail.data.task_background?.content && !backgroundDraft)}
                      onConfirm={() => {
                        // Drop the local draft instead of blanking it. Blanking it
                        // here would show an empty field before the command resolves,
                        // so a failed clear would look like a successful one; falling
                        // back to the server value shows what is actually stored.
                        if (selectedTaskId) {
                          setBackgroundDrafts((current) => {
                            const next = { ...current };
                            delete next[selectedTaskId];
                            return next;
                          });
                        }
                        updateBackground(null);
                      }}
                      tone="danger"
                    >
                      {t("tasks.clearBackground")}
                    </ConfirmButton>
                  </div>
                </TechnicalDetails>
                <CommandResultPanel result={recoveryResult ? null : commandResult} />
              </div>

              <TechnicalDetails summary={t("tasks.recovery")}>
                {recoverableProcessing.length ? (
                  <ul className="timeline-list">
                    {recoverableProcessing.map((item) => {
                      const heldRetry = item.latest_retry?.status === "queued" && item.latest_retry.policy_held;
                      const activeRetry = !heldRetry && (item.latest_retry?.status === "queued" || item.latest_retry?.status === "claimed");
                      return (
                        <li key={`${item.message_id}-${item.revision}-${item.stage}`}>
                          <RotateCcw aria-hidden="true" size={14} />
                          <span>{enumLabel(t, "stage", item.stage)} · {item.message_id} r{item.revision}</span>
                          <small title={`${item.status}${item.terminal_reason ? ` · ${item.terminal_reason}` : ""}`}>{enumLabel(t, "status", item.status)}{item.terminal_reason ? ` · ${item.terminal_reason}` : ""}</small>
                          <Button disabled={selectedTaskBusy || activeRetry} onClick={() => retryProcessing(item.message_id, item.stage)} tone="warning">
                            {heldRetry ? t("tasks.resumeRetry") : activeRetry ? t("tasks.queued") : t("tasks.retry")}
                          </Button>
                        </li>
                      );
                    })}
                  </ul>
                ) : (
                  <p className="detail-note">{t("tasks.noRetryable")}</p>
                )}
                <p className="detail-note">{t("tasks.recoveryBoundary")}</p>
                <CommandResultPanel result={recoveryResult} />
              </TechnicalDetails>

              <TechnicalDetails summary={t("tasks.approvals")}>
                {detail.data.pending_approvals.length ? (
                  <ul className="timeline-list">
                    {detail.data.pending_approvals.map((approval) => (
                      <li key={approval.approval_id}>
                        <Bell aria-hidden="true" size={14} />
                        <a href={`#approvals/${encodeURIComponent(approval.approval_id)}`}>{approval.approval_id}</a>
                        <small>{approval.is_overdue ? t("approvals.overdue") : enumLabel(t, "status", approval.status)}</small>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="detail-note">{t("tasks.noApprovals")}</p>
                )}
              </TechnicalDetails>

              <TechnicalDetails summary={t("tasks.dispatch")}>
                {detail.data.actions.length ? (
                  <ul className="timeline-list">
                    {detail.data.actions.map((action) => (
                      <li key={action.action_id}>
                        <Send aria-hidden="true" size={14} />
                        <a href={`#dispatch/${action.action_id}`}>{t("tasks.dispatchRecord", { id: action.action_id })}</a>
                        <small>{enumLabel(t, "status", action.status)}</small>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="detail-note">{t("tasks.noDispatch")}</p>
                )}
              </TechnicalDetails>

              <TechnicalDetails summary={t("tasks.agentAudit")}>
                <AgentAuditList audits={detail.data.agent_audits} />
              </TechnicalDetails>
            </>
          ) : null}
        </m.aside>

        <aside className="message-drawer workbench-pane" data-pane="evidence" hidden={!messageId} aria-label={t("tasks.messageDetail")}>
          <MessageDetailPanel messageId={messageId} token={token} />
        </aside>
      </div>
    </section>
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
