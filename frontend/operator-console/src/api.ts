import type {
  ApprovalDetail,
  ApprovalStatus,
  ApprovalSummary,
  CommandResult,
  DashboardSnapshot,
  DispatchActionDetail,
  DispatchActionSummary,
  ActionStatus,
  MessageDetail,
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

async function postCommand(path: string, token: string, body: CommandBody): Promise<CommandResult> {
  return fetchApi(path, token, {
    method: "POST",
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
