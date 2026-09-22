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
    return <EmptyState title="没有 Agent 审计记录" detail="Agent 后端调用被记录后会显示在这里。" />;
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
            <FactRow label="会话" value={audit.agent_session_id ?? "无"} />
            <FactRow label="耗时" value={audit.latency_ms === null ? "未记录" : `${audit.latency_ms} ms`} />
            <FactRow label="消息" value={audit.input_message_ids.join(", ") || "无"} />
            {!compact ? <FactRow label="资源" value={audit.input_resource_ids.join(", ") || "无"} /> : null}
            {!compact ? <FactRow label="工具权限" value={audit.tool_permissions_profile ?? "未记录"} /> : null}
          </FieldList>
          {audit.error ? (
            <div className="readonly-note">
              <Bot aria-hidden="true" size={14} />
              <span>{shortText(audit.error, "Agent 错误")}</span>
            </div>
          ) : null}
          <details>
            <summary>响应摘要</summary>
            <JsonBlock value={audit.response_summary} />
          </details>
          {!compact ? (
            <details>
              <summary>响应 JSON</summary>
              <JsonBlock value={audit.response} />
            </details>
          ) : null}
          {Object.keys(audit.prompt_debug ?? {}).length ? (
            <details>
              <summary>调试提示词</summary>
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
        {label === "耗时" ? <Clock aria-hidden="true" size={12} /> : null}
        {value}
      </dd>
    </div>
  );
}
