import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { HeartPulse, RefreshCw, Scissors, Send, ShieldCheck } from "lucide-react";
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
      setCommandResult(errorResult(sendTest ? "maintenance.doctor_send_test" : "maintenance.doctor", error))
  });
  const config = useMutation({
    mutationFn: () => validateConfig(token, { reason: cleanReason }),
    onSuccess: afterCommand,
    onError: (error) => setCommandResult(errorResult("maintenance.config_validate", error))
  });
  const retention = useMutation({
    mutationFn: (dryRun: boolean) => pruneRetention(token, { dry_run: dryRun, reason: cleanReason }),
    onSuccess: afterCommand,
    onError: (error) => setCommandResult(errorResult("maintenance.retention_prune", error))
  });
  const replyStyle = useMutation({
    mutationFn: (dryRun: boolean) => refreshReplyStyle(token, { dry_run: dryRun, reason: cleanReason }),
    onSuccess: afterCommand,
    onError: (error) => setCommandResult(errorResult("maintenance.reply_style_refresh", error))
  });

  return (
    <section className="work-grid" aria-label="Maintenance">
      <div className="work-main">
        <div className="queue-panel">
          <SectionHeader
            eyebrow="Maintenance"
            title="Operator commands"
            badge={<Badge tone="info">Explicit</Badge>}
          >
            <p className="section-note">Run diagnostics and maintenance commands through the same local command boundary as the CLI.</p>
          </SectionHeader>
        </div>

        <section className="queue-panel">
          <div className="subsection-title">
            <HeartPulse aria-hidden="true" size={16} />
            <h2>Diagnostics</h2>
          </div>
          <div className="command-buttons">
            <Button disabled={doctor.isPending} onClick={() => doctor.mutate(false)} tone="info">
              <HeartPulse aria-hidden="true" size={15} />
              Run doctor
            </Button>
            <Button
              disabled={doctor.isPending}
              onClick={() => confirmThen("This sends one test message to the configured owner.", () => doctor.mutate(true))}
              tone="warning"
            >
              <Send aria-hidden="true" size={15} />
              Send owner test
            </Button>
            <Button disabled={config.isPending} onClick={() => config.mutate()} tone="neutral">
              <ShieldCheck aria-hidden="true" size={15} />
              Validate config
            </Button>
          </div>
        </section>

        <section className="queue-panel">
          <div className="subsection-title">
            <Scissors aria-hidden="true" size={16} />
            <h2>Data Retention</h2>
          </div>
          <div className="command-buttons">
            <Button disabled={retention.isPending} onClick={() => retention.mutate(true)} tone="info">
              <Scissors aria-hidden="true" size={15} />
              Dry run
            </Button>
            <Button
              disabled={retention.isPending}
              onClick={() => confirmThen("This prunes expired local raw messages and resources.", () => retention.mutate(false))}
              tone="danger"
            >
              <Scissors aria-hidden="true" size={15} />
              Prune
            </Button>
          </div>
        </section>

        <section className="queue-panel">
          <div className="subsection-title">
            <RefreshCw aria-hidden="true" size={16} />
            <h2>Reply Style</h2>
          </div>
          <div className="command-buttons">
            <Button disabled={replyStyle.isPending} onClick={() => replyStyle.mutate(true)} tone="info">
              <RefreshCw aria-hidden="true" size={15} />
              Dry run
            </Button>
            <Button
              disabled={replyStyle.isPending}
              onClick={() => confirmThen("This refreshes the owner reply style profile.", () => replyStyle.mutate(false))}
              tone="warning"
            >
              <RefreshCw aria-hidden="true" size={15} />
              Refresh
            </Button>
          </div>
        </section>
      </div>

      <aside className="work-detail">
        <div className="detail-panel">
          <p className="eyebrow">Command Note</p>
          <h2>Maintenance reason</h2>
          <TextareaField label="Reason" onChange={setReason} placeholder="Optional maintenance note" rows={3} value={reason} />
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
