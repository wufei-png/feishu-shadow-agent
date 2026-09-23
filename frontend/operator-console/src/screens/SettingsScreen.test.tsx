// @vitest-environment jsdom

import { TooltipProvider } from "@radix-ui/react-tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import { setConsoleLanguage } from "../i18n";
import { PreferencesProvider } from "../preferences";
import type { SettingsCatalogEntry, SettingsRuntime } from "../types";
import { SettingsScreen } from "./SettingsScreen";

vi.mock("../api", () => ({
  getSettingsCatalog: vi.fn(),
  getSettingsRuntime: vi.fn()
}));

beforeEach(async () => {
  await setConsoleLanguage("zh-CN");
  localStorage.clear();
  vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({
    matches: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn()
  }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

function renderSettings() {
  vi.mocked(api.getSettingsCatalog).mockResolvedValue({ version: 1, entries: catalogEntries() });
  vi.mocked(api.getSettingsRuntime).mockResolvedValue(runtime());
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <PreferencesProvider>
          <SettingsScreen token="token" />
        </PreferencesProvider>
      </TooltipProvider>
    </QueryClientProvider>
  );
}

describe("SettingsScreen", () => {
  it("shows one group, searches localized catalog copy, and reveals technical details on demand", async () => {
    const user = userEvent.setup();
    renderSettings();

    expect(await screen.findByText("任务观察窗口")).toBeTruthy();
    expect(screen.queryByText("Codex 模型")).toBeNull();

    await user.type(screen.getByRole("searchbox", { name: "搜索设置" }), "Codex");
    expect(screen.getByText("Codex 模型")).toBeTruthy();
    expect(screen.getByRole("button", { name: "高级 (1)" }).getAttribute("aria-pressed")).toBe("true");
    await user.click(screen.getByText("技术信息"));
    expect(screen.getByText("agent_backend.codex.model")).toBeTruthy();
  });

  it("changes appearance from the console's own preference group", async () => {
    const user = userEvent.setup();
    renderSettings();

    expect(await screen.findByText("外观与语言")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "深色" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("feishu_shadow_agent_console_theme")).toBe("dark");

    await user.click(screen.getByRole("button", { name: "浅色" }));
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("switches interface language without leaving the settings page", async () => {
    const user = userEvent.setup();
    renderSettings();

    await user.click(await screen.findByRole("button", { name: "English" }));

    expect(await screen.findByText("Appearance and language")).toBeTruthy();
    expect(document.documentElement.lang).toBe("en-US");
  });

  it("keeps preferences reachable when the catalog is filtered to nothing", async () => {
    const user = userEvent.setup();
    renderSettings();

    await user.type(await screen.findByRole("searchbox", { name: "搜索设置" }), "no-such-setting");

    expect(screen.getByText("外观与语言")).toBeTruthy();
    expect(screen.getByRole("button", { name: "深色" })).toBeTruthy();
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
