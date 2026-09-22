import { Bot, Clock } from "lucide-react";
import { useTranslation } from "react-i18next";
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
  const { t } = useTranslation();
  if (!audits.length) {
    return <EmptyState title={t("audit.empty")} detail={t("audit.emptyDetail")} />;
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
            <FactRow label={t("audit.session")} value={audit.agent_session_id ?? t("common.none")} />
            <FactRow icon="clock" label={t("audit.latency")} value={audit.latency_ms === null ? t("common.notRecorded") : `${audit.latency_ms} ms`} />
            <FactRow label={t("audit.messages")} value={audit.input_message_ids.join(", ") || t("common.none")} />
            {!compact ? <FactRow label={t("audit.resources")} value={audit.input_resource_ids.join(", ") || t("common.none")} /> : null}
            {!compact ? <FactRow label={t("audit.toolPermissions")} value={audit.tool_permissions_profile ?? t("common.notRecorded")} /> : null}
          </FieldList>
          {audit.error ? (
            <div className="readonly-note">
              <Bot aria-hidden="true" size={14} />
              <span>{shortText(audit.error, t("audit.agentError"))}</span>
            </div>
          ) : null}
          <details>
            <summary>{t("audit.responseSummary")}</summary>
            <JsonBlock value={audit.response_summary} />
          </details>
          {!compact ? (
            <details>
              <summary>{t("audit.responseJson")}</summary>
              <JsonBlock value={audit.response} />
            </details>
          ) : null}
          {Object.keys(audit.prompt_debug ?? {}).length ? (
            <details>
              <summary>{t("audit.debugPrompt")}</summary>
              <JsonBlock value={audit.prompt_debug} />
            </details>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function FactRow({ label, value, icon }: { label: string; value: string; icon?: "clock" }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        {icon === "clock" ? <Clock aria-hidden="true" size={12} /> : null}
        {value}
      </dd>
    </div>
  );
}
