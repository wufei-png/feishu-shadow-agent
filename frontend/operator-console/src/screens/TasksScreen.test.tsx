// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import type { TaskDetail } from "../types";
import { TasksScreen } from "./TasksScreen";

vi.mock("../api", () => ({
  closeTask: vi.fn(),
  getTask: vi.fn(),
  listTasks: vi.fn(),
  reopenTask: vi.fn(),
  retryMessageProcessing: vi.fn()
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("TasksScreen processing recovery", () => {
  it("queues the selected terminal stage and disables an active retry", async () => {
    const detail = taskDetail();
    vi.mocked(api.listTasks).mockResolvedValue([detail]);
    vi.mocked(api.getTask).mockResolvedValue(detail);
    vi.mocked(api.retryMessageProcessing).mockResolvedValue({
      status: "applied",
      command: "processing.retry",
      actor: "local_console",
      reason: null,
      target: {},
      changed: true,
      result: {},
      warnings: [],
      next_actions: []
    });
    const user = userEvent.setup();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <TasksScreen selectedId="t_retry" token="token" />
      </QueryClientProvider>
    );

    await screen.findByRole("heading", { name: "Processing recovery" });
    const buttons = screen.getAllByRole("button", { name: "重试" });
    expect(buttons).toHaveLength(1);
    expect(screen.getByRole("button", { name: "已排队" }).hasAttribute("disabled")).toBe(true);

    await user.click(buttons[0]);

    expect(api.retryMessageProcessing).toHaveBeenCalledWith(
      "token",
      "om_failed",
      "task_session",
      { reason: undefined }
    );
  });
});

function taskDetail(): TaskDetail {
  return {
    id: 1,
    task_id: "t_retry",
    task_short_id: "t_retry",
    short_id: "t_retry",
    status: "watching",
    chat_id: "oc_1",
    chat_type: "group",
    thread_id: null,
    root_message_id: "om_failed",
    task_label: "Retry task",
    watch_until: "2026-09-17T00:00:00Z",
    agent_working_dir: null,
    updated_at: "2026-09-16T00:00:00Z",
    message_count: 1,
    recent_messages: [],
    pending_approvals: [],
    actions: [],
    agent_audits: [],
    processing: [
      {
        id: 1,
        message_id: "om_failed",
        revision: 1,
        task_id: 1,
        stage: "task_session",
        status: "processing_failed_terminal",
        attempt_count: 3,
        last_error: "provider failed",
        terminal_reason: "agent_retry_exhausted",
        created_at: "2026-09-16T00:00:00Z",
        updated_at: "2026-09-16T00:00:00Z",
        latest_retry: null
      },
      {
        id: 2,
        message_id: "om_blocked",
        revision: 1,
        task_id: 1,
        stage: "resource_download",
        status: "blocked_waiting_external",
        attempt_count: 0,
        last_error: null,
        terminal_reason: "bot_not_joined",
        created_at: "2026-09-16T00:00:00Z",
        updated_at: "2026-09-16T00:00:00Z",
        latest_retry: {
          id: 3,
          status: "queued",
          actor: "local_console",
          reason: null,
          error: null,
          created_at: "2026-09-16T00:00:00Z",
          finished_at: null
        }
      }
    ],
    effective_policy: {
      policy_source: "explicit_chat",
      auto_reply: true,
      bot_joined: true,
      reply_identity: "bot_preferred",
      allow_user_fallback: true,
      resource_download: true
    },
    recommended_actions: []
  };
}
