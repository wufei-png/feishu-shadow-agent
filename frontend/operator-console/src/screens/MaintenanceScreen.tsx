import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
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
  ConfirmButton,
  SectionHeader,
  TextareaField
} from "../components/Primitives";
import { failedCommandResult } from "../commandResult";
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
      setCommandResult(failedCommandResult(sendTest ? "maintenance.doctor_send_test" : "maintenance.doctor", error, t("common.requestFailed")))
  });
  const config = useMutation({
    mutationFn: () => validateConfig(token, { reason: cleanReason }),
    onSuccess: afterCommand,
    onError: (error) => setCommandResult(failedCommandResult("maintenance.config_validate", error, t("common.requestFailed")))
  });
  const retention = useMutation({
    mutationFn: (dryRun: boolean) => pruneRetention(token, { dry_run: dryRun, reason: cleanReason }),
    onSuccess: afterCommand,
    onError: (error) => setCommandResult(failedCommandResult("maintenance.retention_prune", error, t("common.requestFailed")))
  });
  const replyStyle = useMutation({
    mutationFn: (dryRun: boolean) => refreshReplyStyle(token, { dry_run: dryRun, reason: cleanReason }),
    onSuccess: afterCommand,
    onError: (error) => setCommandResult(failedCommandResult("maintenance.reply_style_refresh", error, t("common.requestFailed")))
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
            <ConfirmButton
              confirmLabel={t("maintenance.confirmSendTest")}
              disabled={doctor.isPending}
              onConfirm={() => doctor.mutate(true)}
              tone="warning"
            >
              <Send aria-hidden="true" size={15} />
              {t("maintenance.sendTest")}
            </ConfirmButton>
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
            <ConfirmButton
              confirmLabel={t("maintenance.confirmPrune")}
              disabled={retention.isPending}
              onConfirm={() => retention.mutate(false)}
              tone="danger"
            >
              <Scissors aria-hidden="true" size={15} />
              {t("maintenance.prune")}
            </ConfirmButton>
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
            <ConfirmButton
              confirmLabel={t("maintenance.confirmRefresh")}
              disabled={replyStyle.isPending}
              onConfirm={() => replyStyle.mutate(false)}
              tone="warning"
            >
              <RefreshCw aria-hidden="true" size={15} />
              {t("maintenance.refreshStyle")}
            </ConfirmButton>
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
