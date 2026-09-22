// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useThemePreference } from "./theme";

function mockColorScheme(initialDark: boolean) {
  const listeners = new Set<() => void>();
  const media = {
    matches: initialDark,
    addEventListener: (_event: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_event: string, listener: () => void) => listeners.delete(listener)
  };
  vi.stubGlobal("matchMedia", () => media);
  return {
    change(dark: boolean) {
      media.matches = dark;
      listeners.forEach((listener) => listener());
    }
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  delete document.documentElement.dataset.theme;
  delete document.documentElement.dataset.themePreference;
});

describe("appearance preference", () => {
  it("follows system changes until the operator picks a theme, then remembers the choice", () => {
    const system = mockColorScheme(false);
    document.documentElement.dataset.themePreference = "system";
    const { result } = renderHook(() => useThemePreference());
    expect(document.documentElement.dataset.theme).toBe("light");

    act(() => system.change(true));
    expect(document.documentElement.dataset.theme).toBe("dark");

    act(() => result.current[1]("light"));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem("feishu_shadow_agent_console_theme")).toBe("light");

    act(() => system.change(false));
    act(() => system.change(true));
    expect(document.documentElement.dataset.theme).toBe("light");

    act(() => result.current[1]("system"));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("feishu_shadow_agent_console_theme")).toBeNull();
  });
});
