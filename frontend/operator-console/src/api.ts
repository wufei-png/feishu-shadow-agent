import type {
  ApprovalDetail,
  ApprovalStatus,
  ApprovalSummary,
  CommandResult,
  DashboardSnapshot,
  DispatchActionDetail,
  DispatchActionSummary,
  ActionStatus,
  HealthIssuesResponse,
  MessageDetail,
  PolicyAudit,
  PolicyStatus,
  SettingsCatalog,
  SettingsRuntime,
  TaskDetail,
  TaskStatus,
  TaskSummary
} from "./types";

type CommandBody = {
  reason?: string;
  command_id?: string;
  final_reply?: string;
  sent_message_id?: string;
};

export type PolicyImportBody = {
  reason?: string;
  replace?: boolean;
};

export type GlobalPolicyUpdateBody = {
  reason?: string;
  p2p_auto_reply?: boolean;
  unknown_group_auto_reply?: boolean;
  bot_joined?: boolean;
  reply_identity?: string;
  allow_user_fallback?: boolean;
  resource_download?: boolean;
};

export type ChatPolicyUpdateBody = {
  reason?: string;
  name?: string;
  auto_reply?: boolean;
  bot_joined?: boolean;
  reply_identity?: string;
  allow_user_fallback?: boolean;
  resource_download?: boolean;
};

export type PolicyDeleteBody = {
  reason?: string;
};

type ListParams = {
  limit?: number;
  offset?: number;
};

export class ApiError extends Error {
  code: string;
  details: Record<string, unknown>;
  status: number;

  constructor(
    message: string,
    options: {
      code: string;
      details: Record<string, unknown>;
      status: number;
    }
  ) {
    super(message);
    this.name = "ApiError";
    this.code = options.code;
    this.details = options.details;
    this.status = options.status;
  }
}

export function getDashboard(token: string): Promise<DashboardSnapshot> {
  return fetchApi("/api/dashboard", token);
}

export function getHealthIssues(token: string): Promise<HealthIssuesResponse> {
  return fetchApi("/api/health/issues", token);
}

export function listApprovals(
  token: string,
  params: ListParams & { status?: ApprovalStatus }
): Promise<ApprovalSummary[]> {
  return fetchApi(`/api/approvals${queryString(params)}`, token);
}

export function getApproval(token: string, approvalId: string): Promise<ApprovalDetail> {
  return fetchApi(`/api/approvals/${encodeURIComponent(approvalId)}`, token);
}

export function approveApproval(token: string, approvalId: string, body: CommandBody): Promise<CommandResult> {
  return postCommand(`/api/approvals/${encodeURIComponent(approvalId)}/approve`, token, body);
}

export function rejectApproval(token: string, approvalId: string, body: CommandBody): Promise<CommandResult> {
  return postCommand(`/api/approvals/${encodeURIComponent(approvalId)}/reject`, token, body);
}

export function listTasks(token: string, params: ListParams & { status?: TaskStatus; chat_id?: string }): Promise<TaskSummary[]> {
  return fetchApi(`/api/tasks${queryString(params)}`, token);
}

export function getTask(token: string, taskId: string): Promise<TaskDetail> {
  return fetchApi(`/api/tasks/${encodeURIComponent(taskId)}`, token);
}

export function sendTask(token: string, taskId: string, body: CommandBody): Promise<CommandResult> {
  return postCommand(`/api/tasks/${encodeURIComponent(taskId)}/send`, token, body);
}

export function closeTask(token: string, taskId: string, body: CommandBody): Promise<CommandResult> {
  return postCommand(`/api/tasks/${encodeURIComponent(taskId)}/close`, token, body);
}

export function reopenTask(token: string, taskId: string, body: CommandBody): Promise<CommandResult> {
  return postCommand(`/api/tasks/${encodeURIComponent(taskId)}/reopen`, token, body);
}

export function expireApprovals(token: string, body: CommandBody): Promise<CommandResult> {
  return postCommand("/api/maintenance/expire-approvals", token, body);
}

export function listDispatchActions(
  token: string,
  params: ListParams & { status?: ActionStatus | ActionStatus[] }
): Promise<DispatchActionSummary[]> {
  return fetchApi(`/api/dispatch/actions${queryString(params)}`, token);
}

export function getDispatchAction(token: string, actionId: number): Promise<DispatchActionDetail> {
  return fetchApi(`/api/dispatch/actions/${actionId}`, token);
}

export function retryDispatchAction(token: string, actionId: number, body: CommandBody): Promise<CommandResult> {
  return postCommand(`/api/dispatch/actions/${actionId}/retry`, token, body);
}

export function cancelDispatchAction(token: string, actionId: number, body: CommandBody): Promise<CommandResult> {
  return postCommand(`/api/dispatch/actions/${actionId}/cancel`, token, body);
}

export function markDispatchSent(token: string, actionId: number, body: CommandBody): Promise<CommandResult> {
  return postCommand(`/api/dispatch/actions/${actionId}/mark-sent`, token, body);
}

export function getMessageDetail(token: string, messageId: string): Promise<MessageDetail> {
  return fetchApi(`/api/messages/${encodeURIComponent(messageId)}/detail`, token);
}

export function getPolicyStatus(token: string): Promise<PolicyStatus> {
  return fetchApi("/api/policy/status", token);
}

export function listPolicyAudits(token: string, params: ListParams & { scope?: string; policy_key?: string; since?: string }): Promise<PolicyAudit[]> {
  return fetchApi(`/api/policy/audits${queryString(params)}`, token);
}

export function importPolicyConfig(token: string, body: PolicyImportBody): Promise<CommandResult> {
  return postCommand("/api/policy/import-config", token, body);
}

export function updateGlobalPolicy(token: string, body: GlobalPolicyUpdateBody): Promise<CommandResult> {
  return patchCommand("/api/policy/global", token, body);
}

export function updateChatPolicy(token: string, chatId: string, body: ChatPolicyUpdateBody): Promise<CommandResult> {
  return patchCommand(`/api/policy/chats/${encodeURIComponent(chatId)}`, token, body);
}

export function deleteChatPolicy(token: string, chatId: string, body: PolicyDeleteBody): Promise<CommandResult> {
  return deleteCommand(`/api/policy/chats/${encodeURIComponent(chatId)}`, token, body);
}

export function getSettingsCatalog(token: string): Promise<SettingsCatalog> {
  return fetchApi("/api/settings/catalog", token);
}

export function getSettingsRuntime(token: string): Promise<SettingsRuntime> {
  return fetchApi("/api/settings/runtime", token);
}

async function postCommand(path: string, token: string, body: Record<string, unknown>): Promise<CommandResult> {
  return fetchApi(path, token, {
    method: "POST",
    body: JSON.stringify(body)
  });
}

async function patchCommand(path: string, token: string, body: Record<string, unknown>): Promise<CommandResult> {
  return fetchApi(path, token, {
    method: "PATCH",
    body: JSON.stringify(body)
  });
}

async function deleteCommand(path: string, token: string, body: Record<string, unknown>): Promise<CommandResult> {
  return fetchApi(path, token, {
    method: "DELETE",
    body: JSON.stringify(body)
  });
}

async function fetchApi<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...(init?.headers ?? {})
    }
  });
  const body = (await response.json()) as unknown;
  if (!response.ok) {
    if (isErrorEnvelope(body)) {
      throw new ApiError(body.error.message, {
        code: body.error.code,
        details: body.error.details,
        status: response.status
      });
    }
    throw new ApiError(`Request failed: ${response.status}`, {
      code: "request_failed",
      details: {},
      status: response.status
    });
  }
  return body as T;
}

function queryString(params: Record<string, unknown>): string {
  const search = new URLSearchParams();
  for (const [key, rawValue] of Object.entries(params)) {
    if (rawValue === undefined || rawValue === null || rawValue === "" || rawValue === "all") {
      continue;
    }
    const values = Array.isArray(rawValue) ? rawValue : [rawValue];
    for (const value of values) {
      search.append(key, String(value));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

function isErrorEnvelope(value: unknown): value is {
  error: { code: string; message: string; details: Record<string, unknown> };
} {
  if (typeof value !== "object" || value === null || !("error" in value)) {
    return false;
  }
  const error = (value as { error?: unknown }).error;
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    "message" in error &&
    "details" in error &&
    typeof (error as { code?: unknown }).code === "string" &&
    typeof (error as { message?: unknown }).message === "string"
  );
}
