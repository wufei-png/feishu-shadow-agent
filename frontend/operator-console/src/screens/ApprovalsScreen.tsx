import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Send } from "lucide-react";
import { m } from "motion/react";
import { failedCommandResult } from "../commandResult";
import { useTranslation } from "react-i18next";
import { approveApproval, expireApprovals, getApproval, getTask, listApprovals, rejectApproval, sendApproval } from "../api";
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
  JsonBlock,
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
import { invalidateAfterApprovalCommand, invalidateAfterMaintenanceCommand, queryKeys, type ApprovalFilter } from "../queryKeys";
import { panelTransition, panelVariants } from "../motionTokens";
import { enumLabel } from "../presentation";
import type { ApprovalDetail, ApprovalStatus, ApprovalSummary, CommandResult } from "../types";

type ApprovalDraft = {
  reason: string;
  finalReply: string;
  replyLocation: "" | "main" | "thread";
};

type ApprovalCommandInput = {
  kind: "approve" | "reject" | "send";
  approval: ApprovalDetail;
  reason?: string;
  finalReply?: string;
  replyLocation?: "main" | "thread";
  commandId: string;
};

const emptyDraft: ApprovalDraft = { reason: "", finalReply: "", replyLocation: "" };
const pageSize = 50;

export function ApprovalsScreen({ token, selectedId }: { token: string; selectedId: string | null }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<ApprovalFilter>("pending");
  const [page, setPage] = useState(0);
  const [openedApprovalId, setOpenedApprovalId] = useState(selectedId);
  const { heading, rememberPosition } = useWorkbenchNavigation(openedApprovalId ? "detail" : "list", "(max-width: 900px)");
  const [drafts, setDrafts] = useState<Record<string, ApprovalDraft>>({});
  const [commandResults, setCommandResults] = useState<Record<string, CommandResult>>({});
  const [expiryReason, setExpiryReason] = useState("");
  const busyApprovalIdsRef = useRef(new Set<string>());
  const [busyApprovalIds, setBusyApprovalIds] = useState<Set<string>>(new Set());
  const approvals = useQuery({
    queryKey: queryKeys.approvals({ status: approvalStatuses(filter), limit: pageSize + 1, offset: page * pageSize }),
    queryFn: () => listApprovalsForFilter(token, filter, page),
    enabled: Boolean(token),
    refetchInterval: 15_000
  });
  const visibleApprovals = useMemo(() => {
    return (approvals.data ?? []).slice(0, pageSize);
  }, [approvals.data]);
  const hasNextPage = (approvals.data?.length ?? 0) > pageSize;
  const selectedApprovalId = openedApprovalId ?? visibleApprovals[0]?.approval_id ?? null;
  const detail = useQuery({
    queryKey: queryKeys.approval(selectedApprovalId),
    queryFn: () => getApproval(token, selectedApprovalId ?? ""),
    enabled: Boolean(token && selectedApprovalId),
    refetchInterval: 15_000
  });
  const taskId = detail.data?.task_short_id ?? null;
  const task = useQuery({
    queryKey: queryKeys.task(taskId),
    queryFn: () => getTask(token, taskId ?? ""),
    enabled: Boolean(token && taskId),
    refetchInterval: 15_000
  });

  useEffect(() => {
    setOpenedApprovalId(selectedId);
  }, [selectedId]);

  const approvalCommand = useMutation({
    mutationFn: (input: ApprovalCommandInput) => {
      const body = {
        command_id: input.commandId,
        expected_task_id: input.approval.task_id,
        expected_source_message_id: input.approval.source_message_id,
        expected_source_revision: input.approval.source_revision,
        reason: input.reason
      };
      if (input.kind === "approve") {
        return approveApproval(token, input.approval.approval_id, body);
      }
      if (input.kind === "reject") {
        return rejectApproval(token, input.approval.approval_id, body);
      }
      return sendApproval(token, input.approval.approval_id, {
        ...body,
        final_reply: input.finalReply,
        reply_location: input.replyLocation
      });
    },
    onSuccess: async (result, input) => {
      setCommandResults((current) => ({ ...current, [input.approval.approval_id]: result }));
      await invalidateAfterApprovalCommand(queryClient);
    },
    onError: (error, input) => {
      setCommandResults((current) => ({
        ...current,
        [input.approval.approval_id]: failedCommandResult(`approval.${input.kind}`, error, t("common.requestFailed"))
      }));
    },
    onSettled: (_result, _error, input) => {
      busyApprovalIdsRef.current.delete(input.approval.approval_id);
      setBusyApprovalIds(new Set(busyApprovalIdsRef.current));
    }
  });
  const expire = useMutation({
    mutationFn: (reason?: string) => expireApprovals(token, { reason }),
    onSuccess: async () => {
      await invalidateAfterMaintenanceCommand(queryClient);
    }
  });
  const expiryResult = expire.data ?? (expire.error
    ? failedCommandResult("maintenance.expire_approvals", expire.error, t("common.requestFailed"))
    : null);
  const expiredCount = expiryResult?.result.expired_approvals;
  const selectedDraft = selectedApprovalId ? (drafts[selectedApprovalId] ?? emptyDraft) : emptyDraft;
  const selectedCommandResult = selectedApprovalId ? (commandResults[selectedApprovalId] ?? null) : null;
  const selectedApprovalBusy = selectedApprovalId ? busyApprovalIds.has(selectedApprovalId) : false;
  const canApprove = detail.data?.available_commands.includes(`approve ${detail.data.approval_id}`) ?? false;
  const canReject = detail.data?.available_commands.includes(`reject ${detail.data.approval_id}`) ?? false;
  const canSend = detail.data?.available_commands.some((command) => command.startsWith("send ")) ?? false;
  const existingLocation = replyLocation(detail.data?.payload);
  const locationChoices = allowedLocations(detail.data?.payload);
  const chosenLocation = existingLocation || selectedDraft.replyLocation;
  const approvalPostprocess = postprocessInfo(detail.data?.payload);
  const replyText = detail.data?.payload?.text;
  const suggestedReply = typeof replyText === "string" && replyText.trim() ? replyText : detail.data?.preview;
  const approvalFilters: Array<{ value: ApprovalFilter; label: string }> = [
    { value: "pending", label: t("approvals.filters.pending") },
    { value: "expired", label: t("approvals.filters.expired") },
    { value: "resolved", label: t("approvals.filters.resolved") },
    { value: "all", label: t("approvals.filters.all") }
  ];

  function updateDraft(change: Partial<ApprovalDraft>): void {
    if (!selectedApprovalId) {
      return;
    }
    setDrafts((current) => ({
      ...current,
      [selectedApprovalId]: { ...emptyDraft, ...current[selectedApprovalId], ...change }
    }));
  }

  function openApproval(approvalId: string | null): void {
    setOpenedApprovalId(approvalId);
    window.location.hash = approvalId ? `approvals/${encodeURIComponent(approvalId)}` : "approvals";
  }

  function runApprovalCommand(kind: ApprovalCommandInput["kind"]): void {
    const approval = detail.data;
    if (!approval || busyApprovalIdsRef.current.has(approval.approval_id) || expire.isPending) {
      return;
    }
    busyApprovalIdsRef.current.add(approval.approval_id);
    setBusyApprovalIds(new Set(busyApprovalIdsRef.current));
    approvalCommand.mutate({
      kind,
      approval,
      reason: clean(selectedDraft.reason),
      finalReply: selectedDraft.finalReply,
      replyLocation: selectedDraft.replyLocation || undefined,
      commandId: newCommandId()
    });
  }

  return (
    <section aria-label={t("approvals.aria")} className="approvals-screen" data-detail-open={openedApprovalId !== null}>
      <header className="workbench-header approval-workbench-header">
        <h1 ref={heading} tabIndex={-1}>
          <span className="approval-queue-title">{t("approvals.title")}</span>
          {openedApprovalId ? <span className="approval-detail-title">{t("approvals.detailEyebrow")}</span> : null}
        </h1>
        {openedApprovalId ? (
          <button className="button approval-back" onClick={() => openApproval(null)} type="button">
            <ArrowLeft aria-hidden="true" size={16} />
            {t("approvals.backToQueue")}
          </button>
        ) : null}
      </header>

      <div className="approval-layout">
        <section aria-label={t("approvals.title")} className="queue-panel approval-queue">
          <div className="approval-queue-toolbar">
            <SegmentedControl
              label={t("approvals.filterLabel")}
              onChange={(nextFilter) => {
                setFilter(nextFilter);
                setPage(0);
              }}
              options={approvalFilters}
              value={filter}
            />
            <ConfirmButton
              confirmLabel={t("approvals.expireScope")}
              disabled={expire.isPending || busyApprovalIds.size > 0}
              onConfirm={() => expire.mutate(clean(expiryReason))}
              tone="warning"
            >
              {t("approvals.expire")}
            </ConfirmButton>
          </div>
          <TechnicalDetails summary={t("approvals.expiryReason")}>
            <TextareaField label={t("approvals.expiryReason")} onChange={setExpiryReason} rows={2} value={expiryReason} />
          </TechnicalDetails>
          {typeof expiredCount === "number" ? <p className="detail-note" role="status">{t("approvals.expiredCount", { count: expiredCount })}</p> : null}
          <CommandResultPanel result={expiryResult} />
          {approvals.isLoading ? <LoadingState title={t("approvals.loading")} /> : null}
          {approvals.error && !approvals.data ? (
            <div>
              <ErrorState title={t("approvals.unavailable")} error={approvals.error} />
              <Button onClick={() => void approvals.refetch()}>{t("common.refresh")}</Button>
            </div>
          ) : null}
          {approvals.data ? (
            <>
              <QueueControls
                error={approvals.error}
                isFetching={approvals.isFetching}
                onRefresh={() => void approvals.refetch()}
                pagination={{
                  page,
                  hasNext: hasNextPage,
                  onNext: () => setPage((current) => current + 1),
                  onPrevious: () => setPage((current) => Math.max(0, current - 1))
                }}
                updatedAt={approvals.dataUpdatedAt}
              />
              {visibleApprovals.length ? (
                <div className="list-stack">
                  {visibleApprovals.map((approval) => (
                    <ListRow
                      badge={<Badge tone={approval.is_overdue ? "danger" : statusTone(approval.status)}>{approval.is_overdue ? t("approvals.overdue") : enumLabel(t, "status", approval.status)}</Badge>}
                      key={approval.approval_id}
                      meta={`${enumLabel(t, "kind", approval.kind)} · ${approval.task_short_id ?? t("common.notLinked")} · ${formatDate(approval.created_at)}`}
                      onClick={(event) => {
                        rememberPosition(event.currentTarget, "list");
                        openApproval(approval.approval_id);
                      }}
                      selected={approval.approval_id === selectedApprovalId}
                      title={shortText(approval.preview, approval.task_short_id ?? t("approvals.noApprovalPreview"))}
                    >
                      {postprocessBadge(approval) ? (
                        <span className="inline-badges row-actions">
                          <Badge tone={approval.postprocess_status === "needs_owner" ? "warning" : "danger"}>
                            {enumLabel(t, "postprocess", postprocessBadge(approval))}
                          </Badge>
                        </span>
                      ) : null}
                    </ListRow>
                  ))}
                </div>
              ) : (
                <EmptyState title={t("approvals.noItems")} detail={t("approvals.noItemsDetail")} />
              )}
            </>
          ) : null}
        </section>

        <m.div
          animate="visible"
          className="work-detail approval-detail"
          initial="hidden"
          key={selectedApprovalId ?? "none"}
          transition={panelTransition}
          variants={panelVariants}
        >
          {selectedApprovalId && detail.isLoading ? <LoadingState title={t("approvals.detailLoading")} /> : null}
          {detail.error ? (
            <div>
              <ErrorState title={t("approvals.detailUnavailable")} error={detail.error} />
              <Button onClick={() => void detail.refetch()}>{t("common.refresh")}</Button>
            </div>
          ) : null}
          {detail.data ? (
            <article className="detail-panel approval-review">
              <div className="approval-review-heading">
                <div>
                  <h2>{t("approvals.suggestedReply")}</h2>
                  <p className="detail-note">{t("approvals.reviewWindow", { time: formatDate(detail.data.expires_at) })}</p>
                </div>
                <Badge tone={detail.data.is_overdue ? "danger" : statusTone(detail.data.status)}>
                  {detail.data.is_overdue ? t("approvals.overdue") : enumLabel(t, "status", detail.data.status)}
                </Badge>
              </div>

              <section className="approval-section">
                <p className="approval-reply">{suggestedReply?.trim() ? suggestedReply : t("approvals.noApprovalPreview")}</p>
                {approvalPostprocess ? (
                  <div className="approval-postprocess">
                    <h4>{t("approvals.postprocess")}</h4>
                    <FieldList>
                      <Fact label={t("approvals.status")} value={enumLabel(t, "status", String(approvalPostprocess.status ?? ""), t("common.unknown"))} />
                      <Fact label={t("approvals.guidance")} value={approvalPostprocess.guidance || t("common.none")} />
                      <Fact label={t("approvals.failure")} value={approvalPostprocess.failure || t("common.none")} />
                      <Fact label={t("approvals.approveBehavior")} value={enumLabel(t, "postprocess", approvalPostprocess.approveBehavior, t("approvals.originalReply"))} />
                      <Fact label={t("approvals.rejectBehavior")} value={enumLabel(t, "postprocess", approvalPostprocess.rejectBehavior, t("common.unknown"))} />
                    </FieldList>
                  </div>
                ) : null}
              </section>

              <section className="approval-section approval-destination">
                <h3>{t("approvals.destination")}</h3>
                <FieldList>
                  <Fact label={t("approvals.linkedTask")} value={taskId ? <a href={`#tasks/${encodeURIComponent(taskId)}`}>{task.data?.task_label || taskId}</a> : t("common.notLinked")} />
                  <Fact label={t("approvals.replyLocation")} value={chosenLocation ? t(chosenLocation === "thread" ? "approvals.locationThread" : "approvals.locationMain") : t("approvals.locationUnconfirmed")} />
                </FieldList>
                {!existingLocation && locationChoices.length ? (
                  <SegmentedControl
                    label={t("approvals.replyLocation")}
                    value={selectedDraft.replyLocation}
                    options={locationChoices.map((location) => ({
                      value: location,
                      label: t(location === "thread" ? "approvals.locationThread" : "approvals.locationMain")
                    }))}
                    onChange={(replyLocation) => updateDraft({ replyLocation })}
                  />
                ) : null}
                {!existingLocation && !locationChoices.length ? <p className="detail-note">{t("approvals.locationUnavailable")}</p> : null}
                <TechnicalDetails>
                  <FieldList>
                    <Fact label={t("approvals.approvalId")} value={<CopyValue label={t("approvals.approvalId")} value={detail.data.approval_id} />} />
                    <Fact label={t("approvals.sourceMessage")} value={detail.data.source_message_id ? <CopyValue label={t("approvals.sourceMessage")} value={detail.data.source_message_id} /> : t("common.notRecorded")} />
                    <Fact label={t("approvals.replyTarget")} value={replyTarget(detail.data.payload) ?? t("common.notRecorded")} />
                    <Fact label={t("approvals.linkedTask")} value={taskId ?? t("common.notLinked")} />
                    <Fact label={t("approvals.createdAt")} value={formatDate(detail.data.created_at)} />
                    <Fact label={t("approvals.recommendedAction")} value={enumLabel(t, "action", detail.data.recommended_action)} />
                    {task.data ? (
                      <>
                        <Fact label={t("approvals.taskStatus")} value={enumLabel(t, "status", task.data.status)} />
                        <Fact label={t("approvals.chat")} value={task.data.chat_id ?? t("common.notRecorded")} />
                        <Fact label={t("approvals.messageCount")} value={task.data.message_count} />
                        <Fact label={t("approvals.policySource")} value={enumLabel(t, "source", task.data.effective_policy.policy_source)} />
                      </>
                    ) : null}
                  </FieldList>
                  <JsonBlock value={detail.data.payload ?? {}} />
                </TechnicalDetails>
              </section>

              {taskId ? (
                <TechnicalDetails summary={t("approvals.relatedMessages")}>
                  {task.isLoading ? <p className="detail-note">{t("approvals.taskContextLoading")}</p> : null}
                  {task.error ? <p className="detail-note danger">{t("approvals.taskContextUnavailable")}</p> : null}
                  {task.data?.recent_messages.length ? (
                    <ul className="approval-messages">
                      {task.data.recent_messages.slice(-4).map((message) => (
                        <li key={message.message_id}>
                          <span>{shortText(message.text, message.message_id)}</span>
                          <small>{enumLabel(t, "role", message.sender_role)}</small>
                        </li>
                      ))}
                    </ul>
                  ) : task.data ? <p className="detail-note">{t("approvals.noRelatedMessages")}</p> : null}
                </TechnicalDetails>
              ) : null}

              <section className="approval-section approval-operations">
                <h3>{t("approvals.handle")}</h3>
                <TextareaField
                  label={t("common.reason")}
                  onChange={(reason) => updateDraft({ reason })}
                  placeholder={t("approvals.reasonPlaceholder")}
                  rows={2}
                  value={selectedDraft.reason}
                />
                <div className="command-buttons">
                  <Button
                    disabled={!canApprove || selectedApprovalBusy || expire.isPending}
                    onClick={() => runApprovalCommand("approve")}
                    tone="success"
                  >
                    {t("approvals.approve")}
                  </Button>
                  <Button
                    disabled={!canReject || selectedApprovalBusy || expire.isPending}
                    onClick={() => runApprovalCommand("reject")}
                    tone="danger"
                  >
                    {t("approvals.reject")}
                  </Button>
                </div>
                <TextareaField
                  label={t("approvals.finalReply")}
                  onChange={(finalReply) => updateDraft({ finalReply })}
                  placeholder={t("approvals.finalReplyPlaceholder")}
                  rows={4}
                  value={selectedDraft.finalReply}
                />
                <div className="approval-send">
                  <Button
                    disabled={!canSend || !taskId || !selectedDraft.finalReply.trim() || (!existingLocation && !selectedDraft.replyLocation) || selectedApprovalBusy || expire.isPending}
                    onClick={() => runApprovalCommand("send")}
                    tone="info"
                  >
                    <Send aria-hidden="true" size={15} />
                    {t("approvals.sendFinalReply")}
                  </Button>
                </div>
                <CommandResultPanel result={selectedCommandResult} />
              </section>
            </article>
          ) : null}
        </m.div>
      </div>
    </section>
  );
}


function replyLocation(payload: Record<string, unknown> | undefined): "main" | "thread" | null {
  const destination = payload?.reply_destination;
  if (!destination || typeof destination !== "object") return null;
  const location = (destination as Record<string, unknown>).location;
  return location === "main" || location === "thread" ? location : null;
}

function replyTarget(payload: Record<string, unknown> | undefined): string | null {
  return typeof payload?.reply_target_message_id === "string" ? payload.reply_target_message_id : null;
}

function allowedLocations(payload: Record<string, unknown> | undefined): Array<"main" | "thread"> {
  const value = payload?.allowed_reply_locations;
  return Array.isArray(value) ? value.filter((item): item is "main" | "thread" => item === "main" || item === "thread") : [];
}

function clean(value: string): string | undefined {
  const trimmed = value.trim();
  return trimmed || undefined;
}

function postprocessBadge(approval: ApprovalSummary): string | null {
  if (approval.postprocess_status === "failed") {
    return "postprocess_failed";
  }
  if (approval.postprocess_status === "needs_owner") {
    return "postprocess_needs_owner";
  }
  return null;
}

function postprocessInfo(payload: Record<string, unknown> | undefined): {
  status: unknown;
  guidance: string;
  failure: string;
  approveBehavior: string;
  rejectBehavior: string;
} | null {
  const value = payload?.postprocess;
  if (!value || typeof value !== "object") {
    return null;
  }
  const postprocess = value as Record<string, unknown>;
  const guidance = Array.isArray(postprocess.enabled_guidance) ? postprocess.enabled_guidance.join(", ") : "";
  return {
    status: postprocess.status,
    guidance,
    failure: typeof postprocess.failure_reason === "string" ? postprocess.failure_reason : "",
    approveBehavior: typeof postprocess.fallback === "string" ? postprocess.fallback : "postprocessed_candidate",
    rejectBehavior: payload?.keep_watching_on_reject === true ? "keeps task watching" : "normal"
  };
}

async function listApprovalsForFilter(token: string, filter: ApprovalFilter, page: number): Promise<ApprovalSummary[]> {
  return listApprovals(token, {
    status: approvalStatuses(filter),
    limit: pageSize + 1,
    offset: page * pageSize
  });
}

function approvalStatuses(filter: ApprovalFilter): ApprovalStatus | ApprovalStatus[] | undefined {
  if (filter === "all") {
    return undefined;
  }
  if (filter === "resolved") {
    return ["approved", "rejected"];
  }
  return filter;
}

function newCommandId(): string {
  return `console_${crypto.randomUUID()}`;
}
