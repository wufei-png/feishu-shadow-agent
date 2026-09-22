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
    <section className="work-grid" aria-label="维护">
      <div className="work-main">
        <div className="queue-panel">
          <SectionHeader
            eyebrow="维护"
            title="值守操作"
            badge={<Badge tone="info">需主动执行</Badge>}
          >
            <p className="section-note">诊断和维护操作使用与 CLI 相同的本地命令边界。</p>
          </SectionHeader>
        </div>

        <section className="queue-panel">
          <div className="subsection-title">
            <HeartPulse aria-hidden="true" size={16} />
            <h2>诊断</h2>
          </div>
          <div className="command-buttons">
            <Button disabled={doctor.isPending} onClick={() => doctor.mutate(false)} tone="info">
              <HeartPulse aria-hidden="true" size={15} />
              运行 Doctor
            </Button>
            <Button
              disabled={doctor.isPending}
              onClick={() => confirmThen("将向已配置的 Owner 发送一条测试消息，确认继续？", () => doctor.mutate(true))}
              tone="warning"
            >
              <Send aria-hidden="true" size={15} />
              向 Owner 发送测试消息
            </Button>
            <Button disabled={config.isPending} onClick={() => config.mutate()} tone="neutral">
              <ShieldCheck aria-hidden="true" size={15} />
              验证配置
            </Button>
          </div>
        </section>

        <section className="queue-panel">
          <div className="subsection-title">
            <Scissors aria-hidden="true" size={16} />
            <h2>数据留存</h2>
          </div>
          <div className="command-buttons">
            <Button disabled={retention.isPending} onClick={() => retention.mutate(true)} tone="info">
              <Scissors aria-hidden="true" size={15} />
              预演清理
            </Button>
            <Button
              disabled={retention.isPending}
              onClick={() => confirmThen("将清理本地已过期的原始消息和资源，确认继续？", () => retention.mutate(false))}
              tone="danger"
            >
              <Scissors aria-hidden="true" size={15} />
              清理过期数据
            </Button>
          </div>
        </section>

        <section className="queue-panel">
          <div className="subsection-title">
            <RefreshCw aria-hidden="true" size={16} />
            <h2>回复风格</h2>
          </div>
          <div className="command-buttons">
            <Button disabled={replyStyle.isPending} onClick={() => replyStyle.mutate(true)} tone="info">
              <RefreshCw aria-hidden="true" size={15} />
              预演刷新
            </Button>
            <Button
              disabled={replyStyle.isPending}
              onClick={() => confirmThen("将刷新 Owner 回复风格档案，确认继续？", () => replyStyle.mutate(false))}
              tone="warning"
            >
              <RefreshCw aria-hidden="true" size={15} />
              刷新风格
            </Button>
          </div>
        </section>
      </div>

      <aside className="work-detail">
        <div className="detail-panel">
          <p className="eyebrow">命令备注</p>
          <h2>维护原因</h2>
          <TextareaField label="原因" onChange={setReason} placeholder="可选；记录本次维护的原因" rows={3} value={reason} />
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
