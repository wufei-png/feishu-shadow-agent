import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { FileText, GitBranch, Send } from "lucide-react";
import { getMessageDetail } from "../api";
import {
  Badge,
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

export function MessageDetailPanel({ token, messageId }: { token: string; messageId: string | null }) {
  const detail = useQuery({
    queryKey: queryKeys.messageDetail(messageId),
    queryFn: () => getMessageDetail(token, messageId ?? ""),
    enabled: Boolean(token && messageId)
  });

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
