// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import { DispatchScreen } from "./DispatchScreen";

vi.mock("../api", () => ({
  cancelDispatchAction: vi.fn(),
  getDispatchAction: vi.fn(),
  listDispatchActions: vi.fn(),
  markDispatchSent: vi.fn(),
  retryDispatchAction: vi.fn()
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DispatchScreen attention entry", () => {
  it("loads uncertain, failed and sending actions for the dashboard entry", async () => {
    vi.mocked(api.listDispatchActions).mockResolvedValue([]);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <DispatchScreen initialFilter="attention" selectedId={null} token="token" />
      </QueryClientProvider>
    );

    await screen.findByRole("heading", { name: "发送记录" });
    expect(api.listDispatchActions).toHaveBeenCalledWith("token", {
      status: ["failed_needs_review", "failed", "sending"],
      limit: 51,
      offset: 0
    });
  });
});
