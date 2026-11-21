// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
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
const deepLinkedApproval = approval("a_deep", 3, "t_deep", "om_deep", 2);

beforeEach(() => {
  window.location.hash = "";
  vi.stubGlobal("crypto", { randomUUID: vi.fn(() => "test-command-id") });
  vi.mocked(api.listApprovals).mockResolvedValue([approvalA, approvalB]);
  vi.mocked(api.getApproval).mockImplementation(async (_token, approvalId) => {
    if (approvalId === approvalB.approval_id) {
      return approvalB;
    }
    return approvalId === deepLinkedApproval.approval_id ? deepLinkedApproval : approvalA;
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
    await screen.findByText(approvalA.preview ?? "", { selector: ".approval-reply" });

    await user.type(screen.getByLabelText("原因"), "reason for A");
    await user.type(screen.getByLabelText("最终回复"), "reply for A");
    await user.click(screen.getByRole("button", { name: new RegExp(approvalB.approval_id) }));
    await screen.findByText(approvalB.preview ?? "", { selector: ".approval-reply" });

    expect(window.location.hash).toBe(`#approvals/${approvalB.approval_id}`);
    expect((screen.getByLabelText("原因") as HTMLTextAreaElement).value).toBe("");
    expect((screen.getByLabelText("最终回复") as HTMLTextAreaElement).value).toBe("");
    await user.type(screen.getByLabelText("原因"), "reason for B");
    await user.type(screen.getByLabelText("最终回复"), "reply for B");
    await user.click(screen.getByRole("button", { name: new RegExp(approvalA.approval_id) }));
    await screen.findByText(approvalA.preview ?? "", { selector: ".approval-reply" });

    expect((screen.getByLabelText("原因") as HTMLTextAreaElement).value).toBe("reason for A");
    expect((screen.getByLabelText("最终回复") as HTMLTextAreaElement).value).toBe("reply for A");
  });

  it("binds a command to the submitted approval and keeps its late result there", async () => {
    const pending = deferred<CommandResult>();
    vi.mocked(api.approveApproval).mockReturnValue(pending.promise);
    const user = userEvent.setup();
    renderScreen();
    await screen.findByText(approvalA.preview ?? "", { selector: ".approval-reply" });
    await user.type(screen.getByLabelText("原因"), "checked A");

    await user.dblClick(screen.getByRole("button", { name: "批准" }));

    expect(api.approveApproval).toHaveBeenCalledTimes(1);
    expect(api.approveApproval).toHaveBeenCalledWith("token", approvalA.approval_id, {
      command_id: "console_test-command-id",
      expected_task_id: approvalA.task_id,
      expected_source_message_id: approvalA.source_message_id,
      expected_source_revision: approvalA.source_revision,
      reason: "checked A"
    });
    expect((screen.getByRole("button", { name: "拒绝" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "发送最终回复" }) as HTMLButtonElement).disabled).toBe(true);

    await user.click(screen.getByRole("button", { name: new RegExp(approvalB.approval_id) }));
    await screen.findByText(approvalB.preview ?? "", { selector: ".approval-reply" });
    pending.resolve(commandResult("approval.approve", "result-for-A"));
    await waitFor(() => expect(screen.queryByText(/result-for-A/)).toBeNull());

    await user.click(screen.getByRole("button", { name: new RegExp(approvalA.approval_id) }));
    await screen.findByText(/result-for-A/);
  });

  it("keeps a deep-linked detail selected when it is outside the current page", async () => {
    renderScreen(deepLinkedApproval.approval_id);

    await screen.findByText(deepLinkedApproval.preview ?? "", { selector: ".approval-reply" });
    expect(api.getApproval).toHaveBeenCalledWith("token", deepLinkedApproval.approval_id);
  });

  it("returns to the queue and reopens the approval with its draft", async () => {
    const user = userEvent.setup();
    renderScreen();
    await screen.findByText(approvalA.preview ?? "", { selector: ".approval-reply" });
    await user.type(screen.getByLabelText("最终回复"), "draft to keep");

    await user.click(screen.getByRole("button", { name: "返回队列" }));
    expect(window.location.hash).toBe("#approvals");
    expect(screen.getByRole("region", { name: "审批" }).getAttribute("data-detail-open")).toBe("false");

    await user.click(screen.getByRole("button", { name: new RegExp(approvalA.approval_id) }));
    expect(window.location.hash).toBe(`#approvals/${approvalA.approval_id}`);
    expect((screen.getByLabelText("最终回复") as HTMLTextAreaElement).value).toBe("draft to keep");
  });

  it("shows the reply destination and requires a choice when it is unresolved", async () => {
    vi.mocked(api.getApproval).mockResolvedValue({
      ...approvalA,
      payload: {
        reply_target_message_id: "om_root",
        allowed_reply_locations: ["main", "thread"]
      }
    });
    const user = userEvent.setup();
    renderScreen();
    await screen.findByText(approvalA.preview ?? "", { selector: ".approval-reply" });
    expect(screen.getByText("om_root")).toBeTruthy();
    expect(screen.getByText("未确认")).toBeTruthy();
    await user.type(screen.getByLabelText("最终回复"), "owner reply");
    const send = screen.getByRole("button", { name: "发送最终回复" }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    await user.click(screen.getByRole("button", { name: "已有话题" }));
    expect(send.disabled).toBe(false);
    await user.click(send);
    await waitFor(() => expect(api.sendApproval).toHaveBeenCalledWith("token", approvalA.approval_id, {
      command_id: "console_test-command-id",
      expected_task_id: approvalA.task_id,
      expected_source_message_id: approvalA.source_message_id,
      expected_source_revision: approvalA.source_revision,
      reason: undefined,
      final_reply: "owner reply",
      reply_location: "thread"
    }));
  });

  it("paginates beyond 50 rows and resets to the first page when filtering", async () => {
    const firstPage = Array.from({ length: 51 }, (_, index) =>
      approval(`a_page_${index}`, index + 10, `t_page_${index}`, `om_page_${index}`, 1)
    );
    vi.mocked(api.listApprovals).mockImplementation(async (_token, params) => {
      return params.offset === 50 ? [approvalB] : firstPage;
    });
    const user = userEvent.setup();
    renderScreen();
    await screen.findByRole("button", { name: "下一页" });

    await user.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => {
      expect(api.listApprovals).toHaveBeenCalledWith("token", { status: "pending", limit: 51, offset: 50 });
    });
    await user.click(screen.getByRole("button", { name: "已过期" }));
    await waitFor(() => {
      expect(api.listApprovals).toHaveBeenCalledWith("token", { status: "expired", limit: 51, offset: 0 });
    });
  });

  it("expires the entire queue and reports the server count beside the queue controls", async () => {
    vi.mocked(api.expireApprovals).mockResolvedValue({
      ...commandResult("maintenance.expire_approvals"),
      result: { expired_approvals: 3 }
    });
    const user = userEvent.setup();
    renderScreen();
    const queue = screen.getByRole("region", { name: "审批队列" });
    await screen.findByText(approvalA.preview ?? "", { selector: ".approval-reply" });
    await user.click(within(queue).getByRole("button", { name: "已处理" }));
    await user.click(within(queue).getByRole("button", { name: "批量标记超时审批" }));

    expect(within(queue).getByText(/当前筛选和分页之外/)).toBeTruthy();
    expect(api.expireApprovals).not.toHaveBeenCalled();
    await user.click(within(queue).getByRole("button", { name: "确认执行" }));
    expect(api.expireApprovals).toHaveBeenCalledWith("token", { reason: undefined });
    expect(await within(queue).findByText("已标记 3 条超时审批")).toBeTruthy();
  });
});

function renderScreen(selectedId = approvalA.approval_id): void {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ApprovalsScreen selectedId={selectedId} token="token" />
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
  return {
    ...summary,
    payload: {
      text: summary.preview,
      reply_target_message_id: sourceMessageId,
      reply_destination: { chat_id: "oc_test", location: "main" }
    }
  };
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
    task_background: null,
    task_background_history: [],
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
