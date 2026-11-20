// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import * as api from "../api";
import { setConsoleLanguage } from "../i18n";
import type { CoworkerSnapshot, NativeCoworkerSession } from "../types";
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
  await screen.findByText("暂无已确认的项目运行信息");
  const settings = screen.getByText("配置与生效策略");
  expect(settings.closest("details")?.open).toBe(false);
  await user.click(settings);
  expect(screen.getByRole("heading", { name: "配置文件" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "SQLite 生效策略" })).toBeTruthy();
  expect(screen.getByRole("option", { name: "Hermes（兼容后端）" })).toBeTruthy();
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
  const user = userEvent.setup();
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
  await user.click(screen.getByText("配置与生效策略"));
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
  expect(screen.getByText("AI 同事（运行方式未报告）")).toBeTruthy();
  expect(
    (screen.getByRole("button", { name: "丢弃事件" }) as HTMLButtonElement)
      .disabled,
  ).toBe(false);
});

it("shows the active Codex publication and reply mode before policy details", async () => {
  await setConsoleLanguage("zh-CN");
  const data = snapshot();
  data.liveness.status = "live";
  data.runtime = {
    business_mode: "coworker",
    coworker_runtime: "native",
    reply_mode: "topic",
    send_mode: "project",
    chat_id: "group",
    provider: "codex",
  };
  vi.mocked(api.getCoworker).mockResolvedValue(data);
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <CoworkerScreen token="token" />
    </QueryClientProvider>,
  );
  expect(await screen.findByText("Codex 直连")).toBeTruthy();
  expect(screen.getByText("项目长连接")).toBeTruthy();
  expect(screen.getByText("项目发布")).toBeTruthy();
  expect(screen.getByText("新开话题（topic）")).toBeTruthy();
  expect(screen.getByText("配置与生效策略").closest("details")?.open).toBe(false);
});

it("keeps the native Hermes entry available when the project store is unavailable", async () => {
  await setConsoleLanguage("zh-CN");
  vi.mocked(api.getCoworker).mockResolvedValue({ ...snapshot(), available: false });
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <CoworkerScreen token="token" />
    </QueryClientProvider>,
  );
  await screen.findByText("策略库尚不可用");
  const entry = screen.getByRole("link", { name: "打开 Hermes Dashboard" });
  expect(entry.getAttribute("href")).toBe(
    "http://127.0.0.1:9119/sessions?profile=coworker",
  );
  expect(entry.getAttribute("target")).toBe("_blank");
});

it("shows work by Codex session and copies the ready handoff command", async () => {
  await setConsoleLanguage("zh-CN");
  vi.mocked(api.getCoworker).mockResolvedValue({
    ...snapshot(),
    runtime_mode: "production",
    native_sessions: [reviewSession, readySession],
  });
  const user = userEvent.setup();
  const clipboard = vi.spyOn(navigator.clipboard, "writeText");
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <CoworkerScreen token="token" />
    </QueryClientProvider>,
  );
  const region = await screen.findByRole("region", { name: "Codex 直连待处理" });
  const work = within(region);
  expect(work.getByRole("heading", { name: "会话 #1" })).toBeTruthy();
  expect(work.getByText("投递 #7 待核对")).toBeTruthy();
  expect(work.getByText("receipt-topic")).toBeTruthy();
  expect(work.getByText("话题关注到期时间")).toBeTruthy();
  expect(work.getByText("已请求暂停")).toBeTruthy();
  expect(work.getByText("可人工接手")).toBeTruthy();
  expect(region.compareDocumentPosition(screen.getByText("配置与生效策略"))
    & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  await user.click(work.getByRole("button", { name: "复制接手命令" }));
  expect(clipboard).toHaveBeenCalledWith(readySession.handoff?.operator_command);
});

it("shows dry-run handoff status without a regular resume command", async () => {
  await setConsoleLanguage("zh-CN");
  vi.mocked(api.getCoworker).mockResolvedValue({
    ...snapshot(),
    runtime_mode: "dry_run",
    native_sessions: [readySession],
  });
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <CoworkerScreen token="token" />
    </QueryClientProvider>,
  );
  expect(await screen.findByText("可人工接手")).toBeTruthy();
  expect(screen.getByText("预演只显示接手状态。普通接手命令会脱离预演工具隔离。")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "复制接手命令" })).toBeNull();
});

const reviewSession: NativeCoworkerSession = {
  session_id: 1,
  app_id: "app",
  chat_id: "group",
  topic_key: "topic",
  deliveries: [{
    id: 7,
    purpose: "topic",
    target_message_id: "source",
    sent_message_id: "receipt",
    receipt_thread_id: "receipt-topic",
    error: "topic_not_verified",
    updated_at: "2026-11-20T00:00:00Z",
  }],
  handoff: {
    status: "pause_requested",
    error: null,
    updated_at: "2026-11-20T00:00:00Z",
    operator_command: null,
  },
  watch_expires_at: "2099-11-20T00:00:00Z",
};

const readySession: NativeCoworkerSession = {
  session_id: 2,
  app_id: "app",
  chat_id: "group",
  topic_key: "other-topic",
  deliveries: [],
  handoff: {
    status: "ready",
    error: null,
    updated_at: "2026-11-20T00:00:00Z",
    operator_command: "cd '/tmp/with space' && codex --no-daemon resume thread-id",
  },
  watch_expires_at: null,
};

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
