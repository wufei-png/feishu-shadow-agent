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
  delete document.documentElement.dataset.material;
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

function renderSettings(overrides: { entries?: SettingsCatalogEntry[]; runtime?: SettingsRuntime } = {}) {
  vi.mocked(api.getSettingsCatalog).mockResolvedValue({ version: 1, entries: [...catalogEntries(), ...(overrides.entries ?? [])] });
  vi.mocked(api.getSettingsRuntime).mockResolvedValue(overrides.runtime ?? runtime());
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

  it("stores color scheme and material independently", async () => {
    const user = userEvent.setup();
    renderSettings();

    expect(await screen.findByText("外观与语言")).toBeTruthy();
    expect(screen.getByRole("button", { name: "透明玻璃" }).getAttribute("aria-pressed")).toBe("true");

    await user.click(screen.getByRole("button", { name: "石墨实底" }));
    expect(document.documentElement.dataset.material).toBe("solid");
    expect(localStorage.getItem("feishu_shadow_agent_console_material")).toBe("solid");

    await user.click(screen.getByRole("button", { name: "深色" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("feishu_shadow_agent_console_theme")).toBe("dark");
    expect(document.documentElement.dataset.material).toBe("solid");

    await user.click(screen.getByRole("button", { name: "透明玻璃" }));
    expect(document.documentElement.dataset.material).toBe("glass");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("switches interface language without leaving the settings page", async () => {
    const user = userEvent.setup();
    renderSettings();

    await user.click(await screen.findByRole("button", { name: "English" }));

    expect(await screen.findByText("Appearance and language")).toBeTruthy();
    expect(document.documentElement.lang).toBe("en-US");
  });

  it("keeps policy-owned fields out of the default group and links them to Policy", async () => {
    const user = userEvent.setup();
    renderSettings();

    // 常规 must not contain a card the operator can only act on elsewhere.
    expect(await screen.findByText("任务观察窗口")).toBeTruthy();
    expect(screen.queryByText("policy.global.p2p_auto_reply")).toBeNull();
    expect(screen.getByRole("button", { name: "常规 (1)" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "策略字段 (1)" })).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "策略字段 (1)" }));

    const link = await screen.findByRole("link", { name: "在策略页修改" });
    expect(link.getAttribute("href")).toBe("#policy");
    // The dead-end badge is gone for policy-owned fields.
    expect(screen.queryByText("在策略页修改", { selector: "span" })).toBeNull();
  });

  it("says a per-chat field is set per chat instead of showing a chat count as its value", async () => {
    const user = userEvent.setup();
    renderSettings();

    await user.click(await screen.findByRole("button", { name: "策略字段 (1)" }));

    expect(screen.queryByText("2 条会话策略")).toBeNull();
  });

  it("shows a derived count when the API lists the field with a null value", async () => {
    // The API lists `policy.audit.history` in `values` as null, so checking only
    // for key presence made the card claim "not set" while records existed.
    const user = userEvent.setup();
    renderSettings({
      entries: [entry("policy.audit.history", "readonly", { scope: "policy_audit" })],
      runtime: { ...runtime(), values: { "policy.audit.history": null }, policy_audit_history: [{}, {}, {}] as SettingsRuntime["policy_audit_history"] }
    });

    await user.click(await screen.findByRole("button", { name: /^策略字段/ }));

    const auditCard = (await screen.findByText("策略审计记录")).closest(".settings-field");
    expect(auditCard?.textContent).toContain("3 条近期审计记录");
    expect(auditCard?.textContent).not.toContain("未设置");
  });

  it("renders a status object as its status instead of a JSON blob", async () => {
    const user = userEvent.setup();
    renderSettings({
      entries: [entry("policy.status.import_diff", "readonly", { scope: "policy_status" })],
      runtime: {
        ...runtime(),
        values: { "policy.status.import_diff": { status: "matches", message: "long backend message" } }
      }
    });

    await user.click(await screen.findByRole("button", { name: /^策略字段/ }));

    expect(await screen.findByText("一致")).toBeTruthy();
    expect(screen.queryByText(/long backend message/)).toBeNull();
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
    entry("storage.sqlite_path", "diagnostic"),
    // Policy-owned: normal visibility, but the Policy screen owns the write path.
    entry("policy.global.p2p_auto_reply", "normal", { source: "product_policy_store", scope: "global_policy", editable_v1: true })
  ];
}

function entry(
  key: string,
  visibility: string,
  overrides: Partial<SettingsCatalogEntry> = {}
): SettingsCatalogEntry {
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
    write_boundary: null,
    ...overrides
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
