// @vitest-environment jsdom

import { TooltipProvider } from "@radix-ui/react-tooltip";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { HelpTooltip } from "./Primitives";

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
});
