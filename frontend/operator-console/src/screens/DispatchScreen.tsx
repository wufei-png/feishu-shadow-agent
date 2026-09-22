import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw, Send, ShieldCheck, XCircle } from "lucide-react";
import { cancelDispatchAction, getDispatchAction, listDispatchActions, markDispatchSent, retryDispatchAction } from "../api";
import {
  Badge,
  Button,
  CommandResultPanel,
  EmptyState,
  ErrorState,
  FieldList,
  formatDate,
  JsonBlock,
  ListRow,
  LoadingState,
  QueueControls,
  SectionHeader,
  SegmentedControl,
  shortText,
  statusTone,
  TextareaField,
  TextField
} from "../components/Primitives";
import { invalidateAfterDispatchCommand, queryKeys } from "../queryKeys";
import type { ActionStatus, CommandResult } from "../types";

type DispatchFilter = ActionStatus | "all" | "attention";

type DispatchDraft = {
  reason: string;
  sentMessageId: string;
};

type DispatchCommandInput = {
  kind: "retry" | "cancel" | "mark_sent";
  actionId: number;
  reason?: string;
  sentMessageId: string;
};

const emptyDraft: DispatchDraft = { reason: "", sentMessageId: "" };
const pageSize = 50;

const dispatchFilters: Array<{ value: DispatchFilter; label: string }> = [
  { value: "attention", label: "待办相关" },
  { value: "failed_needs_review", label: "待核实" },
  { value: "failed", label: "失败" },
  { value: "sending", label: "发送中" },
  { value: "pending", label: "待发送" },
  { value: "sent", label: "已发送" },
  { value: "cancelled", label: "已取消" },
  { value: "all", label: "全部" }
];

export function DispatchScreen({ token, selectedId, initialFilter }: { token: string; selectedId: string | null; initialFilter?: DispatchFilter }) {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<DispatchFilter>(initialFilter ?? "failed_needs_review");
  const [page, setPage] = useState(0);
  const [selectedActionId, setSelectedActionId] = useState<number | null>(numberOrNull(selectedId));
  const [drafts, setDrafts] = useState<Record<number, DispatchDraft>>({});
  const [commandResults, setCommandResults] = useState<Record<number, CommandResult>>({});
  const busyActionIdsRef = useRef(new Set<number>());
  const [busyActionIds, setBusyActionIds] = useState<Set<number>>(new Set());
  const actions = useQuery({
    queryKey: queryKeys.dispatchActions({ status: filter, limit: pageSize + 1, offset: page * pageSize }),
    queryFn: () => listDispatchActions(token, { status: filter === "all" ? undefined : filter === "attention" ? ["failed_needs_review", "failed", "sending"] : filter, limit: pageSize + 1, offset: page * pageSize }),
    enabled: Boolean(token),
    refetchInterval: 15_000
  });
  const rows = useMemo(() => (actions.data ?? []).slice(0, pageSize), [actions.data]);
  const hasNextPage = (actions.data?.length ?? 0) > pageSize;
  const detail = useQuery({
    queryKey: queryKeys.dispatchAction(selectedActionId),
    queryFn: () => getDispatchAction(token, selectedActionId ?? 0),
    enabled: Boolean(token && selectedActionId !== null),
    refetchInterval: 15_000
  });

  useEffect(() => {
    setFilter(initialFilter ?? "failed_needs_review");
    setPage(0);
  }, [initialFilter]);

  useEffect(() => {
    const routeActionId = numberOrNull(selectedId);
    setSelectedActionId(routeActionId);
    if (routeActionId !== null) {
      setFilter("all");
    }
  }, [selectedId]);

  useEffect(() => {
    if (selectedId) {
      return;
    }
    if (selectedActionId === null && rows[0]) {
      setSelectedActionId(rows[0].action_id);
    }
    if (selectedActionId !== null && rows.length && !rows.some((action) => action.action_id === selectedActionId)) {
      setSelectedActionId(rows[0].action_id);
    }
  }, [rows, selectedActionId, selectedId]);

  const dispatchCommand = useMutation({
    mutationFn: (input: DispatchCommandInput) => {
      if (input.kind === "retry") {
        return retryDispatchAction(token, input.actionId, { reason: input.reason });
      }
      if (input.kind === "cancel") {
        return cancelDispatchAction(token, input.actionId, { reason: input.reason });
      }
      return markDispatchSent(token, input.actionId, {
        sent_message_id: input.sentMessageId,
        reason: input.reason
      });
    },
    onSuccess: async (result, input) => {
      setCommandResults((current) => ({ ...current, [input.actionId]: result }));
      await invalidateAfterDispatchCommand(queryClient);
    },
    onError: (error, input) => {
      setCommandResults((current) => ({
        ...current,
        [input.actionId]: errorResult(`dispatch.${input.kind}`, error)
      }));
    },
    onSettled: (_result, _error, input) => {
      busyActionIdsRef.current.delete(input.actionId);
      setBusyActionIds(new Set(busyActionIdsRef.current));
    }
  });
  const selectedDraft = selectedActionId === null ? emptyDraft : (drafts[selectedActionId] ?? emptyDraft);
  const commandResult = selectedActionId === null ? null : (commandResults[selectedActionId] ?? null);
  const selectedActionBusy = selectedActionId === null ? false : busyActionIds.has(selectedActionId);

  function updateDraft(change: Partial<DispatchDraft>): void {
    if (selectedActionId === null) {
      return;
    }
    setDrafts((current) => ({
      ...current,
      [selectedActionId]: { ...emptyDraft, ...current[selectedActionId], ...change }
    }));
  }

  function runDispatchCommand(kind: DispatchCommandInput["kind"]): void {
    const actionId = detail.data?.action.action_id;
    if (actionId === undefined || busyActionIdsRef.current.has(actionId)) {
      return;
    }
    busyActionIdsRef.current.add(actionId);
    setBusyActionIds(new Set(busyActionIdsRef.current));
    dispatchCommand.mutate({
      kind,
      actionId,
      reason: clean(selectedDraft.reason),
      sentMessageId: selectedDraft.sentMessageId
    });
  }

  if (actions.isLoading) {
    return <LoadingState title="正在读取发送记录" />;
  }
  if (actions.error && !actions.data) {
    return <ErrorState title="无法读取发送记录" error={actions.error} />;
  }

  return (
    <section className="work-grid" aria-label="发送记录">
      <div className="work-main">
        <div className="queue-panel">
          <SectionHeader
            eyebrow="发送核实与恢复"
            title="发送记录"
            badge={<Badge tone={rows.length ? "warning" : "success"}>{rows.length}</Badge>}
          />
          <SegmentedControl
            label="发送状态筛选"
            onChange={(nextFilter) => {
              setFilter(nextFilter);
              setPage(0);
            }}
            options={dispatchFilters}
            value={filter}
          />
          <QueueControls
            error={actions.error}
            hasNext={hasNextPage}
            isFetching={actions.isFetching}
            onNext={() => setPage((current) => current + 1)}
            onPrevious={() => setPage((current) => Math.max(0, current - 1))}
            onRefresh={() => void actions.refetch()}
            page={page}
            updatedAt={actions.dataUpdatedAt}
          />
          {rows.length ? (
            <div className="list-stack">
              {rows.map((action) => (
                <ListRow
                  badge={<Badge tone={statusTone(action.status)}>{action.status}</Badge>}
                  key={action.action_id}
                  meta={`${action.kind} · ${action.task_short_id ?? "未关联任务"} · ${formatDate(action.updated_at)}`}
                  onClick={() => selectAction(action.action_id, setSelectedActionId)}
                  selected={action.action_id === selectedActionId}
                  title={`发送记录 ${action.action_id}`}
                >
                  <span className="row-preview">{action.target_message_id ?? "未记录目标消息"}</span>
                  {(action.recommended_actions ?? []).length ? (
                    <span className="inline-badges row-actions">
                      {(action.recommended_actions ?? []).slice(0, 2).map((recommendedAction) => (
                        <Badge key={recommendedAction} tone="warning">
                          {shortText(recommendedAction, recommendedAction)}
                        </Badge>
                      ))}
                    </span>
                  ) : null}
                </ListRow>
              ))}
            </div>
          ) : (
            <EmptyState title="当前筛选下没有发送记录" detail="可切换上方状态，查看失败、发送中、待发送或已发送的记录。" />
          )}
        </div>
      </div>

      <aside className="work-detail">
        {selectedActionId !== null && detail.isLoading ? <LoadingState title="正在读取发送详情" /> : null}
        {detail.error ? <ErrorState title="无法读取发送详情" error={detail.error} /> : null}
        {detail.data ? (
          <>
            <div className="detail-panel">
              <p className="eyebrow">发送详情</p>
              <div className="detail-title-row">
                <h2>发送记录 {detail.data.action.action_id}</h2>
                <Badge tone={statusTone(detail.data.action.status)}>{detail.data.action.status}</Badge>
              </div>
              <FieldList>
                <FactRow label="类型" value={detail.data.action.kind} />
                <FactRow label="关联任务" value={detail.data.action.task_short_id ?? "未关联"} />
                <FactRow label="目标消息" value={detail.data.action.target_message_id ?? "未记录"} />
                <FactRow label="更新时间" value={formatDate(detail.data.action.updated_at)} />
              </FieldList>
              {postprocessInfo(detail.data.action.payload) ? (
                <div className="subsection">
                  <p className="eyebrow">回复后处理</p>
                  <FieldList>
                    <FactRow label="已应用" value={postprocessInfo(detail.data.action.payload)?.applied ?? "未知"} />
                    <FactRow label="处理提示" value={postprocessInfo(detail.data.action.payload)?.guidance || "无"} />
                    <FactRow label="原始内容" value={shortText(postprocessInfo(detail.data.action.payload)?.original, "未记录")} />
                    <FactRow label="最终内容" value={shortText(postprocessInfo(detail.data.action.payload)?.final, "未记录")} />
                  </FieldList>
                </div>
              ) : null}
              <JsonBlock value={detail.data.action.payload} />
            </div>

            <div className="detail-panel">
              <p className="eyebrow">发送回读</p>
              <h2>尝试记录</h2>
              <FieldList>
                <FactRow label="尝试次数" value={String(detail.data.readback_summary.attempt_count ?? 0)} />
                <FactRow label="最近状态" value={String(detail.data.readback_summary.latest_status ?? "无")} />
                <FactRow label="已发送消息" value={String(detail.data.readback_summary.sent_message_id ?? "未记录")} />
                <FactRow label="回读消息" value={String(detail.data.readback_summary.readback_message_id ?? "未记录")} />
              </FieldList>
              {detail.data.attempts.length ? (
                <ul className="timeline-list">
                  {detail.data.attempts.map((attempt) => (
                    <li key={attempt.id}>
                      <Send aria-hidden="true" size={14} />
                      <span>{attempt.status}</span>
                      <small>{attempt.error_stage ?? attempt.sent_message_id ?? "无错误阶段"}</small>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="detail-note">尚无发送尝试记录。</p>
              )}
            </div>

            <div className="detail-panel">
              <p className="eyebrow">恢复操作</p>
              <h2>处理发送记录</h2>
              <TextareaField
                label="原因"
                onChange={(reason) => updateDraft({ reason })}
                placeholder="可选；记录本次恢复操作的原因"
                rows={2}
                value={selectedDraft.reason}
              />
              <div className="command-buttons">
                <Button
                  disabled={!hasRecommendedCommand(detail.data.recommended_actions, "dispatch retry") || selectedActionBusy}
                  onClick={() => runDispatchCommand("retry")}
                  tone="warning"
                >
                  <RotateCcw aria-hidden="true" size={15} />
                  重试发送
                </Button>
                <Button
                  disabled={!hasRecommendedCommand(detail.data.recommended_actions, "dispatch cancel") || selectedActionBusy}
                  onClick={() => runDispatchCommand("cancel")}
                  tone="danger"
                >
                  <XCircle aria-hidden="true" size={15} />
                  取消发送
                </Button>
              </div>
              <TextField
                label="已发送消息 ID"
                onChange={(sentMessageId) => updateDraft({ sentMessageId })}
                placeholder="填写飞书回读得到的 om_xxx"
                value={selectedDraft.sentMessageId}
              />
              <Button
                disabled={
                  !selectedDraft.sentMessageId.trim() ||
                  !hasRecommendedCommand(detail.data.recommended_actions, "dispatch mark-sent") ||
                  selectedActionBusy
                }
                onClick={() => runDispatchCommand("mark_sent")}
                tone="success"
              >
                <ShieldCheck aria-hidden="true" size={15} />
                标记为已发送
              </Button>
              <CommandResultPanel result={commandResult} />
            </div>

            <div className="detail-panel">
              <p className="eyebrow">已记录结果</p>
              <h2>持久化结果</h2>
              <JsonBlock value={detail.data.action.result} />
              {(detail.data.recommended_actions ?? []).length ? (
                <div className="inline-badges">
                  {(detail.data.recommended_actions ?? []).map((action) => (
                    <Badge key={action} tone="warning">
                      {shortText(action, action)}
                    </Badge>
                  ))}
                </div>
              ) : null}
            </div>
          </>
        ) : (
          <EmptyState title="选择一条发送记录" detail="此处会显示内容、发送尝试、回读和恢复操作。" />
        )}
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

function hasRecommendedCommand(actions: string[], command: string): boolean {
  return actions.some((action) => action.startsWith(command));
}

function numberOrNull(value: string | null): number | null {
  if (!value) {
    return null;
  }
  const parsed = Number(value);
  return Number.isInteger(parsed) ? parsed : null;
}

function clean(value: string): string | undefined {
  const trimmed = value.trim();
  return trimmed || undefined;
}

function postprocessInfo(payload: Record<string, unknown>): {
  applied: string;
  guidance: string;
  original: string;
  final: string;
} | null {
  const value = payload.postprocess;
  if (!value || typeof value !== "object") {
    return null;
  }
  const postprocess = value as Record<string, unknown>;
  const guidance = Array.isArray(postprocess.enabled_guidance) ? postprocess.enabled_guidance.join(", ") : "";
  return {
    applied: String(postprocess.applied ?? false),
    guidance,
    original: typeof postprocess.original_reply === "string" ? postprocess.original_reply : "",
    final: typeof postprocess.final_reply === "string" ? postprocess.final_reply : ""
  };
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

function selectAction(actionId: number, setSelectedActionId: (actionId: number) => void): void {
  setSelectedActionId(actionId);
  window.location.hash = `dispatch/${actionId}`;
}
