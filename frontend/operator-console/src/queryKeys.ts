import type { QueryClient } from "@tanstack/react-query";
import type { ActionStatus, ApprovalStatus, TaskStatus } from "./types";

export type ApprovalFilter = "all" | "pending" | "expired" | "resolved";

export const queryKeys = {
  dashboard: () => ["dashboard"] as const,
  healthIssues: () => ["health-issues"] as const,
  approvals: (filters: { status?: ApprovalStatus | ApprovalFilter; limit?: number; offset?: number }) =>
    ["approvals", filters] as const,
  approval: (approvalId: string | null) => ["approval", approvalId] as const,
  tasks: (filters: { status?: TaskStatus | "all"; chat_id?: string; limit?: number; offset?: number }) =>
    ["tasks", filters] as const,
  task: (taskId: string | null) => ["task", taskId] as const,
  dispatchActions: (filters: { status?: ActionStatus | "all"; limit?: number; offset?: number }) =>
    ["dispatch-actions", filters] as const,
  dispatchAction: (actionId: number | null) => ["dispatch-action", actionId] as const,
  messageDetail: (messageId: string | null) => ["message-detail", messageId] as const,
  policyStatus: () => ["policy-status"] as const,
  policyAudits: (filters: { limit?: number; offset?: number; scope?: string; policy_key?: string; since?: string }) =>
    ["policy-audits", filters] as const,
  settingsCatalog: () => ["settings-catalog"] as const,
  settingsRuntime: () => ["settings-runtime"] as const
};

export function invalidateAfterApprovalCommand(queryClient: QueryClient): Promise<void[]> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ["dashboard"] }),
    queryClient.invalidateQueries({ queryKey: ["health-issues"] }),
    queryClient.invalidateQueries({ queryKey: ["approvals"] }),
    queryClient.invalidateQueries({ queryKey: ["approval"] }),
    queryClient.invalidateQueries({ queryKey: ["tasks"] }),
    queryClient.invalidateQueries({ queryKey: ["task"] }),
    queryClient.invalidateQueries({ queryKey: ["dispatch-actions"] }),
    queryClient.invalidateQueries({ queryKey: ["dispatch-action"] }),
    queryClient.invalidateQueries({ queryKey: ["message-detail"] })
  ]);
}

export function invalidateAfterDispatchCommand(queryClient: QueryClient): Promise<void[]> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ["dashboard"] }),
    queryClient.invalidateQueries({ queryKey: ["health-issues"] }),
    queryClient.invalidateQueries({ queryKey: ["dispatch-actions"] }),
    queryClient.invalidateQueries({ queryKey: ["dispatch-action"] }),
    queryClient.invalidateQueries({ queryKey: ["tasks"] }),
    queryClient.invalidateQueries({ queryKey: ["message-detail"] })
  ]);
}

export function invalidateAfterMaintenanceCommand(queryClient: QueryClient): Promise<void[]> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ["dashboard"] }),
    queryClient.invalidateQueries({ queryKey: ["health-issues"] }),
    queryClient.invalidateQueries({ queryKey: ["approvals"] }),
    queryClient.invalidateQueries({ queryKey: ["approval"] }),
    queryClient.invalidateQueries({ queryKey: ["tasks"] }),
    queryClient.invalidateQueries({ queryKey: ["task"] }),
    queryClient.invalidateQueries({ queryKey: ["message-detail"] })
  ]);
}

export function invalidateAfterTaskCommand(queryClient: QueryClient): Promise<void[]> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ["dashboard"] }),
    queryClient.invalidateQueries({ queryKey: ["health-issues"] }),
    queryClient.invalidateQueries({ queryKey: ["tasks"] }),
    queryClient.invalidateQueries({ queryKey: ["task"] }),
    queryClient.invalidateQueries({ queryKey: ["approvals"] }),
    queryClient.invalidateQueries({ queryKey: ["approval"] }),
    queryClient.invalidateQueries({ queryKey: ["dispatch-actions"] }),
    queryClient.invalidateQueries({ queryKey: ["dispatch-action"] }),
    queryClient.invalidateQueries({ queryKey: ["message-detail"] })
  ]);
}

export function invalidateAfterPolicyCommand(queryClient: QueryClient): Promise<void[]> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ["dashboard"] }),
    queryClient.invalidateQueries({ queryKey: ["health-issues"] }),
    queryClient.invalidateQueries({ queryKey: ["policy-status"] }),
    queryClient.invalidateQueries({ queryKey: ["policy-audits"] }),
    queryClient.invalidateQueries({ queryKey: ["settings-runtime"] }),
    queryClient.invalidateQueries({ queryKey: ["settings-catalog"] }),
    queryClient.invalidateQueries({ queryKey: ["tasks"] }),
    queryClient.invalidateQueries({ queryKey: ["task"] }),
    queryClient.invalidateQueries({ queryKey: ["message-detail"] })
  ]);
}
