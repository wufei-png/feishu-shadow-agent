import { useMutation } from "@tanstack/react-query";
import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import type { ReactNode } from "react";
import { Bot, FileText, GitBranch, PackageSearch, RotateCcw, Send } from "lucide-react";
import { getMessageDetail, replayMessage } from "../api";
import {
  Badge,
  Button,
  CommandResultPanel,
  EmptyState,
  ErrorState,
  FieldList,
  formatDate,
  JsonBlock,
  LoadingState,
  shortText,
  statusTone
} from "../components/Primitives";
import { queryKeys } from "../queryKeys";
import type { CommandResult } from "../types";
import { AgentAuditList } from "./AgentAuditList";

export function MessageDetailPanel({ token, messageId }: { token: string; messageId: string | null }) {
  const detail = useQuery({
    queryKey: queryKeys.messageDetail(messageId),
    queryFn: () => getMessageDetail(token, messageId ?? ""),
    enabled: Boolean(token && messageId)
  });
  const replay = useMutation({
    mutationFn: (targetMessageId: string) => replayMessage(token, targetMessageId),
    onError: (error) => errorResult("message.replay_dry_run", error)
  });
  const replayResult =
    replay.variables === messageId
      ? (replay.data as CommandResult | undefined) ?? (replay.error ? errorResult("message.replay_dry_run", replay.error) : null)
      : null;

  useEffect(() => {
    replay.reset();
  }, [messageId]);

  if (!messageId) {
    return <EmptyState title="Select a message" detail="Message processing context will appear here." />;
  }
  if (detail.isLoading) {
    return <LoadingState title="Loading message detail" />;
  }
  if (detail.error) {
    return <ErrorState title="Message detail unavailable" error={detail.error} />;
  }
  if (!detail.data) {
    return <EmptyState title="Message not found" detail="The local store did not return this message detail." />;
  }

  return (
    <div className="message-detail-stack">
      <div className="detail-panel">
        <p className="eyebrow">Message Detail</p>
        <div className="detail-title-row">
          <h2>{detail.data.message.message_id}</h2>
          <Badge tone={statusTone(detail.data.message.sender_role)}>{detail.data.message.sender_role ?? "unknown"}</Badge>
        </div>
        <p className="preview-copy">{shortText(detail.data.message.text, "No message text")}</p>
        <FieldList>
          <FactRow label="Chat" value={detail.data.message.chat_id ?? "not recorded"} />
          <FactRow label="Sent" value={formatDate(detail.data.message.sent_at)} />
          <FactRow label="Thread" value={detail.data.message.thread_id ?? "none"} />
          <FactRow label="Reply to" value={detail.data.message.reply_to_message_id ?? "none"} />
        </FieldList>
      </div>

      <div className="detail-panel">
        <div className="subsection-title">
          <GitBranch aria-hidden="true" size={16} />
          <h2>Routing and tasks</h2>
        </div>
        <ul className="timeline-list">
          {detail.data.routing_audits.map((audit, index) => (
            <li key={`${String(audit.id ?? index)}`}>
              <GitBranch aria-hidden="true" size={14} />
              <span>{String(audit.route ?? "route")}</span>
              <small>{String(audit.route_reason ?? "")}</small>
            </li>
          ))}
        </ul>
        {detail.data.task_summaries.length ? (
          <div className="inline-badges">
            {detail.data.task_summaries.map((task) => (
              <Badge key={task.task_id} tone={statusTone(task.status)}>
                {task.task_id}
              </Badge>
            ))}
          </div>
        ) : null}
      </div>

      <div className="detail-panel">
        <div className="subsection-title">
          <RotateCcw aria-hidden="true" size={16} />
          <h2>Replay dry-run</h2>
        </div>
        <Button disabled={replay.isPending} onClick={() => replay.mutate(messageId)} tone="info">
          <RotateCcw aria-hidden="true" size={15} />
          Replay dry-run
        </Button>
        <CommandResultPanel result={replayResult} />
      </div>

      <div className="detail-panel">
        <div className="subsection-title">
          <PackageSearch aria-hidden="true" size={16} />
          <h2>Processing</h2>
        </div>
        {detail.data.processing.length ? (
          <ul className="timeline-list">
            {detail.data.processing.map((item) => (
              <li key={item.id}>
                <PackageSearch aria-hidden="true" size={14} />
                <span>{item.stage}</span>
                <small>{`${item.status}${item.terminal_reason ? ` · ${item.terminal_reason}` : ""}`}</small>
              </li>
            ))}
          </ul>
        ) : (
          <p className="detail-note">No processing stages recorded for this message.</p>
        )}
      </div>

      <div className="detail-panel">
        <div className="subsection-title">
          <PackageSearch aria-hidden="true" size={16} />
          <h2>Resources</h2>
        </div>
        {detail.data.resources.length ? (
          <ul className="audit-list">
            {detail.data.resources.map((resource) => (
              <li key={resource.id}>
                <div className="audit-row-head">
                  <Badge tone={statusTone(resource.download_status)}>{resource.download_status}</Badge>
                  <strong>{resource.resource_type}</strong>
                  <span>{resource.file_key}</span>
                </div>
                <FieldList>
                  <FactRow label="Path" value={resource.path ?? "not recorded"} />
                  <FactRow label="Path exists" value={resource.path_exists === null ? "not checked" : resource.path_exists ? "yes" : "no"} />
                  <FactRow label="SHA-256" value={resource.sha256_short ?? "not recorded"} />
                </FieldList>
                {Object.keys(resource.raw_summary).length ? (
                  <details>
                    <summary>Raw summary</summary>
                    <JsonBlock value={resource.raw_summary} />
                  </details>
                ) : null}
                <details>
                  <summary>Raw JSON</summary>
                  <JsonBlock value={resource.raw} />
                </details>
              </li>
            ))}
          </ul>
        ) : (
          <p className="detail-note">No downloadable resources recorded for this message.</p>
        )}
      </div>

      <div className="detail-panel">
        <div className="subsection-title">
          <Bot aria-hidden="true" size={16} />
          <h2>Agent audits</h2>
        </div>
        <AgentAuditList audits={detail.data.agent_audits} compact />
      </div>

      <div className="detail-panel">
        <div className="subsection-title">
          <FileText aria-hidden="true" size={16} />
          <h2>Approvals</h2>
        </div>
        {detail.data.approvals.length ? (
          <ul className="timeline-list">
            {detail.data.approvals.map((approval) => (
              <li key={approval.approval_id}>
                <FileText aria-hidden="true" size={14} />
                <span>{approval.approval_id}</span>
                <small>{approval.status}</small>
              </li>
            ))}
          </ul>
        ) : (
          <p className="detail-note">No approvals recorded for this message context.</p>
        )}
      </div>

      <div className="detail-panel">
        <div className="subsection-title">
          <Send aria-hidden="true" size={16} />
          <h2>Dispatch outcomes</h2>
        </div>
        {detail.data.recorded_dispatch_outcomes.length ? (
          <JsonBlock value={detail.data.recorded_dispatch_outcomes} />
        ) : (
          <p className="detail-note">No recorded dispatch outcome.</p>
        )}
      </div>
    </div>
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
