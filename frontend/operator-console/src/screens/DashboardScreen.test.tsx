// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import type { DashboardSnapshot } from "../types";
import { DashboardScreen } from "./DashboardScreen";

vi.mock("../api", () => ({
  expireApprovals: vi.fn(),
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
    expect(screen.getByText("待审批").parentElement?.textContent).toContain("23");
    expect(screen.getByRole("button", { name: /发送待核实或失败/ }).textContent).toContain("4");
    expect(screen.getByText("涉及 4 个任务")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "消息积压" })).toBeTruthy();
    expect(screen.getByText("ingest.p2p").parentElement?.textContent).toContain("page_cap_exhausted");
    expect(screen.getByText("oc_absent").parentElement?.textContent).toContain("已离群");

    await user.click(screen.getByRole("button", { name: /发送待核实或失败/ }));
    expect(navigate).toHaveBeenCalledWith("dispatch", undefined, "attention");

    await user.click(screen.getByRole("button", { name: /处理阻塞或失败/ }));
    expect(navigate).toHaveBeenCalledWith("tasks", undefined, "all");

    await user.click(screen.getByRole("button", { name: /需要处理的任务/ }));
    expect(navigate).toHaveBeenCalledWith("tasks", "t_attention");
    await user.click(screen.getByRole("button", { name: /a_attention/ }));
    expect(navigate).toHaveBeenCalledWith("approvals", "a_attention");
  });
});

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
