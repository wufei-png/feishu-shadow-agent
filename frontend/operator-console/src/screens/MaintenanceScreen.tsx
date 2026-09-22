import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { HeartPulse, RefreshCw, Scissors, Send, ShieldCheck } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  pruneRetention,
  refreshReplyStyle,
  runDoctor,
  validateConfig
} from "../api";
import {
  Badge,
  Button,
  CommandResultPanel,
  SectionHeader,
  TextareaField
} from "../components/Primitives";
import { invalidateAfterMaintenanceCommand } from "../queryKeys";
import type { CommandResult } from "../types";

export function MaintenanceScreen({ token }: { token: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const [commandResult, setCommandResult] = useState<CommandResult | null>(null);
  const cleanReason = reason.trim() || undefined;
  const afterCommand = async (result: CommandResult) => {
    setCommandResult(result);
    await invalidateAfterMaintenanceCommand(queryClient);
  };

  const doctor = useMutation({
    mutationFn: (sendTest: boolean) => runDoctor(token, { send_test: sendTest, reason: cleanReason }),
    onSuccess: afterCommand,
    onError: (error, sendTest) =>
      setCommandResult(errorResult(sendTest ? "maintenance.doctor_send_test" : "maintenance.doctor", error, t))
  });
  const config = useMutation({
    mutationFn: () => validateConfig(token, { reason: cleanReason }),
    onSuccess: afterCommand,
    onError: (error) => setCommandResult(errorResult("maintenance.config_validate", error, t))
  });
  const retention = useMutation({
    mutationFn: (dryRun: boolean) => pruneRetention(token, { dry_run: dryRun, reason: cleanReason }),
    onSuccess: afterCommand,
    onError: (error) => setCommandResult(errorResult("maintenance.retention_prune", error, t))
  });
  const replyStyle = useMutation({
    mutationFn: (dryRun: boolean) => refreshReplyStyle(token, { dry_run: dryRun, reason: cleanReason }),
    onSuccess: afterCommand,
    onError: (error) => setCommandResult(errorResult("maintenance.reply_style_refresh", error, t))
  });

  return (
    <section className="work-grid" aria-label={t("maintenance.aria")}>
      <div className="work-main">
        <div className="queue-panel">
          <SectionHeader
            eyebrow={t("maintenance.eyebrow")}
            title={t("maintenance.title")}
            badge={<Badge tone="info">{t("maintenance.explicit")}</Badge>}
          >
            <p className="section-note">{t("maintenance.boundary")}</p>
          </SectionHeader>
        </div>

        <section className="queue-panel">
          <div className="subsection-title">
            <HeartPulse aria-hidden="true" size={16} />
            <h2>{t("maintenance.diagnostics")}</h2>
          </div>
          <div className="command-buttons">
            <Button disabled={doctor.isPending} onClick={() => doctor.mutate(false)} tone="info">
              <HeartPulse aria-hidden="true" size={15} />
              {t("maintenance.runDoctor")}
            </Button>
            <Button
              disabled={doctor.isPending}
              onClick={() => confirmThen(t("maintenance.confirmSendTest"), () => doctor.mutate(true))}
              tone="warning"
            >
              <Send aria-hidden="true" size={15} />
              {t("maintenance.sendTest")}
            </Button>
            <Button disabled={config.isPending} onClick={() => config.mutate()} tone="neutral">
              <ShieldCheck aria-hidden="true" size={15} />
              {t("maintenance.validateConfig")}
            </Button>
          </div>
        </section>

        <section className="queue-panel">
          <div className="subsection-title">
            <Scissors aria-hidden="true" size={16} />
            <h2>{t("maintenance.retention")}</h2>
          </div>
          <div className="command-buttons">
            <Button disabled={retention.isPending} onClick={() => retention.mutate(true)} tone="info">
              <Scissors aria-hidden="true" size={15} />
              {t("maintenance.previewPrune")}
            </Button>
            <Button
              disabled={retention.isPending}
              onClick={() => confirmThen(t("maintenance.confirmPrune"), () => retention.mutate(false))}
              tone="danger"
            >
              <Scissors aria-hidden="true" size={15} />
              {t("maintenance.prune")}
            </Button>
          </div>
        </section>

        <section className="queue-panel">
          <div className="subsection-title">
            <RefreshCw aria-hidden="true" size={16} />
            <h2>{t("maintenance.replyStyle")}</h2>
          </div>
          <div className="command-buttons">
            <Button disabled={replyStyle.isPending} onClick={() => replyStyle.mutate(true)} tone="info">
              <RefreshCw aria-hidden="true" size={15} />
              {t("maintenance.previewRefresh")}
            </Button>
            <Button
              disabled={replyStyle.isPending}
              onClick={() => confirmThen(t("maintenance.confirmRefresh"), () => replyStyle.mutate(false))}
              tone="warning"
            >
              <RefreshCw aria-hidden="true" size={15} />
              {t("maintenance.refreshStyle")}
            </Button>
          </div>
        </section>
      </div>

      <aside className="work-detail">
        <div className="detail-panel">
          <p className="eyebrow">{t("maintenance.commandNote")}</p>
          <h2>{t("maintenance.reason")}</h2>
          <TextareaField label={t("common.reason")} onChange={setReason} placeholder={t("maintenance.reasonPlaceholder")} rows={3} value={reason} />
        </div>
        <CommandResultPanel result={commandResult} />
      </aside>
    </section>
  );
}

function confirmThen(message: string, action: () => void): void {
  if (window.confirm(message)) {
    action();
  }
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
