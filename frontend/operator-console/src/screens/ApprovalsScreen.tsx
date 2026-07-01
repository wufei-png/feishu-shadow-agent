import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, ClipboardList, Send } from "lucide-react";
import { approveApproval, expireApprovals, getApproval, getTask, listApprovals, rejectApproval, sendTask } from "../api";
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
import type { ApprovalStatus, ApprovalSummary, CommandResult } from "../types";

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
  const [reason, setReason] = useState("");
  const [finalReply, setFinalReply] = useState("");
  const [commandResult, setCommandResult] = useState<CommandResult | null>(null);
  const approvals = useQuery({
    queryKey: queryKeys.approvals({ status: filter, limit: 50, offset: 0 }),
    queryFn: () => listApprovalsForFilter(token, filter),
    enabled: Boolean(token)
  });
  const visibleApprovals = useMemo(() => {
    return approvals.data ?? [];
  }, [approvals.data, filter]);
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

  const approve = useMutation({
    mutationFn: (approvalId: string) => approveApproval(token, approvalId, { reason: clean(reason) }),
    onSuccess: async (result) => {
      setCommandResult(result);
      await invalidateAfterApprovalCommand(queryClient);
    },
    onError: (error) => setCommandResult(errorResult("approval.approve", error))
  });
  const reject = useMutation({
    mutationFn: (approvalId: string) => rejectApproval(token, approvalId, { reason: clean(reason) }),
    onSuccess: async (result) => {
      setCommandResult(result);
      await invalidateAfterApprovalCommand(queryClient);
    },
    onError: (error) => setCommandResult(errorResult("approval.reject", error))
  });
  const send = useMutation({
    mutationFn: () => sendTask(token, taskId ?? "", { final_reply: finalReply, reason: clean(reason) }),
    onSuccess: async (result) => {
      setCommandResult(result);
      await invalidateAfterApprovalCommand(queryClient);
    },
    onError: (error) => setCommandResult(errorResult("approval.send", error))
  });
  const expire = useMutation({
    mutationFn: () => expireApprovals(token, { reason: clean(reason) }),
    onSuccess: async (result) => {
      setCommandResult(result);
      await invalidateAfterMaintenanceCommand(queryClient);
    },
    onError: (error) => setCommandResult(errorResult("maintenance.expire_approvals", error))
  });
  const canApprove = detail.data?.available_commands.includes(`approve ${detail.data.approval_id}`) ?? false;
  const canReject = detail.data?.available_commands.includes(`reject ${detail.data.approval_id}`) ?? false;
  const canSend = detail.data?.available_commands.some((command) => command.startsWith("send ")) ?? false;

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
              <JsonBlock value={detail.data.payload ?? {}} />
            </div>

            <div className="detail-panel">
              <p className="eyebrow">Commands</p>
              <h2>Resolve blocker</h2>
              <TextareaField label="Reason" onChange={setReason} placeholder="Optional audit reason" rows={2} value={reason} />
              <div className="command-buttons">
                <Button
                  disabled={!canApprove || approve.isPending}
                  onClick={() => approve.mutate(detail.data.approval_id)}
                  tone="success"
                >
                  Approve
                </Button>
                <Button
                  disabled={!canReject || reject.isPending}
                  onClick={() => reject.mutate(detail.data.approval_id)}
                  tone="danger"
                >
                  Reject
                </Button>
                <Button disabled={expire.isPending} onClick={() => expire.mutate()} tone="warning">
                  Expire overdue
                </Button>
              </div>
              <TextareaField
                label="Final reply"
                onChange={setFinalReply}
                placeholder="Send a final reply for the related task"
                rows={4}
                value={finalReply}
              />
              <Button disabled={!canSend || !taskId || !finalReply.trim() || send.isPending} onClick={() => send.mutate()} tone="info">
                <Send aria-hidden="true" size={15} />
                Send final reply
              </Button>
              <CommandResultPanel result={commandResult} />
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
