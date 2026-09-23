// @vitest-environment jsdom

import { TooltipProvider } from "@radix-ui/react-tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import type { SettingsCatalogEntry, SettingsRuntime } from "../types";
import { SettingsScreen } from "./SettingsScreen";

vi.mock("../api", () => ({
  getSettingsCatalog: vi.fn(),
  getSettingsRuntime: vi.fn()
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SettingsScreen", () => {
  it("shows one group, searches localized catalog copy, and reveals technical details on demand", async () => {
    vi.mocked(api.getSettingsCatalog).mockResolvedValue({ version: 1, entries: catalogEntries() });
    vi.mocked(api.getSettingsRuntime).mockResolvedValue(runtime());
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <SettingsScreen token="token" />
        </TooltipProvider>
      </QueryClientProvider>
    );

    expect(await screen.findByText("任务观察窗口")).toBeTruthy();
    expect(screen.queryByText("Codex 模型")).toBeNull();

    await user.type(screen.getByRole("searchbox", { name: "搜索设置" }), "Codex");
    expect(screen.getByText("Codex 模型")).toBeTruthy();
    expect(screen.getByRole("button", { name: "高级 (1)" }).getAttribute("aria-pressed")).toBe("true");
    await user.click(screen.getByText("技术信息"));
    expect(screen.getByText("agent_backend.codex.model")).toBeTruthy();
  });
});

function catalogEntries(): SettingsCatalogEntry[] {
  return [
    entry("lifecycle.watch_minutes", "normal"),
    entry("agent_backend.codex.model", "advanced"),
    entry("storage.sqlite_path", "diagnostic")
  ];
}

function entry(key: string, visibility: string): SettingsCatalogEntry {
  return {
    key,
    label: key,
    description: `Description for ${key}`,
    help: null,
    source: "config_yaml",
    scope: "runtime",
    visibility,
    editable_v1: false,
    requires_restart: false,
    audit_behavior: "read only",
    write_boundary: null
  };
}

function runtime(): SettingsRuntime {
  return {
    values: {
      "lifecycle.watch_minutes": 30,
      "agent_backend.codex.model": "gpt-6-sol",
      "storage.sqlite_path": "data/agent.db"
    },
    global_policy: null,
    chat_policies: [],
    policy_status: { initialized: true, policy_import_diff: { status: "matches" } },
    policy_audit_history: []
  };
}
