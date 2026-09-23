import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, ClipboardList, Send } from "lucide-react";
import { useTranslation } from "react-i18next";
import { approveApproval, expireApprovals, getApproval, getTask, listApprovals, rejectApproval, sendApproval } from "../api";
import {
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
  SectionHeader,
  SegmentedControl,
  shortText,
  statusTone,
  TechnicalDetails,
  TextareaField
} from "../components/Primitives";
import { invalidateAfterApprovalCommand, invalidateAfterMaintenanceCommand, queryKeys, type ApprovalFilter } from "../queryKeys";
import { enumLabel } from "../presentation";
import type { ApprovalDetail, ApprovalStatus, ApprovalSummary, CommandResult } from "../types";

type ApprovalDraft = {
  reason: string;
  finalReply: string;
};

type ApprovalCommandInput = {
  kind: "approve" | "reject" | "send";
  approval: ApprovalDetail;
  reason?: string;
  finalReply?: string;
  commandId: string;
};

const emptyDraft: ApprovalDraft = { reason: "", finalReply: "" };
const pageSize = 50;

export function ApprovalsScreen({ token, selectedId }: { token: string; selectedId: string | null }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<ApprovalFilter>("pending");
  const [page, setPage] = useState(0);
  const [selectedApprovalId, setSelectedApprovalId] = useState<string | null>(selectedId);
  const [drafts, setDrafts] = useState<Record<string, ApprovalDraft>>({});
  const [commandResults, setCommandResults] = useState<Record<string, CommandResult>>({});
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
  const selectedApproval = visibleApprovals.find((approval) => approval.approval_id === selectedApprovalId) ?? null;
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
    setSelectedApprovalId(selectedId);
  }, [selectedId]);

  useEffect(() => {
    if (selectedId) {
      return;
    }
    if (!selectedApprovalId && visibleApprovals[0]) {
      setSelectedApprovalId(visibleApprovals[0].approval_id);
    }
    if (selectedApprovalId && visibleApprovals.length && !visibleApprovals.some((approval) => approval.approval_id === selectedApprovalId)) {
      setSelectedApprovalId(visibleApprovals[0].approval_id);
    }
  }, [selectedApprovalId, selectedId, visibleApprovals]);

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
      return sendApproval(token, input.approval.approval_id, { ...body, final_reply: input.finalReply });
    },
    onSuccess: async (result, input) => {
      setCommandResults((current) => ({ ...current, [input.approval.approval_id]: result }));
      await invalidateAfterApprovalCommand(queryClient);
    },
    onError: (error, input) => {
      setCommandResults((current) => ({
        ...current,
        [input.approval.approval_id]: errorResult(`approval.${input.kind}`, error)
      }));
    },
    onSettled: (_result, _error, input) => {
      busyApprovalIdsRef.current.delete(input.approval.approval_id);
      setBusyApprovalIds(new Set(busyApprovalIdsRef.current));
    }
  });
  const expire = useMutation({
    mutationFn: (input: { approvalId: string | null; reason?: string }) => expireApprovals(token, { reason: input.reason }),
    onSuccess: async (result, input) => {
      if (input.approvalId) {
        setCommandResults((current) => ({ ...current, [input.approvalId as string]: result }));
      }
      await invalidateAfterMaintenanceCommand(queryClient);
    },
    onError: (error, input) => {
      if (input.approvalId) {
        setCommandResults((current) => ({
          ...current,
          [input.approvalId as string]: errorResult("maintenance.expire_approvals", error)
        }));
      }
    }
  });
  const selectedDraft = selectedApprovalId ? (drafts[selectedApprovalId] ?? emptyDraft) : emptyDraft;
  const selectedCommandResult = selectedApprovalId ? (commandResults[selectedApprovalId] ?? null) : null;
  const selectedApprovalBusy = selectedApprovalId ? busyApprovalIds.has(selectedApprovalId) : false;
  const canApprove = detail.data?.available_commands.includes(`approve ${detail.data.approval_id}`) ?? false;
  const canReject = detail.data?.available_commands.includes(`reject ${detail.data.approval_id}`) ?? false;
  const canSend = detail.data?.available_commands.some((command) => command.startsWith("send ")) ?? false;
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
      commandId: newCommandId()
    });
  }

  if (approvals.isLoading) {
    return <LoadingState title={t("approvals.loading")} />;
  }
  if (approvals.error && !approvals.data) {
    return <ErrorState title={t("approvals.unavailable")} error={approvals.error} />;
  }

  return (
    <section className="work-grid" aria-label={t("approvals.aria")}>
      <div className="work-main">
        <div className="queue-panel">
          <SectionHeader
            eyebrow={t("approvals.eyebrow")}
            title={t("approvals.title")}
            badge={<Badge tone={visibleApprovals.length ? "warning" : "success"}>{visibleApprovals.length}</Badge>}
          />
          <SegmentedControl
            label={t("approvals.filterLabel")}
            onChange={(nextFilter) => {
              setFilter(nextFilter);
              setPage(0);
            }}
            options={approvalFilters}
            value={filter}
          />
          <QueueControls
            error={approvals.error}
            hasNext={hasNextPage}
            isFetching={approvals.isFetching}
            onNext={() => setPage((current) => current + 1)}
            onPrevious={() => setPage((current) => Math.max(0, current - 1))}
            onRefresh={() => void approvals.refetch()}
            page={page}
            updatedAt={approvals.dataUpdatedAt}
          />
          {visibleApprovals.length ? (
            <div className="list-stack">
              {visibleApprovals.map((approval) => (
                <ListRow
                  badge={<Badge tone={approval.is_overdue ? "danger" : statusTone(approval.status)}>{approval.is_overdue ? t("approvals.overdue") : enumLabel(t, "status", approval.status)}</Badge>}
                  key={approval.approval_id}
                  meta={`${enumLabel(t, "kind", approval.kind)} · ${approval.task_short_id ?? t("common.notLinked")} · ${formatDate(approval.created_at)}`}
                  onClick={() => selectApproval(approval.approval_id, setSelectedApprovalId)}
                  selected={approval.approval_id === selectedApprovalId}
                  title={approval.approval_id}
                >
                  <span className="row-preview">{shortText(approval.preview)}</span>
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
        </div>
      </div>

      <aside className="work-detail">
        {selectedApprovalId && detail.isLoading ? <LoadingState title={t("approvals.detailLoading")} /> : null}
        {detail.error ? <ErrorState title={t("approvals.detailUnavailable")} error={detail.error} /> : null}
        {detail.data ? (
          <>
            <div className="detail-panel">
              <p className="eyebrow">{t("approvals.detailEyebrow")}</p>
              <div className="detail-title-row">
                <h2><CopyValue label={t("approvals.approvalId")} value={detail.data.approval_id} /></h2>
                <Badge tone={detail.data.is_overdue ? "danger" : statusTone(detail.data.status)}>
                  {detail.data.is_overdue ? t("approvals.overdue") : enumLabel(t, "status", detail.data.status)}
                </Badge>
              </div>
              <p className="preview-copy">{shortText(detail.data.preview, t("approvals.noApprovalPreview"))}</p>
              <FieldList>
                <FactRow label={t("approvals.linkedTask")} value={detail.data.task_short_id ?? t("common.notLinked")} />
                <FactRow label={t("approvals.createdAt")} value={formatDate(detail.data.created_at)} />
                <FactRow label={t("approvals.expiresAt")} value={formatDate(detail.data.expires_at)} />
                <FactRow label={t("approvals.recommendedAction")} value={enumLabel(t, "action", detail.data.recommended_action)} />
              </FieldList>
              {postprocessInfo(detail.data.payload) ? (
                <div className="subsection">
                  <p className="eyebrow">{t("approvals.postprocess")}</p>
                  <FieldList>
                    <FactRow label={t("approvals.status")} value={enumLabel(t, "status", String(postprocessInfo(detail.data.payload)?.status ?? ""), t("common.unknown"))} />
                    <FactRow label={t("approvals.guidance")} value={postprocessInfo(detail.data.payload)?.guidance || t("common.none")} />
                    <FactRow label={t("approvals.failure")} value={postprocessInfo(detail.data.payload)?.failure || t("common.none")} />
                    <FactRow label={t("approvals.approveBehavior")} value={enumLabel(t, "postprocess", postprocessInfo(detail.data.payload)?.approveBehavior, t("approvals.originalReply"))} />
                    <FactRow label={t("approvals.rejectBehavior")} value={enumLabel(t, "postprocess", postprocessInfo(detail.data.payload)?.rejectBehavior, t("common.unknown"))} />
                  </FieldList>
                </div>
              ) : null}
              <TechnicalDetails>
                <JsonBlock value={detail.data.payload ?? {}} />
              </TechnicalDetails>
            </div>

            <div className="detail-panel">
              <p className="eyebrow">{t("approvals.operations")}</p>
              <h2>{t("approvals.handle")}</h2>
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
                <Button
                  disabled={expire.isPending || busyApprovalIds.size > 0}
                  onClick={() => expire.mutate({ approvalId: selectedApprovalId, reason: clean(selectedDraft.reason) })}
                  tone="warning"
                >
                  {t("approvals.expire")}
                </Button>
              </div>
              <TextareaField
                label={t("approvals.finalReply")}
                onChange={(finalReply) => updateDraft({ finalReply })}
                placeholder={t("approvals.finalReplyPlaceholder")}
                rows={4}
                value={selectedDraft.finalReply}
              />
              <Button
                disabled={!canSend || !taskId || !selectedDraft.finalReply.trim() || selectedApprovalBusy || expire.isPending}
                onClick={() => runApprovalCommand("send")}
                tone="info"
              >
                <Send aria-hidden="true" size={15} />
                {t("approvals.sendFinalReply")}
              </Button>
              <CommandResultPanel result={selectedCommandResult} />
            </div>

            <div className="detail-panel">
              <div className="subsection-title">
                <ClipboardList aria-hidden="true" size={16} />
                <h2>{t("approvals.taskContext")}</h2>
              </div>
              {task.isLoading ? <p className="detail-note">{t("approvals.taskContextLoading")}</p> : null}
              {task.data ? (
                <>
                  <FieldList>
                    <FactRow label={t("approvals.taskStatus")} value={enumLabel(t, "status", task.data.status)} />
                    <FactRow label={t("approvals.chat")} value={task.data.chat_id ?? t("common.notRecorded")} />
                    <FactRow label={t("approvals.messageCount")} value={task.data.message_count} />
                    <FactRow label={t("approvals.policySource")} value={enumLabel(t, "source", task.data.effective_policy.policy_source)} />
                  </FieldList>
                  <ul className="timeline-list">
                    {task.data.recent_messages.slice(-4).map((message) => (
                      <li key={message.message_id}>
                        <Bell aria-hidden="true" size={14} />
                        <span>{shortText(message.text, message.message_id)}</span>
                        <small>{enumLabel(t, "role", message.sender_role)}</small>
                      </li>
                    ))}
                  </ul>
                </>
              ) : null}
            </div>
          </>
        ) : selectedApproval ? null : (
          <EmptyState title={t("approvals.selectTitle")} detail={t("approvals.selectDetail")} />
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

function selectApproval(approvalId: string, setSelectedApprovalId: (approvalId: string) => void): void {
  setSelectedApprovalId(approvalId);
  window.location.hash = `approvals/${encodeURIComponent(approvalId)}`;
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

function newCommandId(): string {
  return `console_${crypto.randomUUID()}`;
}
