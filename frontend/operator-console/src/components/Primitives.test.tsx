// @vitest-environment jsdom

import { TooltipProvider } from "@radix-ui/react-tooltip";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CopyValue, HelpTooltip, QueueControls } from "./Primitives";

afterEach(cleanup);

describe("HelpTooltip", () => {
  it("exposes a keyboard-focusable help trigger", () => {
    render(
      <TooltipProvider delayDuration={0}>
        <HelpTooltip label="查看帮助">仅在优先机器人时生效。</HelpTooltip>
      </TooltipProvider>
    );

    const trigger = screen.getByRole("button", { name: "查看帮助" });
    expect(trigger.getAttribute("type")).toBe("button");
    expect(trigger.tabIndex).toBe(0);
  });

  it("copies technical identifiers and announces success", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText");
    render(<CopyValue label="任务 ID" value="task_123" />);

    await user.click(screen.getByRole("button", { name: "复制任务 ID" }));

    expect(writeText).toHaveBeenCalledWith("task_123");
    expect(screen.getByRole("button", { name: "已复制任务 ID" })).toBeTruthy();
  });
});

it("shows pagination when there is another page in either direction", async () => {
  const user = userEvent.setup();
  const onRefresh = vi.fn();
  const onPrevious = vi.fn();
  const onNext = vi.fn();
  const paging = { page: 0, hasNext: false, onPrevious, onNext };
  const props = { error: null, isFetching: false, updatedAt: 1, onRefresh };
  const { rerender } = render(<QueueControls {...props} pagination={paging} />);

  expect(screen.queryByRole("button", { name: "上一页" })).toBeNull();
  expect(screen.queryByRole("button", { name: "下一页" })).toBeNull();
  expect(screen.getByText(/^更新于/)).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "刷新" }));
  expect(onRefresh).toHaveBeenCalledOnce();

  rerender(<QueueControls {...props} pagination={{ ...paging, hasNext: true }} />);
  await user.click(screen.getByRole("button", { name: "下一页" }));
  expect(onNext).toHaveBeenCalledOnce();

  rerender(<QueueControls {...props} pagination={{ ...paging, page: 1 }} />);
  await user.click(screen.getByRole("button", { name: "上一页" }));
  expect(onPrevious).toHaveBeenCalledOnce();
});
