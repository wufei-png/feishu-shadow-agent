import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { HeartPulse, RefreshCw, Scissors, Send, ShieldCheck } from "lucide-react";
import { useTranslation } from "react-i18next";
import { getDoctorTarget, pruneRetention, refreshReplyStyle, runDoctor, validateConfig } from "../api";
import {
  Button,
  CommandResultPanel,
  ConfirmButton,
  CopyValue,
  ErrorState,
  Fact,
  FieldList,
  LoadingState,
  TechnicalDetails,
  TextareaField
} from "../components/Primitives";
import { failedCommandResult } from "../commandResult";
import { invalidateAfterMaintenanceCommand } from "../queryKeys";
import type { CommandResult } from "../types";

type MaintenanceGroup = "diagnostics" | "retention" | "replyStyle";

export function MaintenanceScreen({ token }: { token: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [reasons, setReasons] = useState<Record<MaintenanceGroup, string>>({ diagnostics: "", retention: "", replyStyle: "" });
  const target = useQuery({
    queryKey: ["maintenance-doctor-target"],
    queryFn: () => getDoctorTarget(token),
    enabled: Boolean(token)
  });
  const afterCommand = () => invalidateAfterMaintenanceCommand(queryClient);
  const reason = (group: MaintenanceGroup) => reasons[group].trim() || undefined;
  const errorResult = (command: string, error: unknown) =>
    error ? failedCommandResult(command, error, t("common.requestFailed")) : null;

  const doctor = useMutation({
    mutationFn: (sendTest: boolean) => runDoctor(token, { send_test: sendTest, reason: reason("diagnostics") }),
    onSuccess: afterCommand
  });
  const config = useMutation({
    mutationFn: () => validateConfig(token, { reason: reason("diagnostics") }),
    onSuccess: afterCommand
  });
  const retention = useMutation({
    mutationFn: (dryRun: boolean) => pruneRetention(token, { dry_run: dryRun, reason: reason("retention") }),
    onSuccess: afterCommand
  });
  const replyStyle = useMutation({
    mutationFn: (dryRun: boolean) => refreshReplyStyle(token, { dry_run: dryRun, reason: reason("replyStyle") }),
    onSuccess: afterCommand
  });
  const doctorResult = doctor.data ?? errorResult(doctor.variables ? "maintenance.doctor_send_test" : "maintenance.doctor", doctor.error);
  const retentionResult = retention.data ?? errorResult("maintenance.retention_prune", retention.error);
  const replyStyleResult = replyStyle.data ?? errorResult("maintenance.reply_style_refresh", replyStyle.error);
  const recipient = target.data;
  const recipientLabel = recipient?.name ? `${recipient.name} · ${recipient.open_id}` : recipient?.open_id;

  function reasonField(group: MaintenanceGroup) {
    return (
      <TechnicalDetails summary={t("maintenance.reason")}>
        <TextareaField
          label={t("common.reason")}
          onChange={(value) => setReasons((current) => ({ ...current, [group]: value }))}
          placeholder={t("maintenance.reasonPlaceholder")}
          rows={2}
          value={reasons[group]}
        />
      </TechnicalDetails>
    );
  }

  return (
    <section className="work-main" aria-label={t("maintenance.aria")}>
      <header className="workbench-header"><h1>{t("maintenance.title")}</h1></header>
      <section className="queue-panel" aria-labelledby="maintenance-diagnostics">
        <h2 id="maintenance-diagnostics">{t("maintenance.diagnostics")}</h2>
        {reasonField("diagnostics")}
        <div className="command-buttons">
          <Button disabled={doctor.isPending} onClick={() => doctor.mutate(false)} tone="info">
            <HeartPulse aria-hidden="true" size={15} />
            {t("maintenance.runDoctor")}
          </Button>
          <Button disabled={config.isPending} onClick={() => config.mutate()}>
            <ShieldCheck aria-hidden="true" size={15} />
            {t("maintenance.validateConfig")}
          </Button>
        </div>
        <CommandResultPanel result={doctor.variables ? null : doctorResult} />
        <CommandResultPanel result={config.data ?? errorResult("maintenance.config_validate", config.error)} />
        <TechnicalDetails summary={t("maintenance.testMessage")}>
          {target.isLoading ? <LoadingState title={t("maintenance.loadingRecipient")} /> : null}
          {target.error && !recipient ? (
            <div>
              <ErrorState title={t("maintenance.recipientUnavailable")} error={target.error} />
              <Button onClick={() => void target.refetch()}>{t("common.refresh")}</Button>
            </div>
          ) : null}
          {recipient && recipientLabel ? (
            <>
              <FieldList>
                <Fact label={t("maintenance.recipient")} value={<CopyValue label={t("maintenance.recipient")} value={recipientLabel} />} />
              </FieldList>
              {recipient.can_send_test ? (
                <ConfirmButton
                  confirmLabel={t("maintenance.confirmRecipient", { recipient: recipientLabel })}
                  disabled={doctor.isPending}
                  onConfirm={() => doctor.mutate(true)}
                  tone="warning"
                >
                  <Send aria-hidden="true" size={15} />
                  {t("maintenance.sendTest")}
                </ConfirmButton>
              ) : <p className="detail-note">{t("maintenance.sendTestDryRun")}</p>}
            </>
          ) : null}
          <CommandResultPanel result={doctor.variables ? doctorResult : null} />
        </TechnicalDetails>
      </section>

      <section className="queue-panel" aria-labelledby="maintenance-retention">
        <h2 id="maintenance-retention">{t("maintenance.retention")}</h2>
        {reasonField("retention")}
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
            {t("maintenance.prune")}
          </ConfirmButton>
        </div>
        <CommandResultPanel result={retentionResult} summary={retentionResult ? <RetentionSummary result={retentionResult} /> : null} />
      </section>

      <section className="queue-panel" aria-labelledby="maintenance-style">
        <h2 id="maintenance-style">{t("maintenance.replyStyle")}</h2>
        {reasonField("replyStyle")}
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
            {t("maintenance.refreshStyle")}
          </ConfirmButton>
        </div>
        <CommandResultPanel
          result={replyStyleResult}
          summary={typeof replyStyleResult?.result.selected_count === "number" ? (
            <p className="detail-note">{t("maintenance.styleSamples", { count: replyStyleResult.result.selected_count })}</p>
          ) : null}
        />
      </section>
    </section>
  );
}

function RetentionSummary({ result }: { result: CommandResult }) {
  const { t } = useTranslation();
  const preview = result.result.dry_run;
  if (typeof preview !== "boolean") return null;
  const resources = result.result[preview ? "resources_candidates" : "resources_deleted"];
  const feedback = result.result[preview ? "feedback_content_candidates" : "feedback_content_expired"];
  return (
    <FieldList>
      {typeof resources === "number" ? <Fact label={t(preview ? "maintenance.resourceCandidates" : "maintenance.resourcesDeleted")} value={resources} /> : null}
      {typeof feedback === "number" ? <Fact label={t(preview ? "maintenance.feedbackCandidates" : "maintenance.feedbackExpired")} value={feedback} /> : null}
    </FieldList>
  );
}
