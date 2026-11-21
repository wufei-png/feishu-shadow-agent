import { useMutation } from "@tanstack/react-query";
import { useQuery } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { FileText, GitBranch, PackageSearch, RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { getMessageDetail, replayMessage, retryMessageProcessing } from "../api";
import {
  Fact,
  Badge,
  Button,
  CommandResultPanel,
  CopyValue,
  EmptyState,
  ErrorState,
  FieldList,
  formatDate,
  JsonBlock,
  LoadingState,
  statusTone,
  TechnicalDetails
} from "../components/Primitives";
import { queryKeys } from "../queryKeys";
import { enumLabel } from "../presentation";
import type { CommandResult } from "../types";
import { AgentAuditList } from "./AgentAuditList";

export function MessageDetailPanel({ token, messageId }: { token: string; messageId: string | null }) {
  const { t } = useTranslation();
  const detail = useQuery({
    queryKey: queryKeys.messageDetail(messageId),
    queryFn: () => getMessageDetail(token, messageId ?? ""),
    enabled: Boolean(token && messageId)
  });
  const replay = useMutation({
    mutationFn: (targetMessageId: string) => replayMessage(token, targetMessageId),
    onError: (error) => errorResult("message.replay_dry_run", error, t)
  });
  const retryProcessing = useMutation({
    mutationFn: ({ targetMessageId, stage }: { targetMessageId: string; stage: string }) =>
      retryMessageProcessing(token, targetMessageId, stage, {}),
    onSuccess: async () => {
      await detail.refetch();
    }
  });
  const replayResult =
    replay.variables === messageId
      ? (replay.data as CommandResult | undefined) ?? (replay.error ? errorResult("message.replay_dry_run", replay.error, t) : null)
      : null;
  const retryResult =
    retryProcessing.variables?.targetMessageId === messageId
      ? (retryProcessing.data as CommandResult | undefined) ??
        (retryProcessing.error ? errorResult("processing.retry", retryProcessing.error, t) : null)
      : null;

  if (!messageId) {
    return <EmptyState title={t("message.select")} detail={t("message.selectDetail")} />;
  }
  if (detail.isLoading) {
    return <LoadingState title={t("message.loading")} />;
  }
  if (detail.error) {
    return (
      <div className="detail-panel">
        <ErrorState title={t("message.error")} error={detail.error} />
        <Button onClick={() => void detail.refetch()}>{t("common.refresh")}</Button>
      </div>
    );
  }
  if (!detail.data) {
    return <EmptyState title={t("message.notFound")} detail={t("message.notFoundDetail")} />;
  }

  return (
    <div className="message-detail-stack">
      <div className="detail-panel">
        <div className="detail-title-row">
          <h2>{t("message.body")}</h2>
          <Badge tone={statusTone(detail.data.message.sender_role)}>{enumLabel(t, "role", detail.data.message.sender_role)}</Badge>
        </div>
        <p className="preview-copy">{detail.data.message.text?.trim() || t("message.noBody")}</p>
        <p className="detail-note">{formatDate(detail.data.message.sent_at)}</p>
        <TechnicalDetails>
          <FieldList>
            <Fact label={t("message.messageId")} value={<CopyValue label={t("message.messageId")} value={detail.data.message.message_id} />} />
            <Fact label={t("message.chat")} value={detail.data.message.chat_id ?? t("common.notRecorded")} />
            <Fact label={t("message.thread")} value={detail.data.message.thread_id ?? t("common.none")} />
            <Fact label={t("message.replyTo")} value={detail.data.message.reply_to_message_id ?? t("common.none")} />
          </FieldList>
        </TechnicalDetails>
      </div>

      <div className="detail-panel">
        <div className="subsection-title">
          <PackageSearch aria-hidden="true" size={16} />
          <h2>{t("message.processing")}</h2>
        </div>
        {detail.data.processing.length ? (
          <ul className="timeline-list">
            {detail.data.processing.map((item) => {
              const heldRetry = item.latest_retry?.status === "queued" && item.latest_retry.policy_held;
              const activeRetry = !heldRetry && (item.latest_retry?.status === "queued" || item.latest_retry?.status === "claimed");
              return (
                <li key={item.id}>
                  <PackageSearch aria-hidden="true" size={14} />
                  <span>{enumLabel(t, "stage", item.stage)}</span>
                  <small title={`${item.status}${item.terminal_reason ? ` · ${item.terminal_reason}` : ""}`}>{`${enumLabel(t, "status", item.status)}${item.terminal_reason ? ` · ${item.terminal_reason}` : ""}`}</small>
                  {["processing_failed_terminal", "blocked_waiting_external"].includes(item.status) ? (
                    <Button
                      disabled={retryProcessing.isPending || activeRetry}
                      onClick={() => retryProcessing.mutate({ targetMessageId: messageId, stage: item.stage })}
                      tone="warning"
                    >
                      {heldRetry ? t("message.resumeRetry") : activeRetry ? t("message.queued") : t("message.retry")}
                    </Button>
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="detail-note">{t("message.noProcessing")}</p>
        )}
        <CommandResultPanel result={retryResult} />
      </div>

      <TechnicalDetails summary={t("message.routing")}>
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
      </TechnicalDetails>

      <TechnicalDetails summary={t("message.replay")}>
        <Button disabled={replay.isPending} onClick={() => replay.mutate(messageId)} tone="info">
          <RotateCcw aria-hidden="true" size={15} />
          {t("message.previewReplay")}
        </Button>
        <CommandResultPanel result={replayResult} />
      </TechnicalDetails>

      <TechnicalDetails summary={t("message.resources")}>
        {detail.data.resources.length ? (
          <ul className="audit-list">
            {detail.data.resources.map((resource) => (
              <li key={resource.id}>
                <div className="audit-row-head">
                  <Badge tone={statusTone(resource.download_status)}>{enumLabel(t, "status", resource.download_status)}</Badge>
                  <strong>{resource.resource_type}</strong>
                  <span>{resource.file_key}</span>
                </div>
                <FieldList>
                  <Fact label={t("message.path")} value={resource.path ?? t("common.notRecorded")} />
                  <Fact label={t("message.pathExists")} value={resource.path_exists === null ? t("common.notChecked") : resource.path_exists ? t("common.yes") : t("common.no")} />
                  <Fact label="SHA-256" value={resource.sha256_short ?? t("common.notRecorded")} />
                </FieldList>
                {Object.keys(resource.raw_summary).length ? (
                  <details>
                    <summary>{t("common.rawSummary")}</summary>
                    <JsonBlock value={resource.raw_summary} />
                  </details>
                ) : null}
                <details>
                  <summary>{t("common.rawJson")}</summary>
                  <JsonBlock value={resource.raw} />
                </details>
              </li>
            ))}
          </ul>
        ) : (
          <p className="detail-note">{t("message.noResources")}</p>
        )}
      </TechnicalDetails>

      <TechnicalDetails summary={t("message.agentAudit")}>
        <AgentAuditList audits={detail.data.agent_audits} compact />
      </TechnicalDetails>

      <TechnicalDetails summary={t("message.approvals")}>
        {detail.data.approvals.length ? (
          <ul className="timeline-list">
            {detail.data.approvals.map((approval) => (
              <li key={approval.approval_id}>
                <FileText aria-hidden="true" size={14} />
                <a href={`#approvals/${encodeURIComponent(approval.approval_id)}`}>{approval.approval_id}</a>
                <small title={approval.status}>{enumLabel(t, "status", approval.status)}</small>
              </li>
            ))}
          </ul>
        ) : (
          <p className="detail-note">{t("message.noApprovals")}</p>
        )}
      </TechnicalDetails>

      <TechnicalDetails summary={t("message.dispatchOutcomes")}>
        {detail.data.recorded_dispatch_outcomes.length ? (
          <JsonBlock value={detail.data.recorded_dispatch_outcomes} />
        ) : (
          <p className="detail-note">{t("message.noDispatchOutcomes")}</p>
        )}
      </TechnicalDetails>
    </div>
  );
}


function errorResult(command: string, error: unknown, t: TFunction): CommandResult {
  return {
    status: "failed",
    command,
    actor: "local_console",
    reason: null,
    target: {},
    changed: false,
    result: { error: error instanceof Error ? error.message : t("common.requestFailed") },
    warnings: [],
    next_actions: []
  };
}
