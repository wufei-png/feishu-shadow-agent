import { Bot, Clock } from "lucide-react";
import {
  Badge,
  EmptyState,
  FieldList,
  formatDate,
  JsonBlock,
  shortText
} from "../components/Primitives";
import type { AgentAudit } from "../types";

export function AgentAuditList({ audits, compact = false }: { audits: AgentAudit[]; compact?: boolean }) {
  if (!audits.length) {
    return <EmptyState title="No agent audits" detail="Agent backend calls will appear here after processing records them." />;
  }
  return (
    <ul className="audit-list">
      {audits.map((audit) => (
        <li key={audit.id}>
          <div className="audit-row-head">
            <Badge tone={audit.error ? "danger" : "info"}>{audit.request_type}</Badge>
            <strong>{audit.backend_provider}</strong>
            <span>{formatDate(audit.created_at)}</span>
          </div>
          <FieldList>
            <FactRow label="Session" value={audit.agent_session_id ?? "none"} />
            <FactRow label="Latency" value={audit.latency_ms === null ? "not recorded" : `${audit.latency_ms} ms`} />
            <FactRow label="Messages" value={audit.input_message_ids.join(", ") || "none"} />
            {!compact ? <FactRow label="Resources" value={audit.input_resource_ids.join(", ") || "none"} /> : null}
            {!compact ? <FactRow label="Tool permissions" value={audit.tool_permissions_profile ?? "not recorded"} /> : null}
          </FieldList>
          {audit.error ? (
            <div className="readonly-note">
              <Bot aria-hidden="true" size={14} />
              <span>{shortText(audit.error, "agent error")}</span>
            </div>
          ) : null}
          <details>
            <summary>Response summary</summary>
            <JsonBlock value={audit.response_summary} />
          </details>
          {!compact ? (
            <details>
              <summary>Response JSON</summary>
              <JsonBlock value={audit.response} />
            </details>
          ) : null}
          {Object.keys(audit.prompt_debug ?? {}).length ? (
            <details>
              <summary>Debug prompt</summary>
              <JsonBlock value={audit.prompt_debug} />
            </details>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function FactRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        {label === "Latency" ? <Clock aria-hidden="true" size={12} /> : null}
        {value}
      </dd>
    </div>
  );
}
