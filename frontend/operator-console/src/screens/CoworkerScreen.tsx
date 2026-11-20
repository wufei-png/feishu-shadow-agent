import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { coworkerRecovery, getCoworker, updateCoworkerPolicy } from "../api";
import {
  Badge,
  Button,
  ConfirmButton,
  CopyValue,
  EmptyState,
  ErrorState,
  Fact,
  FieldList,
  LoadingState,
  TechnicalDetails,
} from "../components/Primitives";
import { formatDateTime } from "../i18n";
import { enumLabel } from "../presentation";
import type { CoworkerPolicy } from "../types";

export function CoworkerScreen({ token }: { token: string }) {
  const { t } = useTranslation();
  return (
    <section className="work-main" aria-label={t("coworker.title")}>
      <div className="queue-panel">
        <h1>{t("coworker.title")}</h1>
        <p className="section-note">{t("coworker.hermesProfile")}</p>
        <div className="coworker-hermes-entry">
          <a
            className="button"
            href="http://127.0.0.1:9119/sessions?profile=coworker"
            target="_blank"
            rel="noopener noreferrer"
          >
            {t("coworker.openHermes")}
          </a>
          <TechnicalDetails summary={t("coworker.startHermesDashboard")}>
            <CopyValue
              value="hermes -p coworker dashboard --host 127.0.0.1 --port 9119 --no-open"
              label={t("coworker.startHermesDashboard")}
            />
          </TechnicalDetails>
        </div>
      </div>
      <ProjectCoworkerScreen token={token} />
    </section>
  );
}

function ProjectCoworkerScreen({ token }: { token: string }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const snapshot = useQuery({
    queryKey: ["coworker"],
    queryFn: () => getCoworker(token),
    enabled: Boolean(token),
    refetchInterval: 15_000,
  });
  const [trigger, setTrigger] =
    useState<CoworkerPolicy["trigger_mode"]>("mention");
  const [history, setHistory] =
    useState<CoworkerPolicy["history_source"]>("live_on_demand");
  const [memory, setMemory] = useState(true);
  const [memoryTime, setMemoryTime] = useState("00:00");
  const [enabled, setEnabled] = useState(false);
  const [chatId, setChatId] = useState("");
  const [provider, setProvider] = useState("codex");
  const [sendMode, setSendMode] = useState("agent_direct");
  const [confirmedStopped, setConfirmedStopped] = useState(false);
  const policy = snapshot.data?.policy;
  const policyTrigger = policy?.trigger_mode;
  const policyHistory = policy?.history_source;
  const policyMemory = policy?.memory_maintenance;
  const policyTime = policy?.memory_maintenance_time;
  const policyEnabled = policy?.enabled;
  const policyChatId = policy?.chat_id;
  const policyProvider = policy?.provider;
  const policySendMode = policy?.send_mode;
  useEffect(() => {
    if (
      policyTrigger === undefined ||
      policyHistory === undefined ||
      policyMemory === undefined
    )
      return;
    setTrigger(policyTrigger);
    setHistory(policyHistory);
    setMemory(policyMemory);
    setMemoryTime(policyTime ?? "00:00");
    setEnabled(policyEnabled ?? false);
    setChatId(policyChatId ?? "");
    setProvider(policyProvider ?? "codex");
    setSendMode(policySendMode ?? "agent_direct");
  }, [
    policyTrigger,
    policyHistory,
    policyMemory,
    policyTime,
    policyEnabled,
    policyChatId,
    policyProvider,
    policySendMode,
  ]);

  const refresh = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: ["coworker"] }),
      client.invalidateQueries({ queryKey: ["dashboard"] }),
      client.invalidateQueries({ queryKey: ["policy-status"] }),
    ]);
  };
  const changes: Partial<
    Pick<
      CoworkerPolicy,
      | "enabled"
      | "chat_id"
      | "provider"
      | "send_mode"
      | "trigger_mode"
      | "history_source"
      | "memory_maintenance"
      | "memory_maintenance_time"
    >
  > = {};
  if (policy && enabled !== policy.enabled) changes.enabled = enabled;
  if (policy && chatId !== (policy.chat_id ?? "")) changes.chat_id = chatId;
  if (policy && provider !== policy.provider) changes.provider = provider;
  if (policy && sendMode !== policy.send_mode) changes.send_mode = sendMode;
  if (policy && trigger !== policy.trigger_mode) changes.trigger_mode = trigger;
  if (policy && history !== policy.history_source)
    changes.history_source = history;
  if (policy && memory !== policy.memory_maintenance)
    changes.memory_maintenance = memory;
  if (policy && memoryTime !== (policy.memory_maintenance_time ?? "00:00"))
    changes.memory_maintenance_time = memoryTime;
  const save = useMutation({
    mutationFn: () =>
      updateCoworkerPolicy(token, {
        ...changes,
        confirm_stopped: confirmedStopped,
      }),
    onSuccess: refresh,
  });
  const recover = useMutation({
    mutationFn: (path: string) =>
      coworkerRecovery(token, path, confirmedStopped),
    onSuccess: refresh,
  });
  if (snapshot.isLoading) return <LoadingState />;
  if (snapshot.error && !snapshot.data)
    return (
      <ErrorState title={t("coworker.unavailable")} error={snapshot.error} />
    );
  const data = snapshot.data;
  if (!data?.available)
    return (
      <EmptyState
        title={t("coworker.unavailable")}
        detail={t("common.notRecorded")}
      />
    );
  const canRecover =
    ["stopped", "not_started", "startup_failed"].includes(
      data.liveness.status,
    ) ||
    (data.liveness.status === "stale" && confirmedStopped);
  const changed = data.memory_maintenance?.memory_file_changed;
  const shown = (value: unknown) =>
    value === null || value === undefined || value === ""
      ? t("common.notRecorded")
      : String(value);
  const date = (value?: string) =>
    value ? formatDateTime(value) : t("common.notRecorded");
  const runtime = data.runtime;
  const publication = {
    project: t("coworker.projectSend"),
    agent_direct: t("coworker.agentSend"),
  };
  let runtimeLabel = t("coworker.runtimeUnreported");
  if (runtime?.business_mode === "shadow") runtimeLabel = t("coworker.shadowMode");
  if (runtime?.coworker_runtime === "legacy") runtimeLabel = t("coworker.legacyMode");
  if (runtime?.coworker_runtime === "native") runtimeLabel = t("coworker.codexDirect");
  return (
    <div className="work-grid">
      <div className="work-main">
        <div className="queue-panel">
          <div className="section-header">
            <h2>{t("coworker.runtime")}</h2>
            <Badge tone={data.liveness.status === "live" ? "success" : "muted"}>
              {enumLabel(t, "status", data.liveness.status)}
            </Badge>
          </div>
          {runtime ? (
            <dl className="compact-facts">
              <Fact label={t("coworker.mode")} value={runtimeLabel} />
              <Fact
                label={t("coworker.feishuReceiver")}
                value={t(
                  runtime.business_mode === "shadow"
                    ? "coworker.projectPolling"
                    : "coworker.projectConnection",
                )}
              />
              {runtime.business_mode === "coworker" ? (
                <>
                  <Fact
                    label={t("coworker.group")}
                    value={shown(runtime.chat_id)}
                  />
                  {runtime.coworker_runtime !== "native" ? (
                    <Fact
                      label={t("coworker.provider")}
                      value={
                        runtime.provider === "hermes"
                          ? t("coworker.legacyHermes")
                          : shown(runtime.provider)
                      }
                    />
                  ) : null}
                  <Fact
                    label={t("coworker.send")}
                    value={
                      runtime.send_mode === "project" || runtime.send_mode === "agent_direct"
                        ? publication[runtime.send_mode]
                        : shown(runtime.send_mode)
                    }
                  />
                  {runtime.reply_mode ? (
                    <Fact
                      label={t("coworker.replyMode")}
                      value={t(`coworker.replyMode.${runtime.reply_mode}`)}
                    />
                  ) : null}
                  {runtime.provider === "codex" && data.backend_health ? (
                    <Fact
                      label={t("coworker.backendHealth")}
                      value={`${data.backend_health.status} · ${data.backend_health.message}`}
                    />
                  ) : null}
                </>
              ) : null}
            </dl>
          ) : (
            <p className="detail-note">{t("coworker.runtimeUnknown")}</p>
          )}
          {data.liveness.status === "stale" ? (
            <label>
              <input
                type="checkbox"
                checked={confirmedStopped}
                onChange={(event) => setConfirmedStopped(event.target.checked)}
              />{" "}
              {t("coworker.confirmStopped")}
            </label>
          ) : null}
        </div>
        <div className="queue-panel">
          <h2>{t("coworker.events")}</h2>
          <p className="detail-note">{t("coworker.listLimit")}</p>
          <p className="detail-note">{t("coworker.recoveryNote")}</p>
          {data.events.length ? (
            <ul className="timeline-list">
              {data.events.map((event) => (
                <li key={event.id}>
                  <span>
                    {event.topic_key} · {event.message_id} · {event.status}{" "}
                    {event.error ?? ""}
                  </span>
                  {["processing", "needs_review"].includes(event.status) ||
                  (event.status === "queued" &&
                    event.message_id.startsWith("internal:subagent:")) ? (
                    <ConfirmButton
                      disabled={!canRecover || recover.isPending}
                      confirmLabel={t("coworker.confirmDiscard")}
                      onConfirm={() =>
                        recover.mutate(`events/${event.id}/discard`)
                      }
                    >
                      {t("coworker.discard")}
                    </ConfirmButton>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <p>{t("common.none")}</p>
          )}
          <h2>{t("coworker.sessions")}</h2>
          {data.sessions.length ? (
            <ul className="timeline-list">
              {data.sessions.map((session) => (
                <li key={session.id}>
                  <span>
                    {session.thread_id} · {session.provider} · {session.state}
                  </span>
                  {session.state === "active" ? (
                    <ConfirmButton
                      disabled={!canRecover || recover.isPending}
                      confirmLabel={t("coworker.confirmSession")}
                      onConfirm={() =>
                        recover.mutate(`sessions/${session.id}/reset`)
                      }
                    >
                      {t("coworker.resetSession")}
                    </ConfirmButton>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <p>{t("common.none")}</p>
          )}
          {recover.error ? (
            <ErrorState
              title={t("common.requestFailed")}
              error={recover.error}
            />
          ) : null}
        </div>
        <details className="queue-panel coworker-settings">
          <summary>{t("coworker.settings")}</summary>
          <div className="policy-form-grid">
            {[
              { name: t("coworker.source"), value: data.config },
              { name: t("coworker.policy"), value: data.policy },
            ].map(({ name, value }) => (
              <div className="detail-panel" key={name}>
                <h2>{name}</h2>
                <FieldList>
                  <Fact
                    label={t("coworker.mode")}
                    value={
                      value
                        ? value.enabled
                          ? "coworker"
                          : "shadow"
                        : t("common.notRecorded")
                    }
                  />
                  <Fact
                    label={t("coworker.group")}
                    value={shown(value?.chat_id)}
                  />
                  <Fact
                    label={t("coworker.provider")}
                    value={
                      value?.provider === "hermes"
                        ? t("coworker.legacyHermes")
                        : shown(value?.provider)
                    }
                  />
                  <Fact
                    label={t("coworker.legacySend")}
                    value={shown(value?.send_mode)}
                  />
                  <Fact
                    label={t("coworker.trigger")}
                    value={shown(value?.trigger_mode)}
                  />
                  <Fact
                    label={t("coworker.history")}
                    value={shown(value?.history_source)}
                  />
                  <Fact
                    label={t("coworker.enabled")}
                    value={
                      value
                        ? value.memory_maintenance
                          ? t("common.yes")
                          : t("common.no")
                        : t("common.notRecorded")
                    }
                  />
                  <Fact
                    label={t("coworker.memoryTime")}
                    value={
                      value
                        ? (value.memory_maintenance_time ?? "00:00")
                        : t("common.notRecorded")
                    }
                  />
                </FieldList>
              </div>
            ))}
          </div>
          <h2>{t("coworker.edit")}</h2>
          <p className="section-note">{t("coworker.editDetail")}</p>
          {policy ? (
            <div className="policy-form-grid">
              <label>
                <input
                  type="checkbox"
                  checked={enabled}
                  disabled={!canRecover}
                  onChange={(event) => setEnabled(event.target.checked)}
                />{" "}
                {t("coworker.enableMode")}
              </label>
              <label className="field-control">
                <span>{t("coworker.group")}</span>
                <input
                  value={chatId}
                  disabled={!canRecover}
                  onChange={(event) => setChatId(event.target.value)}
                />
              </label>
              <label className="field-control">
                <span>{t("coworker.provider")}</span>
                <select
                  value={provider}
                  disabled={!canRecover}
                  onChange={(event) => setProvider(event.target.value)}
                >
                  <option value="codex">Codex</option>
                  <option value="hermes">
                    {t("coworker.legacyHermes")}
                  </option>
                </select>
              </label>
              <label className="field-control">
                <span>{t("coworker.legacySend")}</span>
                <select
                  value={sendMode}
                  disabled={!canRecover}
                  onChange={(event) => setSendMode(event.target.value)}
                >
                  <option value="agent_direct">agent_direct</option>
                  <option value="project">project</option>
                </select>
              </label>
              <label className="field-control">
                <span>{t("coworker.trigger")}</span>
                <select
                  value={trigger}
                  onChange={(event) =>
                    setTrigger(
                      event.target.value as CoworkerPolicy["trigger_mode"],
                    )
                  }
                >
                  <option value="mention">{t("coworker.mention")}</option>
                  <option value="followups">{t("coworker.followups")}</option>
                </select>
              </label>
              <label className="field-control">
                <span>{t("coworker.history")}</span>
                <select
                  value={history}
                  onChange={(event) =>
                    setHistory(
                      event.target.value as CoworkerPolicy["history_source"],
                    )
                  }
                >
                  <option value="live_on_demand">{t("coworker.live")}</option>
                  <option value="local_cache">{t("coworker.cache")}</option>
                </select>
              </label>
              <label className="field-control">
                <span>{t("coworker.memoryTime")}</span>
                <input
                  type="time"
                  value={memoryTime}
                  onChange={(event) => setMemoryTime(event.target.value)}
                />
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={memory}
                  onChange={(event) => setMemory(event.target.checked)}
                />{" "}
                {t("coworker.enabled")}
              </label>
              <Button
                disabled={
                  save.isPending || !memoryTime || !Object.keys(changes).length
                }
                onClick={() => save.mutate()}
                tone="success"
              >
                {t("coworker.save")}
              </Button>
            </div>
          ) : (
            <p className="detail-note">
              {t(
                data.policy_invalid
                  ? "coworker.policyInvalid"
                  : "coworker.policyNotImported",
              )}
            </p>
          )}
          {save.isSuccess ? <p role="status">{t("coworker.saved")}</p> : null}
          {save.error ? (
            <ErrorState title={t("common.requestFailed")} error={save.error} />
          ) : null}
        </details>
      </div>
      <aside className="work-detail">
        <div className="detail-panel">
          <h2>{t("coworker.memory")}</h2>
          <FieldList>
            <Fact
              label={t("common.status")}
              value={shown(data.memory_maintenance?.state)}
            />
            <Fact
              label={t("coworker.lastAttempt")}
              value={date(data.memory_maintenance?.last_attempt)}
            />
            <Fact
              label={t("coworker.lastSuccess")}
              value={date(data.memory_maintenance?.last_success_at)}
            />
            <Fact
              label={t("coworker.fileChanged")}
              value={
                changed === null || changed === undefined
                  ? t("common.notRecorded")
                  : changed
                    ? t("common.yes")
                    : t("common.no")
              }
            />
          </FieldList>
          <p className="detail-note">{t("coworker.fileChangedDetail")}</p>
          {["failed", "needs_review", "in_progress"].includes(
            data.memory_maintenance?.state ?? "",
          ) ? (
            <ConfirmButton
              disabled={!canRecover || recover.isPending}
              confirmLabel={t("coworker.confirmMemory")}
              onConfirm={() => recover.mutate("memory/reset")}
            >
              {t("coworker.resetMemory")}
            </ConfirmButton>
          ) : null}
        </div>
        <div className="detail-panel">
          <h2>{t("coworker.children")}</h2>
          {data.subagents.length ? (
            <ul className="timeline-list">
              {data.subagents.map((child) => (
                <li key={child.child_thread_id}>
                  {child.child_thread_id} · {child.status} · {child.outcome}
                </li>
              ))}
            </ul>
          ) : (
            <p>{t("common.none")}</p>
          )}
        </div>
        <div className="detail-panel">
          <h2>{t("coworker.gaps")}</h2>
          {data.history_gaps.length ? (
            <ul className="timeline-list">
              {data.history_gaps.map((gap) => (
                <li key={gap.topic_key}>
                  {gap.topic_key} ·{" "}
                  {gap.last_error ??
                    (gap.has_more ? "has_more" : "needs_backfill")}
                </li>
              ))}
            </ul>
          ) : (
            <p>{t("common.none")}</p>
          )}
        </div>
        <div className="detail-panel">
          <h2>{t("coworker.delegations")}</h2>
          <p className="detail-note">{t("coworker.directNote")}</p>
          {data.delegations.length ? (
            <ul className="timeline-list">
              {data.delegations.map((item) => (
                <li key={item.id}>
                  {item.peer_id} · {item.status}
                </li>
              ))}
            </ul>
          ) : (
            <p>{t("common.none")}</p>
          )}
        </div>
      </aside>
    </div>
  );
}
