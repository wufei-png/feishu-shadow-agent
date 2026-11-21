// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import type { DashboardSnapshot, RouteKey } from "../types";
import { DashboardScreen } from "./DashboardScreen";

vi.mock("../api", () => ({
  getDashboard: vi.fn()
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DashboardScreen", () => {
  it("uses full attention totals and keeps task and object entry points", async () => {
    vi.mocked(api.getDashboard).mockResolvedValue(snapshot());
    const navigate = vi.fn();
    const user = userEvent.setup();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <DashboardScreen navigate={navigate} token="token" />
      </QueryClientProvider>
    );

    await screen.findByRole("heading", { name: "待我处理" });
    expect(screen.getByText("待审批", { selector: ".decision-label" }).parentElement?.textContent).toContain("23");
    expect(screen.getByRole("button", { name: /发送待核实或失败/ }).textContent).toContain("4");
    expect(screen.getByText("涉及 4 个任务")).toBeTruthy();
    await user.click(screen.getByText("机器当前状态", { selector: "summary" }));
    expect(screen.getByRole("heading", { name: "消息积压" })).toBeTruthy();
    const backlog = screen.getByText("ingest.p2p").parentElement;
    expect(backlog?.textContent).toContain("已达到单次抓取页数上限");
    expect(backlog?.querySelector("small")?.getAttribute("title")).toBe("page_cap_exhausted");
    expect(screen.getByText("oc_absent").parentElement?.textContent).toContain("已离群");

    await user.click(screen.getByRole("button", { name: /发送待核实或失败/ }));
    expect(screen.getByRole("button", { name: /发送待核实或失败/ }).getAttribute("aria-pressed")).toBe("true");
    await user.click(screen.getByRole("button", { name: "打开完整队列" }));
    expect(navigate).toHaveBeenCalledWith("dispatch", undefined, "attention");

    await user.click(screen.getByRole("button", { name: /处理阻塞或失败/ }));
    await user.click(screen.getByRole("button", { name: "打开完整队列" }));
    expect(navigate).toHaveBeenCalledWith("tasks", undefined, "all");

    await user.click(screen.getByRole("button", { name: "查看全部待办" }));
    await user.click(screen.getByRole("button", { name: "按任务" }));
    await user.click(screen.getByText(/需要处理的任务/, { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "打开任务" }));
    expect(navigate).toHaveBeenCalledWith("tasks", "t_attention");
    await user.click(screen.getByRole("button", { name: /建议内容/ }));
    expect(navigate).toHaveBeenCalledWith("approvals", "a_attention");
  });

  it("directs an uninitialized policy to the policy page", async () => {
    vi.mocked(api.getDashboard).mockResolvedValue(emptySnapshot({
      policy_status: { initialized: false, policy_import_diff: { status: "unknown" } }
    }));
    const navigate = vi.fn();
    const user = userEvent.setup();
    renderDashboard(navigate);

    expect(await screen.findByRole("heading", { name: "先初始化产品策略" })).toBeTruthy();
    expect(screen.queryByText("已清空")).toBeNull();
    await user.click(screen.getByRole("button", { name: "前往策略页" }));
    expect(navigate).toHaveBeenCalledWith("policy");
  });

  it("directs an unavailable store to health before suggesting policy import", async () => {
    vi.mocked(api.getDashboard).mockResolvedValue(emptySnapshot({
      policy_status: { initialized: false, policy_import_diff: { status: "unknown" } },
      store_status: { status: "schema_incompatible", available: false },
      health_issue_summary: { highest_severity: "critical", open_issue_count: 1 },
      has_messages: null
    }));
    const navigate = vi.fn();
    const user = userEvent.setup();
    renderDashboard(navigate);

    expect(await screen.findByRole("heading", { name: "无法访问运行数据库" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "先初始化产品策略" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "检查健康状态" }));
    expect(navigate).toHaveBeenCalledWith("health");
  });

  it("shows dry-run startup guidance in the correct mode", async () => {
    vi.mocked(api.getDashboard).mockResolvedValue(emptySnapshot());
    const navigate = vi.fn();
    const user = userEvent.setup();
    renderDashboard(navigate);

    expect(await screen.findByRole("heading", { name: "启动守护进程" })).toBeTruthy();
    expect(screen.getByText(/python -m feishu_shadow_agent daemon/).textContent).toContain("--dry-run");
    expect(screen.queryByText("当前没有待处理事项")).toBeNull();
    await user.click(screen.getByRole("button", { name: "前往维护页" }));
    expect(navigate).toHaveBeenCalledWith("maintenance");
  });

  it("warns before showing a production startup command", async () => {
    vi.mocked(api.getDashboard).mockResolvedValue(emptySnapshot({ runtime_mode: "production" }));
    renderDashboard(vi.fn());

    expect(await screen.findByText(/生产模式会按照当前策略真实发送/)).toBeTruthy();
    expect(screen.getByText(/python -m feishu_shadow_agent daemon/).textContent).not.toContain("--dry-run");
  });

  it("keeps pending work visible when the latest daemon startup failed", async () => {
    vi.mocked(api.getDashboard).mockResolvedValue({
      ...snapshot(),
      daemon_liveness: { status: "startup_failed" }
    });
    const navigate = vi.fn();
    const user = userEvent.setup();
    renderDashboard(navigate);

    expect(await screen.findByRole("heading", { name: "最近一次启动失败" })).toBeTruthy();
    expect(screen.getByText("待审批", { selector: ".decision-label" }).parentElement?.textContent).toContain("23");
    await user.click(screen.getByRole("button", { name: "检查健康状态" }));
    expect(navigate).toHaveBeenCalledWith("health");
  });

  it.each([
    ["starting", "守护进程正在启动"],
    ["stale", "守护进程心跳已过期"],
    ["stopped", "守护进程未运行"]
  ])("explains the %s daemon state", async (status, title) => {
    vi.mocked(api.getDashboard).mockResolvedValue(emptySnapshot({ daemon_liveness: { status } }));
    renderDashboard(vi.fn());

    expect(await screen.findByRole("heading", { name: title })).toBeTruthy();
    expect(screen.queryByText("当前没有待处理事项")).toBeNull();
  });

  it("distinguishes waiting for the first message from a clear action queue", async () => {
    vi.mocked(api.getDashboard).mockResolvedValue(emptySnapshot({ daemon_liveness: { status: "live" } }));
    renderDashboard(vi.fn());

    expect(await screen.findByRole("heading", { name: "等待第一条消息" })).toBeTruthy();
    expect(screen.getByText(/另一位成员在新消息中 @ 你的 owner 账号/)).toBeTruthy();
    expect(screen.queryByText("当前没有待处理事项")).toBeNull();
  });

  it("shows a clear queue after a message has been recorded", async () => {
    vi.mocked(api.getDashboard).mockResolvedValue(emptySnapshot({ daemon_liveness: { status: "live" }, has_messages: true }));
    renderDashboard(vi.fn());

    expect(await screen.findByRole("heading", { name: "当前没有待处理事项" })).toBeTruthy();
    expect(screen.queryByText("等待第一条消息")).toBeNull();
  });
});

function renderDashboard(navigate: (route: RouteKey, selectedId?: string, view?: "attention" | "all") => void) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><DashboardScreen navigate={navigate} token="token" /></QueryClientProvider>);
}

function emptySnapshot(overrides: Partial<DashboardSnapshot> = {}): DashboardSnapshot {
  return {
    ...snapshot(),
    runtime_mode: "dry_run",
    daemon_liveness: { status: "not_started" },
    has_messages: false,
    attention_summary: {
      pending_approval_count: 0,
      overdue_approval_count: 0,
      failed_action_count: 0,
      uncertain_action_count: 0,
      blocked_processing_count: 0,
      failed_processing_count: 0,
      affected_task_count: 0,
      total_item_count: 0
    },
    attention_tasks: [],
    pending_approvals: [],
    ...overrides
  };
}

function snapshot(): DashboardSnapshot {
  return {
    attention_summary: {
      pending_approval_count: 23,
      overdue_approval_count: 2,
      failed_action_count: 3,
      uncertain_action_count: 1,
      blocked_processing_count: 2,
      failed_processing_count: 1,
      affected_task_count: 4,
      total_item_count: 30
    },
    attention_tasks: [
      {
        task_id: 1,
        task_short_id: "t_attention",
        task_label: "需要处理的任务",
        chat_id: "oc_chat",
        status: "watching",
        pending_approval_count: 2,
        overdue_approval_count: 1,
        failed_action_count: 1,
        uncertain_action_count: 1,
        blocked_processing_count: 0,
        failed_processing_count: 0,
        latest_at: "2026-09-16T00:00:00Z"
      }
    ],
    ingestion_status: {
      summary: {
        source_count: 2,
        backlog_count: 1,
        budget_exhausted_count: 0,
        oldest_checkpoint_age_seconds: 3600
      },
      sources: [
        {
          checkpoint_key: "ingest.p2p",
          updated_at: "2026-09-16T00:00:00Z",
          last_success_at: null,
          checkpoint_age_seconds: null,
          drain_complete: false,
          backlog: { reason: "page_cap_exhausted", pages_fetched: 2, messages_fetched: 100 },
          last_drain: null
        }
      ]
    },
    bot_membership_status: {
      summary: { present: 1, absent: 1, unknown: 0, unobserved: 0 },
      facts: [
        {
          chat_id: "oc_absent",
          status: "absent",
          observed_status: "absent",
          checked_at: "2026-09-16T00:00:00Z",
          next_probe_at: "2026-09-16T00:05:00Z",
          source: "active_probe",
          error: null,
          updated_at: "2026-09-16T00:00:00Z"
        }
      ]
    },
    pending_approvals: [
      {
        id: 1,
        approval_id: "a_attention",
        short_id: "a_attention",
        task_id: 1,
        task_short_id: "t_attention",
        source_message_id: "om_source",
        source_revision: 1,
        kind: "send_reply",
        status: "pending",
        preview: "建议内容",
        created_at: "2026-09-16T00:00:00Z",
        expires_at: null,
        resolved_at: null,
        is_overdue: false,
        overdue_seconds: 0,
        recommended_action: "review",
        available_commands: ["approve a_attention"]
      }
    ],
    failed_or_needs_review_actions: [],
    stale_sending_actions: [],
    recent_errors: [],
    health_issue_summary: { highest_severity: "info", open_issue_count: 0 },
    policy_status: { initialized: true, policy_import_diff: { status: "matches" } },
    last_run: null
  };
}
