// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import type { ApprovalDetail, ApprovalSummary, CommandResult, TaskDetail } from "../types";
import { ApprovalsScreen } from "./ApprovalsScreen";

vi.mock("../api", () => ({
  approveApproval: vi.fn(),
  expireApprovals: vi.fn(),
  getApproval: vi.fn(),
  getTask: vi.fn(),
  listApprovals: vi.fn(),
  rejectApproval: vi.fn(),
  sendApproval: vi.fn()
}));

const approvalA = approval("a_one", 1, "t_one", "om_one", 3);
const approvalB = approval("a_two", 2, "t_two", "om_two", 7);

beforeEach(() => {
  vi.stubGlobal("crypto", { randomUUID: vi.fn(() => "test-command-id") });
  vi.mocked(api.listApprovals).mockResolvedValue([approvalA, approvalB]);
  vi.mocked(api.getApproval).mockImplementation(async (_token, approvalId) => {
    return approvalId === approvalB.approval_id ? approvalB : approvalA;
  });
  vi.mocked(api.getTask).mockImplementation(async (_token, taskId) => task(taskId));
  vi.mocked(api.expireApprovals).mockResolvedValue(commandResult("maintenance.expire_approvals"));
  vi.mocked(api.rejectApproval).mockResolvedValue(commandResult("approval.reject"));
  vi.mocked(api.sendApproval).mockResolvedValue(commandResult("approval.send"));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("ApprovalsScreen", () => {
  it("keeps drafts isolated by approval while switching", async () => {
    const user = userEvent.setup();
    renderScreen();
    await screen.findByRole("heading", { name: approvalA.approval_id });

    await user.type(screen.getByLabelText("Reason"), "reason for A");
    await user.type(screen.getByLabelText("Final reply"), "reply for A");
    await user.click(screen.getByRole("button", { name: new RegExp(approvalB.approval_id) }));
    await screen.findByRole("heading", { name: approvalB.approval_id });

    expect((screen.getByLabelText("Reason") as HTMLTextAreaElement).value).toBe("");
    expect((screen.getByLabelText("Final reply") as HTMLTextAreaElement).value).toBe("");
    await user.type(screen.getByLabelText("Reason"), "reason for B");
    await user.type(screen.getByLabelText("Final reply"), "reply for B");
    await user.click(screen.getByRole("button", { name: new RegExp(approvalA.approval_id) }));
    await screen.findByRole("heading", { name: approvalA.approval_id });

    expect((screen.getByLabelText("Reason") as HTMLTextAreaElement).value).toBe("reason for A");
    expect((screen.getByLabelText("Final reply") as HTMLTextAreaElement).value).toBe("reply for A");
  });

  it("binds a command to the submitted approval and keeps its late result there", async () => {
    const pending = deferred<CommandResult>();
    vi.mocked(api.approveApproval).mockReturnValue(pending.promise);
    const user = userEvent.setup();
    renderScreen();
    await screen.findByRole("heading", { name: approvalA.approval_id });
    await user.type(screen.getByLabelText("Reason"), "checked A");

    await user.dblClick(screen.getByRole("button", { name: "Approve" }));

    expect(api.approveApproval).toHaveBeenCalledTimes(1);
    expect(api.approveApproval).toHaveBeenCalledWith("token", approvalA.approval_id, {
      command_id: "console_test-command-id",
      expected_task_id: approvalA.task_id,
      expected_source_message_id: approvalA.source_message_id,
      expected_source_revision: approvalA.source_revision,
      reason: "checked A"
    });
    expect((screen.getByRole("button", { name: "Reject" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Send final reply" }) as HTMLButtonElement).disabled).toBe(true);

    await user.click(screen.getByRole("button", { name: new RegExp(approvalB.approval_id) }));
    await screen.findByRole("heading", { name: approvalB.approval_id });
    pending.resolve(commandResult("approval.approve", "result-for-A"));
    await waitFor(() => expect(screen.queryByText(/result-for-A/)).toBeNull());

    await user.click(screen.getByRole("button", { name: new RegExp(approvalA.approval_id) }));
    await screen.findByText(/result-for-A/);
  });
});

function renderScreen(): void {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ApprovalsScreen selectedId={approvalA.approval_id} token="token" />
    </QueryClientProvider>
  );
}

function approval(
  approvalId: string,
  taskId: number,
  taskShortId: string,
  sourceMessageId: string,
  sourceRevision: number
): ApprovalDetail {
  const summary: ApprovalSummary = {
    id: taskId,
    approval_id: approvalId,
    short_id: approvalId,
    task_id: taskId,
    task_short_id: taskShortId,
    source_message_id: sourceMessageId,
    source_revision: sourceRevision,
    kind: "send_reply",
    status: "pending",
    preview: `preview ${approvalId}`,
    created_at: "2026-09-16T00:00:00Z",
    expires_at: null,
    resolved_at: null,
    is_overdue: false,
    overdue_seconds: 0,
    recommended_action: "review",
    available_commands: [`approve ${approvalId}`, `reject ${approvalId}`, `send ${taskShortId}`]
  };
  return { ...summary, payload: { text: summary.preview } };
}

function task(taskId: string): TaskDetail {
  return {
    id: 1,
    task_id: taskId,
    task_short_id: taskId,
    short_id: taskId,
    status: "watching",
    chat_id: "oc_test",
    chat_type: "group",
    thread_id: null,
    root_message_id: "om_root",
    task_label: "test task",
    watch_until: null,
    agent_working_dir: null,
    updated_at: "2026-09-16T00:00:00Z",
    message_count: 0,
    recommended_actions: [],
    recent_messages: [],
    pending_approvals: [],
    actions: [],
    agent_audits: [],
    effective_policy: {
      policy_source: "global",
      auto_reply: true,
      bot_joined: true,
      reply_identity: "user",
      allow_user_fallback: false,
      resource_download: true
    }
  };
}

function commandResult(command: string, marker = "ok"): CommandResult {
  return {
    status: "applied",
    command,
    actor: "local_console",
    reason: null,
    target: {},
    changed: true,
    result: { marker },
    warnings: [],
    next_actions: []
  };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}
