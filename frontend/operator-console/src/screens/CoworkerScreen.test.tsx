// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import * as api from "../api";
import { setConsoleLanguage } from "../i18n";
import type { CoworkerSnapshot } from "../types";
import { CoworkerScreen } from "./CoworkerScreen";

vi.mock("../api", () => ({
  getCoworker: vi.fn(),
  updateCoworkerPolicy: vi.fn(),
  coworkerRecovery: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("shows separate sources and saves the daily time to the policy", async () => {
  await setConsoleLanguage("zh-CN");
  vi.mocked(api.getCoworker).mockResolvedValue(snapshot());
  vi.mocked(api.updateCoworkerPolicy).mockResolvedValue({ changed: true });
  const user = userEvent.setup();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <CoworkerScreen token="token" />
    </QueryClientProvider>,
  );
  expect(await screen.findByRole("heading", { name: "配置文件" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "SQLite 生效策略" })).toBeTruthy();
  expect(screen.getByText("未运行或无法确认")).toBeTruthy();
  expect(
    screen.getByText("模型直发的消息和委派不一定有项目侧记录。"),
  ).toBeTruthy();
  const time = screen.getByLabelText("每天整理时间（运行机器本地时间）");
  await user.clear(time);
  await user.type(time, "01:30");
  await user.click(screen.getByRole("button", { name: "保存策略" }));
  await waitFor(() =>
    expect(api.updateCoworkerPolicy).toHaveBeenCalledWith(
      "token",
      expect.objectContaining({ memory_maintenance_time: "01:30" }),
    ),
  );
});

it("disables recovery while the daemon is running", async () => {
  await setConsoleLanguage("zh-CN");
  const data = snapshot();
  data.liveness.status = "live";
  data.events = [
    {
      id: 7,
      topic_key: "topic",
      message_id: "message",
      status: "needs_review",
      error: null,
    },
  ];
  vi.mocked(api.getCoworker).mockResolvedValue(data);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <CoworkerScreen token="token" />
    </QueryClientProvider>,
  );
  expect(
    (
      (await screen.findByRole("button", {
        name: "丢弃事件",
      })) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  expect(
    (
      screen.getByRole("checkbox", {
        name: "启用 AI 同事模式",
      }) as HTMLInputElement
    ).disabled,
  ).toBe(true);
});

it("allows stale-run recovery only after the operator confirms shutdown", async () => {
  await setConsoleLanguage("zh-CN");
  const data = snapshot();
  data.liveness.status = "stale";
  data.memory_maintenance = { state: "needs_review" };
  vi.mocked(api.getCoworker).mockResolvedValue(data);
  vi.mocked(api.coworkerRecovery).mockResolvedValue({ changed: true });
  const user = userEvent.setup();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <CoworkerScreen token="token" />
    </QueryClientProvider>,
  );
  const action = await screen.findByRole("button", {
    name: "解除记忆整理异常",
  });
  expect((action as HTMLButtonElement).disabled).toBe(true);
  await user.click(
    screen.getByRole("checkbox", {
      name: "心跳已过期，我已确认守护进程及相关后台任务停止",
    }),
  );
  await user.click(action);
  await user.click(screen.getByRole("button", { name: "确认执行" }));
  await waitFor(() =>
    expect(api.coworkerRecovery).toHaveBeenCalledWith(
      "token",
      "memory/reset",
      true,
    ),
  );
});

it("allows recovery after a failed startup", async () => {
  await setConsoleLanguage("zh-CN");
  const data = snapshot();
  data.liveness.status = "startup_failed";
  data.memory_maintenance = { state: "needs_review" };
  vi.mocked(api.getCoworker).mockResolvedValue(data);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <CoworkerScreen token="token" />
    </QueryClientProvider>,
  );
  expect(
    (
      (await screen.findByRole("button", {
        name: "解除记忆整理异常",
      })) as HTMLButtonElement
    ).disabled,
  ).toBe(false);
});

it("shows a failed app server and allows discarding a queued completion", async () => {
  await setConsoleLanguage("zh-CN");
  const data = snapshot();
  data.runtime = {
    business_mode: "coworker",
    send_mode: "agent_direct",
    chat_id: "group",
    provider: "codex",
  };
  data.backend_health = {
    status: "failed",
    message: "server stopped",
    checked_at: "2026-11-18T00:00:00Z",
  };
  data.events = [
    {
      id: 9,
      topic_key: "topic",
      message_id: "internal:subagent:child",
      status: "queued",
      error: null,
    },
  ];
  vi.mocked(api.getCoworker).mockResolvedValue(data);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <CoworkerScreen token="token" />
    </QueryClientProvider>,
  );
  expect(await screen.findByText("failed · server stopped")).toBeTruthy();
  expect(
    (screen.getByRole("button", { name: "丢弃事件" }) as HTMLButtonElement)
      .disabled,
  ).toBe(false);
});

function snapshot(): CoworkerSnapshot {
  const policy = {
    enabled: true,
    chat_id: "group",
    provider: "codex",
    send_mode: "agent_direct",
    trigger_mode: "mention" as const,
    history_source: "live_on_demand" as const,
    memory_maintenance: true,
    memory_maintenance_time: "00:00",
  };
  return {
    available: true,
    config: policy,
    policy,
    runtime: null,
    liveness: { status: "stopped" },
    memory_maintenance: null,
    sessions: [],
    events: [],
    subagents: [],
    history_gaps: [],
    delegations: [],
  };
}
