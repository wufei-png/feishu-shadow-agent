import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, ClipboardList, Send } from "lucide-react";
import { approveApproval, expireApprovals, getApproval, getTask, listApprovals, rejectApproval, sendApproval } from "../api";
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
  TextareaField
} from "../components/Primitives";
import { invalidateAfterApprovalCommand, invalidateAfterMaintenanceCommand, queryKeys, type ApprovalFilter } from "../queryKeys";
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

const approvalFilters: Array<{ value: ApprovalFilter; label: string }> = [
  { value: "pending", label: "Pending" },
  { value: "expired", label: "Expired" },
  { value: "resolved", label: "Resolved" },
  { value: "all", label: "All" }
];

export function ApprovalsScreen({ token, selectedId }: { token: string; selectedId: string | null }) {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<ApprovalFilter>("pending");
  const [selectedApprovalId, setSelectedApprovalId] = useState<string | null>(selectedId);
  const [drafts, setDrafts] = useState<Record<string, ApprovalDraft>>({});
  const [commandResults, setCommandResults] = useState<Record<string, CommandResult>>({});
  const busyApprovalIdsRef = useRef(new Set<string>());
  const [busyApprovalIds, setBusyApprovalIds] = useState<Set<string>>(new Set());
  const approvals = useQuery({
    queryKey: queryKeys.approvals({ status: filter, limit: 50, offset: 0 }),
    queryFn: () => listApprovalsForFilter(token, filter),
    enabled: Boolean(token)
  });
  const visibleApprovals = useMemo(() => {
    return approvals.data ?? [];
  }, [approvals.data]);
  const selectedApproval = visibleApprovals.find((approval) => approval.approval_id === selectedApprovalId) ?? null;
  const detail = useQuery({
    queryKey: queryKeys.approval(selectedApprovalId),
    queryFn: () => getApproval(token, selectedApprovalId ?? ""),
    enabled: Boolean(token && selectedApprovalId)
  });
  const taskId = detail.data?.task_short_id ?? null;
  const task = useQuery({
    queryKey: queryKeys.task(taskId),
    queryFn: () => getTask(token, taskId ?? ""),
    enabled: Boolean(token && taskId)
  });

  useEffect(() => {
    setSelectedApprovalId(selectedId);
  }, [selectedId]);

  useEffect(() => {
    if (!selectedApprovalId && visibleApprovals[0]) {
      setSelectedApprovalId(visibleApprovals[0].approval_id);
    }
    if (selectedApprovalId && visibleApprovals.length && !visibleApprovals.some((approval) => approval.approval_id === selectedApprovalId)) {
      setSelectedApprovalId(visibleApprovals[0].approval_id);
    }
  }, [selectedApprovalId, visibleApprovals]);

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
    return <LoadingState title="Loading approvals" />;
  }
  if (approvals.error) {
    return <ErrorState title="Approvals unavailable" error={approvals.error} />;
  }

  return (
    <section className="work-grid" aria-label="Approvals">
      <div className="work-main">
        <div className="queue-panel">
          <SectionHeader
            eyebrow="Approval Queue"
            title="Human-reviewed replies"
            badge={<Badge tone={visibleApprovals.length ? "warning" : "success"}>{visibleApprovals.length}</Badge>}
          />
          <SegmentedControl label="Approval status filter" onChange={setFilter} options={approvalFilters} value={filter} />
          {visibleApprovals.length ? (
            <div className="list-stack">
              {visibleApprovals.map((approval) => (
                <ListRow
                  badge={<Badge tone={approval.is_overdue ? "danger" : statusTone(approval.status)}>{approval.is_overdue ? "overdue" : approval.status}</Badge>}
                  key={approval.approval_id}
                  meta={`${approval.kind} · ${approval.task_short_id ?? "no task"} · ${formatDate(approval.created_at)}`}
                  onClick={() => setSelectedApprovalId(approval.approval_id)}
                  selected={approval.approval_id === selectedApprovalId}
                  title={approval.approval_id}
                >
                  <span className="row-preview">{shortText(approval.preview)}</span>
                  {postprocessBadge(approval) ? (
                    <span className="inline-badges row-actions">
                      <Badge tone={approval.postprocess_status === "needs_owner" ? "warning" : "danger"}>
                        {postprocessBadge(approval)}
                      </Badge>
                    </span>
                  ) : null}
                </ListRow>
              ))}
            </div>
          ) : (
            <EmptyState title="No approvals in this view" detail="Pending, expired, and resolved blockers stay separated for queue work." />
          )}
        </div>
      </div>

      <aside className="work-detail">
        {selectedApprovalId && detail.isLoading ? <LoadingState title="Loading approval detail" /> : null}
        {detail.error ? <ErrorState title="Approval detail unavailable" error={detail.error} /> : null}
        {detail.data ? (
          <>
            <div className="detail-panel">
              <p className="eyebrow">Approval Detail</p>
              <div className="detail-title-row">
                <h2>{detail.data.approval_id}</h2>
                <Badge tone={detail.data.is_overdue ? "danger" : statusTone(detail.data.status)}>
                  {detail.data.is_overdue ? "overdue" : detail.data.status}
                </Badge>
              </div>
              <p className="preview-copy">{shortText(detail.data.preview, "No approval preview")}</p>
              <FieldList>
                <FactRow label="Task" value={detail.data.task_short_id ?? "not linked"} />
                <FactRow label="Created" value={formatDate(detail.data.created_at)} />
                <FactRow label="Expires" value={formatDate(detail.data.expires_at)} />
                <FactRow label="Recommended" value={detail.data.recommended_action} />
              </FieldList>
              {postprocessInfo(detail.data.payload) ? (
                <div className="subsection">
                  <p className="eyebrow">Postprocess</p>
                  <FieldList>
                    <FactRow label="Status" value={String(postprocessInfo(detail.data.payload)?.status ?? "unknown")} />
                    <FactRow label="Guidance" value={postprocessInfo(detail.data.payload)?.guidance || "none"} />
                    <FactRow label="Failure" value={postprocessInfo(detail.data.payload)?.failure || "none"} />
                    <FactRow label="Approve sends" value={postprocessInfo(detail.data.payload)?.approveBehavior || "payload text"} />
                    <FactRow label="Reject behavior" value={postprocessInfo(detail.data.payload)?.rejectBehavior || "normal"} />
                  </FieldList>
                </div>
              ) : null}
              <JsonBlock value={detail.data.payload ?? {}} />
            </div>

            <div className="detail-panel">
              <p className="eyebrow">Commands</p>
              <h2>Resolve blocker</h2>
              <TextareaField
                label="Reason"
                onChange={(reason) => updateDraft({ reason })}
                placeholder="Optional audit reason"
                rows={2}
                value={selectedDraft.reason}
              />
              <div className="command-buttons">
                <Button
                  disabled={!canApprove || selectedApprovalBusy || expire.isPending}
                  onClick={() => runApprovalCommand("approve")}
                  tone="success"
                >
                  Approve
                </Button>
                <Button
                  disabled={!canReject || selectedApprovalBusy || expire.isPending}
                  onClick={() => runApprovalCommand("reject")}
                  tone="danger"
                >
                  Reject
                </Button>
                <Button
                  disabled={expire.isPending || busyApprovalIds.size > 0}
                  onClick={() => expire.mutate({ approvalId: selectedApprovalId, reason: clean(selectedDraft.reason) })}
                  tone="warning"
                >
                  Expire overdue
                </Button>
              </div>
              <TextareaField
                label="Final reply"
                onChange={(finalReply) => updateDraft({ finalReply })}
                placeholder="Send a final reply for the related task"
                rows={4}
                value={selectedDraft.finalReply}
              />
              <Button
                disabled={!canSend || !taskId || !selectedDraft.finalReply.trim() || selectedApprovalBusy || expire.isPending}
                onClick={() => runApprovalCommand("send")}
                tone="info"
              >
                <Send aria-hidden="true" size={15} />
                Send final reply
              </Button>
              <CommandResultPanel result={selectedCommandResult} />
            </div>

            <div className="detail-panel">
              <div className="subsection-title">
                <ClipboardList aria-hidden="true" size={16} />
                <h2>Related task context</h2>
              </div>
              {task.isLoading ? <p className="detail-note">Loading task context...</p> : null}
              {task.data ? (
                <>
                  <FieldList>
                    <FactRow label="Task status" value={task.data.status} />
                    <FactRow label="Chat" value={task.data.chat_id ?? "not recorded"} />
                    <FactRow label="Messages" value={task.data.message_count} />
                    <FactRow label="Policy" value={task.data.effective_policy.policy_source} />
                  </FieldList>
                  <ul className="timeline-list">
                    {task.data.recent_messages.slice(-4).map((message) => (
                      <li key={message.message_id}>
                        <Bell aria-hidden="true" size={14} />
                        <span>{shortText(message.text, message.message_id)}</span>
                        <small>{message.sender_role}</small>
                      </li>
                    ))}
                  </ul>
                </>
              ) : null}
            </div>
          </>
        ) : selectedApproval ? null : (
          <EmptyState title="Select an approval" detail="Approval payload, task context, and commands will appear here." />
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

async function listApprovalsForFilter(token: string, filter: ApprovalFilter): Promise<ApprovalSummary[]> {
  if (filter === "resolved") {
    const [approved, rejected] = await Promise.all([
      listApprovals(token, { status: "approved", limit: 50, offset: 0 }),
      listApprovals(token, { status: "rejected", limit: 50, offset: 0 })
    ]);
    return [...approved, ...rejected].sort((left, right) => String(right.created_at).localeCompare(String(left.created_at)));
  }
  const status = filter === "all" ? undefined : (filter as ApprovalStatus);
  return listApprovals(token, { status, limit: 50, offset: 0 });
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
