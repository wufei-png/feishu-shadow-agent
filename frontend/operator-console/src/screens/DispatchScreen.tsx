import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw, Send, ShieldCheck, XCircle } from "lucide-react";
import { useTranslation } from "react-i18next";
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
import { enumLabel } from "../presentation";
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

export function DispatchScreen({ token, selectedId, initialFilter }: { token: string; selectedId: string | null; initialFilter?: DispatchFilter }) {
  const { t } = useTranslation();
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
  const dispatchFilters: Array<{ value: DispatchFilter; label: string }> = [
    { value: "attention", label: t("dispatch.filters.attention") },
    { value: "failed_needs_review", label: t("dispatch.filters.failed_needs_review") },
    { value: "failed", label: t("dispatch.filters.failed") },
    { value: "sending", label: t("dispatch.filters.sending") },
    { value: "pending", label: t("dispatch.filters.pending") },
    { value: "sent", label: t("dispatch.filters.sent") },
    { value: "cancelled", label: t("dispatch.filters.cancelled") },
    { value: "all", label: t("dispatch.filters.all") }
  ];

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
    return <LoadingState title={t("dispatch.loading")} />;
  }
  if (actions.error && !actions.data) {
    return <ErrorState title={t("dispatch.unavailable")} error={actions.error} />;
  }

  return (
    <section className="work-grid" aria-label={t("dispatch.aria")}>
      <div className="work-main">
        <div className="queue-panel">
          <SectionHeader
            eyebrow={t("dispatch.eyebrow")}
            title={t("dispatch.title")}
            badge={<Badge tone={rows.length ? "warning" : "success"}>{rows.length}</Badge>}
          />
          <SegmentedControl
            label={t("dispatch.filterLabel")}
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
                  badge={<Badge tone={statusTone(action.status)}>{enumLabel(t, "status", action.status)}</Badge>}
                  key={action.action_id}
                  meta={`${enumLabel(t, "kind", action.kind)} · ${action.task_short_id ?? t("dispatch.noTask")} · ${formatDate(action.updated_at)}`}
                  onClick={() => selectAction(action.action_id, setSelectedActionId)}
                  selected={action.action_id === selectedActionId}
                  title={t("dispatch.record", { id: action.action_id })}
                >
                  <span className="row-preview">{action.target_message_id ?? t("dispatch.noTarget")}</span>
                  {(action.recommended_actions ?? []).length ? (
                    <span className="inline-badges row-actions">
                      {(action.recommended_actions ?? []).slice(0, 2).map((recommendedAction) => (
                        <Badge key={recommendedAction} tone="warning">
                          {enumLabel(t, "action", recommendedAction, shortText(recommendedAction, recommendedAction))}
                        </Badge>
                      ))}
                    </span>
                  ) : null}
                </ListRow>
              ))}
            </div>
          ) : (
            <EmptyState title={t("dispatch.noItems")} detail={t("dispatch.noItemsDetail")} />
          )}
        </div>
      </div>

      <aside className="work-detail">
        {selectedActionId !== null && detail.isLoading ? <LoadingState title={t("dispatch.detailLoading")} /> : null}
        {detail.error ? <ErrorState title={t("dispatch.detailUnavailable")} error={detail.error} /> : null}
        {detail.data ? (
          <>
            <div className="detail-panel">
              <p className="eyebrow">{t("dispatch.detailEyebrow")}</p>
              <div className="detail-title-row">
                <h2>{t("dispatch.record", { id: detail.data.action.action_id })}</h2>
                <Badge tone={statusTone(detail.data.action.status)}>{enumLabel(t, "status", detail.data.action.status)}</Badge>
              </div>
              <FieldList>
                <FactRow label={t("dispatch.kind")} value={enumLabel(t, "kind", detail.data.action.kind)} />
                <FactRow label={t("dispatch.linkedTask")} value={detail.data.action.task_short_id ?? t("common.notLinked")} />
                <FactRow label={t("dispatch.targetMessage")} value={detail.data.action.target_message_id ?? t("common.notRecorded")} />
                <FactRow label={t("dispatch.updatedAt")} value={formatDate(detail.data.action.updated_at)} />
              </FieldList>
              {postprocessInfo(detail.data.action.payload) ? (
                <div className="subsection">
                  <p className="eyebrow">{t("dispatch.postprocess")}</p>
                  <FieldList>
                    <FactRow label={t("dispatch.applied")} value={postprocessInfo(detail.data.action.payload)?.applied ?? t("common.unknown")} />
                    <FactRow label={t("dispatch.guidance")} value={postprocessInfo(detail.data.action.payload)?.guidance || t("common.none")} />
                    <FactRow label={t("dispatch.original")} value={shortText(postprocessInfo(detail.data.action.payload)?.original, t("common.notRecorded"))} />
                    <FactRow label={t("dispatch.final")} value={shortText(postprocessInfo(detail.data.action.payload)?.final, t("common.notRecorded"))} />
                  </FieldList>
                </div>
              ) : null}
              <JsonBlock value={detail.data.action.payload} />
            </div>

            <div className="detail-panel">
              <p className="eyebrow">{t("dispatch.readback")}</p>
              <h2>{t("dispatch.attempts")}</h2>
              <FieldList>
                <FactRow label={t("dispatch.attemptCount")} value={String(detail.data.readback_summary.attempt_count ?? 0)} />
                <FactRow label={t("dispatch.latestStatus")} value={enumLabel(t, "status", String(detail.data.readback_summary.latest_status ?? ""), t("common.none"))} />
                <FactRow label={t("dispatch.sentMessage")} value={String(detail.data.readback_summary.sent_message_id ?? t("common.notRecorded"))} />
                <FactRow label={t("dispatch.readbackMessage")} value={String(detail.data.readback_summary.readback_message_id ?? t("common.notRecorded"))} />
              </FieldList>
              {detail.data.attempts.length ? (
                <ul className="timeline-list">
                  {detail.data.attempts.map((attempt) => (
                    <li key={attempt.id}>
                      <Send aria-hidden="true" size={14} />
                      <span>{enumLabel(t, "status", attempt.status)}</span>
                      <small>{attempt.error_stage ? enumLabel(t, "stage", attempt.error_stage) : attempt.sent_message_id ?? t("dispatch.noErrorStage")}</small>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="detail-note">{t("dispatch.noAttempts")}</p>
              )}
            </div>

            <div className="detail-panel">
              <p className="eyebrow">{t("dispatch.recovery")}</p>
              <h2>{t("dispatch.handle")}</h2>
              <TextareaField
                label={t("common.reason")}
                onChange={(reason) => updateDraft({ reason })}
                placeholder={t("dispatch.reasonPlaceholder")}
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
                  {t("dispatch.retry")}
                </Button>
                <Button
                  disabled={!hasRecommendedCommand(detail.data.recommended_actions, "dispatch cancel") || selectedActionBusy}
                  onClick={() => runDispatchCommand("cancel")}
                  tone="danger"
                >
                  <XCircle aria-hidden="true" size={15} />
                  {t("dispatch.cancel")}
                </Button>
              </div>
              <TextField
                label={t("dispatch.sentMessageId")}
                onChange={(sentMessageId) => updateDraft({ sentMessageId })}
                placeholder={t("dispatch.sentMessagePlaceholder")}
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
                {t("dispatch.markSent")}
              </Button>
              <CommandResultPanel result={commandResult} />
            </div>

            <div className="detail-panel">
              <p className="eyebrow">{t("dispatch.recordedResult")}</p>
              <h2>{t("dispatch.persistedOutcome")}</h2>
              <JsonBlock value={detail.data.action.result} />
              {(detail.data.recommended_actions ?? []).length ? (
                <div className="inline-badges">
                  {(detail.data.recommended_actions ?? []).map((action) => (
                    <Badge key={action} tone="warning">
                      {enumLabel(t, "action", action, shortText(action, action))}
                    </Badge>
                  ))}
                </div>
              ) : null}
            </div>
          </>
        ) : (
          <EmptyState title={t("dispatch.selectTitle")} detail={t("dispatch.selectDetail")} />
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
