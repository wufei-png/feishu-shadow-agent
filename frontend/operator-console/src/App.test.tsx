// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
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
  it("preserves the requested list view in the URL and destination", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><App /></QueryClientProvider>);

    await user.click(screen.getByRole("button", { name: "查看发送待办" }));
    expect(await screen.findByText("发送筛选：attention")).toBeTruthy();
    expect(window.location.hash).toBe("#dispatch?view=attention");

    await user.click(screen.getByRole("link", { name: "待我处理" }));
    await user.click(await screen.findByRole("button", { name: "查看任务待办" }));
    expect(await screen.findByText("任务筛选：all")).toBeTruthy();
    expect(window.location.hash).toBe("#tasks?view=all");
  });

  it("switches the application shell to English without reloading", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><App /></QueryClientProvider>);

    await user.selectOptions(screen.getByRole("combobox", { name: "语言" }), "en-US");

    expect(await screen.findByRole("link", { name: "Approvals" })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "Language" })).toBeTruthy();
    expect(document.documentElement.lang).toBe("en-US");
  });
});
