import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, RotateCcw, Send, ShieldCheck, XCircle } from "lucide-react";
import { m } from "motion/react";
import { useTranslation } from "react-i18next";
import { cancelDispatchAction, getDispatchAction, listDispatchActions, markDispatchSent, retryDispatchAction } from "../api";
import {
  Fact,
  Badge,
  Button,
  CommandResultPanel,
  CopyValue,
  EmptyState,
  ErrorState,
  FieldList,
  formatDate,
  JsonBlock,
  ListRow,
  LoadingState,
  QueueControls,
  SegmentedControl,
  shortText,
  statusTone,
  TechnicalDetails,
  TextareaField,
  TextField
} from "../components/Primitives";
import { useWorkbenchNavigation } from "../components/useWorkbenchNavigation";
import { invalidateAfterDispatchCommand, queryKeys } from "../queryKeys";
import { panelTransition, panelVariants } from "../motionTokens";
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
  const [filter, setFilter] = useState<DispatchFilter>(initialFilter ?? (selectedId ? "all" : "failed_needs_review"));
  const [page, setPage] = useState(0);
  const [openedActionId, setOpenedActionId] = useState(numberOrNull(selectedId));
  const pane = openedActionId === null ? "list" : "detail";
  const { heading, rememberPosition } = useWorkbenchNavigation(pane);
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
  const selectedActionId = openedActionId ?? rows[0]?.action_id ?? null;
  const hasNextPage = (actions.data?.length ?? 0) > pageSize;
  const detail = useQuery({
    queryKey: queryKeys.dispatchAction(selectedActionId),
    queryFn: () => getDispatchAction(token, selectedActionId ?? 0),
    enabled: Boolean(token && selectedActionId !== null),
    refetchInterval: 15_000
  });

  useEffect(() => {
    if (initialFilter) {
      setFilter(initialFilter);
      setPage(0);
    }
  }, [initialFilter]);

  useEffect(() => {
    setOpenedActionId(numberOrNull(selectedId));
  }, [selectedId]);

  function openAction(actionId: number | null): void {
    setOpenedActionId(actionId);
    const query = window.location.hash.split("?")[1];
    const path = actionId === null ? "dispatch" : `dispatch/${actionId}`;
    window.location.hash = query ? `${path}?${query}` : path;
  }

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
  const action = detail.data?.action;
  const postprocess = action ? postprocessInfo(action.payload) : null;
  const recommendations = detail.data?.recommended_actions ?? [];
  const canRetry = hasRecommendedCommand(recommendations, "dispatch retry");
  const canCancel = hasRecommendedCommand(recommendations, "dispatch cancel");
  const canMarkSent = hasRecommendedCommand(recommendations, "dispatch mark-sent");
  const mainFilters: DispatchFilter[] = ["failed_needs_review", "failed", "all"];
  const moreFilters: DispatchFilter[] = ["attention", "sending", "pending", "sent", "cancelled"];
  const changeFilter = (next: DispatchFilter) => {
    setFilter(next);
    setPage(0);
  };

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

  return (
    <section className="workbench" data-pane={pane} aria-label={t("dispatch.aria")}>
      <header className="workbench-header">
        <h1 ref={heading} tabIndex={-1}>{t("dispatch.title")}</h1>
        {openedActionId !== null ? (
          <button className="button workbench-back" onClick={() => openAction(null)} type="button">
            <ArrowLeft aria-hidden="true" size={16} />
            {t("dispatch.backToList")}
          </button>
        ) : null}
      </header>
      <div className="work-grid">
        <div className="work-main workbench-pane" data-pane="list">
          <div className="queue-panel">
            <SegmentedControl
              label={t("dispatch.filterLabel")}
              onChange={changeFilter}
              options={mainFilters.map((value) => ({ value, label: t(`dispatch.filters.${value}`) }))}
              value={filter}
            />
            <TechnicalDetails summary={moreFilters.includes(filter) ? t("dispatch.moreSelected", { status: t(`dispatch.filters.${filter}`) }) : t("dispatch.moreFilters")}>
              <SegmentedControl
                label={t("dispatch.moreFilters")}
                onChange={changeFilter}
                options={moreFilters.map((value) => ({ value, label: t(`dispatch.filters.${value}`) }))}
                value={filter}
              />
            </TechnicalDetails>
            <QueueControls
              error={actions.error}
              isFetching={actions.isFetching}
              onRefresh={() => void actions.refetch()}
              pagination={{
                page,
                hasNext: hasNextPage,
                onNext: () => setPage((current) => current + 1),
                onPrevious: () => setPage((current) => Math.max(0, current - 1))
              }}
              updatedAt={actions.dataUpdatedAt}
            />
            {actions.isLoading ? <LoadingState title={t("dispatch.loading")} /> : null}
            {actions.error && !actions.data ? <ErrorState title={t("dispatch.unavailable")} error={actions.error} /> : null}
            {rows.length ? (
              <div className="list-stack">
                {rows.map((action) => (
                  <ListRow
                    badge={<Badge tone={statusTone(action.status)}>{enumLabel(t, "status", action.status)}</Badge>}
                    key={action.action_id}
                    meta={`${enumLabel(t, "kind", action.kind)} · ${action.task_short_id ?? t("dispatch.noTask")} · ${formatDate(action.updated_at)}`}
                    onClick={(event) => {
                      rememberPosition(event.currentTarget);
                      openAction(action.action_id);
                    }}
                    selected={action.action_id === selectedActionId}
                    title={t("dispatch.record", { id: action.action_id })}
                  />
                ))}
              </div>
            ) : actions.data ? (
              <EmptyState title={t("dispatch.noItems")} detail={t("dispatch.noItemsDetail")} />
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
          key={selectedActionId ?? "none"}
          initial="hidden"
          transition={panelTransition}
          variants={panelVariants}
        >
          {selectedActionId !== null && detail.isLoading ? <LoadingState title={t("dispatch.detailLoading")} /> : null}
          {detail.error ? (
            <div>
              <ErrorState title={t("dispatch.detailUnavailable")} error={detail.error} />
              <Button onClick={() => void detail.refetch()}>{t("common.refresh")}</Button>
            </div>
          ) : null}
          {detail.data && action ? (
            <>
              <div className="detail-panel">
                <div className="detail-title-row">
                  <h2>{t("dispatch.record", { id: action.action_id })}</h2>
                  <Badge tone={statusTone(action.status)}>{enumLabel(t, "status", action.status)}</Badge>
                </div>
                <FieldList>
                  <Fact label={t("dispatch.kind")} value={enumLabel(t, "kind", action.kind)} />
                  <Fact label={t("dispatch.linkedTask")} value={action.task_short_id ? <a href={`#tasks/${encodeURIComponent(action.task_short_id)}`}>{action.task_short_id}</a> : t("common.notLinked")} />
                  <Fact label={t("dispatch.latestStatus")} value={enumLabel(t, "status", String(detail.data.readback_summary.latest_status ?? ""), t("common.none"))} />
                  <Fact label={t("dispatch.readback")} value={detail.data.readback_summary.readback_message_id ? t("dispatch.readbackRecorded") : t("common.notRecorded")} />
                </FieldList>
                <TechnicalDetails>
                  <FieldList>
                    <Fact label={t("dispatch.actionId")} value={<CopyValue label={t("dispatch.actionId")} value={String(action.action_id)} />} />
                    <Fact label={t("dispatch.targetMessage")} value={action.target_message_id ? <CopyValue label={t("dispatch.targetMessage")} value={action.target_message_id} /> : t("common.notRecorded")} />
                    <Fact label={t("dispatch.updatedAt")} value={formatDate(action.updated_at)} />
                  </FieldList>
                  {postprocess ? (
                    <div className="subsection">
                      <h3>{t("dispatch.postprocess")}</h3>
                      <FieldList>
                        <Fact label={t("dispatch.applied")} value={postprocess.applied} />
                        <Fact label={t("dispatch.guidance")} value={postprocess.guidance || t("common.none")} />
                        <Fact label={t("dispatch.original")} value={shortText(postprocess.original, t("common.notRecorded"))} />
                        <Fact label={t("dispatch.final")} value={shortText(postprocess.final, t("common.notRecorded"))} />
                      </FieldList>
                    </div>
                  ) : null}
                  <JsonBlock value={action.payload} />
                </TechnicalDetails>
              </div>

              <TechnicalDetails summary={t("dispatch.readbackEvidence")}>
                <FieldList>
                  <Fact label={t("dispatch.attemptCount")} value={String(detail.data.readback_summary.attempt_count ?? 0)} />
                  <Fact label={t("dispatch.sentMessage")} value={detail.data.readback_summary.sent_message_id ? <CopyValue label={t("dispatch.sentMessage")} value={String(detail.data.readback_summary.sent_message_id)} /> : t("common.notRecorded")} />
                  <Fact label={t("dispatch.readbackMessage")} value={detail.data.readback_summary.readback_message_id ? <CopyValue label={t("dispatch.readbackMessage")} value={String(detail.data.readback_summary.readback_message_id)} /> : t("common.notRecorded")} />
                </FieldList>
                <h3>{t("dispatch.attempts")}</h3>
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
              </TechnicalDetails>

              {canRetry || canCancel || canMarkSent || commandResult || action.policy_held ? <div className="detail-panel">
                <h2>{t("dispatch.handle")}</h2>
                {action.policy_held ? <p className="detail-note">{t("dispatch.policyHeld")}</p> : null}
                <TextareaField
                  label={t("common.reason")}
                  onChange={(reason) => updateDraft({ reason })}
                  placeholder={t("dispatch.reasonPlaceholder")}
                  rows={2}
                  value={selectedDraft.reason}
                />
                <div className="command-buttons">
                  {canRetry ? <Button
                    disabled={selectedActionBusy}
                    onClick={() => runDispatchCommand("retry")}
                    tone="warning"
                  >
                    <RotateCcw aria-hidden="true" size={15} />
                    {t("dispatch.retry")}
                  </Button> : null}
                  {canCancel ? <Button
                    disabled={selectedActionBusy}
                    onClick={() => runDispatchCommand("cancel")}
                    tone="danger"
                  >
                    <XCircle aria-hidden="true" size={15} />
                    {t("dispatch.cancel")}
                  </Button> : null}
                </div>
                {canMarkSent ? <div className="work-main">
                  <TextField
                    label={t("dispatch.sentMessageId")}
                    onChange={(sentMessageId) => updateDraft({ sentMessageId })}
                    placeholder={t("dispatch.sentMessagePlaceholder")}
                    value={selectedDraft.sentMessageId}
                  />
                  <Button
                    disabled={
                      !selectedDraft.sentMessageId.trim() || selectedActionBusy
                    }
                    onClick={() => runDispatchCommand("mark_sent")}
                    tone="success"
                  >
                    <ShieldCheck aria-hidden="true" size={15} />
                    {t("dispatch.markSent")}
                  </Button>
                </div> : null}
                <CommandResultPanel result={commandResult} />
              </div> : null}

              <TechnicalDetails summary={t("dispatch.persistedOutcome")}>
                <JsonBlock value={action.result} />
              </TechnicalDetails>
              {recommendations.length ? (
                <TechnicalDetails summary={t("dispatch.recommendations")}>
                  <ul className="warning-list">
                    {recommendations.map((item) => <li key={item}>{enumLabel(t, "action", item)} <code>{item}</code></li>)}
                  </ul>
                </TechnicalDetails>
              ) : null}
            </>
          ) : null}
        </m.aside>
      </div>
    </section>
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
