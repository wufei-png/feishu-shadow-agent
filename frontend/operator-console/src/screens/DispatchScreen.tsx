import { useEffect, useMemo, useState } from "react";
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
  SectionHeader,
  SegmentedControl,
  shortText,
  statusTone,
  TextareaField,
  TextField
} from "../components/Primitives";
import { invalidateAfterDispatchCommand, queryKeys } from "../queryKeys";
import type { ActionStatus, CommandResult } from "../types";

type DispatchFilter = ActionStatus | "all";

const dispatchFilters: Array<{ value: DispatchFilter; label: string }> = [
  { value: "failed_needs_review", label: "Needs review" },
  { value: "failed", label: "Failed" },
  { value: "sending", label: "Sending" },
  { value: "pending", label: "Pending" },
  { value: "sent", label: "Sent" },
  { value: "cancelled", label: "Cancelled" },
  { value: "all", label: "All" }
];

export function DispatchScreen({ token, selectedId }: { token: string; selectedId: string | null }) {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<DispatchFilter>("failed_needs_review");
  const [selectedActionId, setSelectedActionId] = useState<number | null>(numberOrNull(selectedId));
  const [reason, setReason] = useState("");
  const [sentMessageId, setSentMessageId] = useState("");
  const [commandResult, setCommandResult] = useState<CommandResult | null>(null);
  const actions = useQuery({
    queryKey: queryKeys.dispatchActions({ status: filter, limit: 50, offset: 0 }),
    queryFn: () => listDispatchActions(token, { status: filter === "all" ? undefined : filter, limit: 50, offset: 0 }),
    enabled: Boolean(token)
  });
  const rows = useMemo(() => actions.data ?? [], [actions.data]);
  const detail = useQuery({
    queryKey: queryKeys.dispatchAction(selectedActionId),
    queryFn: () => getDispatchAction(token, selectedActionId ?? 0),
    enabled: Boolean(token && selectedActionId !== null)
  });

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

  const retry = useMutation({
    mutationFn: () => retryDispatchAction(token, selectedActionId ?? 0, { reason: clean(reason) }),
    onSuccess: async (result) => {
      setCommandResult(result);
      await invalidateAfterDispatchCommand(queryClient);
    },
    onError: (error) => setCommandResult(errorResult("dispatch.retry", error))
  });
  const cancel = useMutation({
    mutationFn: () => cancelDispatchAction(token, selectedActionId ?? 0, { reason: clean(reason) }),
    onSuccess: async (result) => {
      setCommandResult(result);
      await invalidateAfterDispatchCommand(queryClient);
    },
    onError: (error) => setCommandResult(errorResult("dispatch.cancel", error))
  });
  const markSent = useMutation({
    mutationFn: () =>
      markDispatchSent(token, selectedActionId ?? 0, {
        sent_message_id: sentMessageId,
        reason: clean(reason)
      }),
    onSuccess: async (result) => {
      setCommandResult(result);
      await invalidateAfterDispatchCommand(queryClient);
    },
    onError: (error) => setCommandResult(errorResult("dispatch.mark_sent", error))
  });

  if (actions.isLoading) {
    return <LoadingState title="Loading dispatch actions" />;
  }
  if (actions.error) {
    return <ErrorState title="Dispatch actions unavailable" error={actions.error} />;
  }

  return (
    <section className="work-grid" aria-label="Dispatch">
      <div className="work-main">
        <div className="queue-panel">
          <SectionHeader
            eyebrow="Dispatch Recovery"
            title="Send action readback"
            badge={<Badge tone={rows.length ? "warning" : "success"}>{rows.length}</Badge>}
          />
          <SegmentedControl label="Dispatch status filter" onChange={setFilter} options={dispatchFilters} value={filter} />
          {rows.length ? (
            <div className="list-stack">
              {rows.map((action) => (
                <ListRow
                  badge={<Badge tone={statusTone(action.status)}>{action.status}</Badge>}
                  key={action.action_id}
                  meta={`${action.kind} · ${action.task_short_id ?? "no task"} · ${formatDate(action.updated_at)}`}
                  onClick={() => setSelectedActionId(action.action_id)}
                  selected={action.action_id === selectedActionId}
                  title={`Action ${action.action_id}`}
                >
                  <span className="row-preview">{action.target_message_id ?? "no target message"}</span>
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
            <EmptyState title="No dispatch actions in this view" detail="Failed, sending, pending, sent, and cancelled actions are filterable here." />
          )}
        </div>
      </div>

      <aside className="work-detail">
        {selectedActionId !== null && detail.isLoading ? <LoadingState title="Loading action detail" /> : null}
        {detail.error ? <ErrorState title="Dispatch detail unavailable" error={detail.error} /> : null}
        {detail.data ? (
          <>
            <div className="detail-panel">
              <p className="eyebrow">Action Detail</p>
              <div className="detail-title-row">
                <h2>Action {detail.data.action.action_id}</h2>
                <Badge tone={statusTone(detail.data.action.status)}>{detail.data.action.status}</Badge>
              </div>
              <FieldList>
                <FactRow label="Kind" value={detail.data.action.kind} />
                <FactRow label="Task" value={detail.data.action.task_short_id ?? "not linked"} />
                <FactRow label="Target message" value={detail.data.action.target_message_id ?? "not recorded"} />
                <FactRow label="Updated" value={formatDate(detail.data.action.updated_at)} />
              </FieldList>
              <JsonBlock value={detail.data.action.payload} />
            </div>

            <div className="detail-panel">
              <p className="eyebrow">Readback</p>
              <h2>Attempt summary</h2>
              <FieldList>
                <FactRow label="Attempts" value={String(detail.data.readback_summary.attempt_count ?? 0)} />
                <FactRow label="Latest status" value={String(detail.data.readback_summary.latest_status ?? "none")} />
                <FactRow label="Sent message" value={String(detail.data.readback_summary.sent_message_id ?? "not recorded")} />
                <FactRow label="Readback message" value={String(detail.data.readback_summary.readback_message_id ?? "not recorded")} />
              </FieldList>
              {detail.data.attempts.length ? (
                <ul className="timeline-list">
                  {detail.data.attempts.map((attempt) => (
                    <li key={attempt.id}>
                      <Send aria-hidden="true" size={14} />
                      <span>{attempt.status}</span>
                      <small>{attempt.error_stage ?? attempt.sent_message_id ?? "no error stage"}</small>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="detail-note">No dispatch attempts recorded.</p>
              )}
            </div>

            <div className="detail-panel">
              <p className="eyebrow">Commands</p>
              <h2>Recover action</h2>
              <TextareaField label="Reason" onChange={setReason} placeholder="Optional recovery note" rows={2} value={reason} />
              <div className="command-buttons">
                <Button disabled={!hasRecommendedCommand(detail.data.recommended_actions, "dispatch retry") || retry.isPending} onClick={() => retry.mutate()} tone="warning">
                  <RotateCcw aria-hidden="true" size={15} />
                  Retry
                </Button>
                <Button disabled={!hasRecommendedCommand(detail.data.recommended_actions, "dispatch cancel") || cancel.isPending} onClick={() => cancel.mutate()} tone="danger">
                  <XCircle aria-hidden="true" size={15} />
                  Cancel
                </Button>
              </div>
              <TextField label="Sent message ID" onChange={setSentMessageId} placeholder="om_xxx from Feishu readback" value={sentMessageId} />
              <Button
                disabled={
                  !sentMessageId.trim() ||
                  !hasRecommendedCommand(detail.data.recommended_actions, "dispatch mark-sent") ||
                  markSent.isPending
                }
                onClick={() => markSent.mutate()}
                tone="success"
              >
                <ShieldCheck aria-hidden="true" size={15} />
                Mark sent
              </Button>
              <CommandResultPanel result={commandResult} />
            </div>

            <div className="detail-panel">
              <p className="eyebrow">Recorded Result</p>
              <h2>Persisted outcome</h2>
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
          <EmptyState title="Select a dispatch action" detail="Payload, attempts, readback, and recovery commands will appear here." />
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
