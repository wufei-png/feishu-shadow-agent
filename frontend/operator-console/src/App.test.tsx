// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { getDashboard } from "./api";
import { setConsoleLanguage } from "./i18n";

vi.mock("./api", () => ({ getDashboard: vi.fn().mockResolvedValue({}) }));
vi.mock("./screens/DashboardScreen", () => ({
  DashboardScreen: ({ navigate }: { navigate: (route: "dispatch" | "tasks", selectedId?: string, view?: "attention" | "all") => void }) => (
    <>
      <button onClick={() => navigate("dispatch", undefined, "attention")}>查看发送待办</button>
      <button onClick={() => navigate("tasks", undefined, "all")}>查看任务待办</button>
    </>
  )
}));
vi.mock("./screens/DispatchScreen", () => ({
  DispatchScreen: ({ initialFilter }: { initialFilter?: string }) => <p>发送筛选：{initialFilter ?? "默认"}</p>
}));
vi.mock("./screens/TasksScreen", () => ({
  TasksScreen: ({ initialFilter }: { initialFilter?: string }) => <p>任务筛选：{initialFilter ?? "默认"}</p>
}));

beforeEach(async () => {
  await setConsoleLanguage("zh-CN");
  sessionStorage.setItem("feishu_shadow_agent_console_token", "token");
  window.location.hash = "#dashboard";
  vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({
    matches: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn()
  }));
});

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  window.location.hash = "";
  vi.unstubAllGlobals();
});

describe("dashboard attention navigation", () => {
  it("identifies the isolated dry-run console", async () => {
    vi.mocked(getDashboard).mockResolvedValueOnce({ runtime_mode: "dry_run" });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><App /></QueryClientProvider>);

    expect(await screen.findByText("本地预演")).toBeTruthy();
  });

  it("preserves the requested list view in the URL and destination", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><App /></QueryClientProvider>);

    // Screens are code-split, so the first visit resolves asynchronously.
    await user.click(await screen.findByRole("button", { name: "查看发送待办" }));
    expect(await screen.findByText("发送筛选：attention")).toBeTruthy();
    expect(window.location.hash).toBe("#dispatch?view=attention");

    await user.click(screen.getByRole("link", { name: "待我处理" }));
    await user.click(await screen.findByRole("button", { name: "查看任务待办" }));
    expect(await screen.findByText("任务筛选：all")).toBeTruthy();
    expect(window.location.hash).toBe("#tasks?view=all");
  });

  it("groups navigation into duty, configure, and system sections", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><App /></QueryClientProvider>);

    expect(await screen.findByRole("link", { name: "待我处理" })).toBeTruthy();
    expect(screen.getByText("值守")).toBeTruthy();
    expect(screen.getByText("配置")).toBeTruthy();
    expect(screen.getByText("系统")).toBeTruthy();
  });

  it("keeps operator preferences out of the shell so the runtime strip only reports", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><App /></QueryClientProvider>);

    expect(await screen.findByText("运行状态")).toBeTruthy();
    expect(screen.queryAllByRole("combobox")).toHaveLength(0);
  });
});
