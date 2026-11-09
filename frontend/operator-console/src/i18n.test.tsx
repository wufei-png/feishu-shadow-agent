// @vitest-environment jsdom

import { act, cleanup, render, screen } from "@testing-library/react";
import { useTranslation } from "react-i18next";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import i18n, {
  CONSOLE_LANGUAGE_STORAGE_KEY,
  formatDateTime,
  normalizeLanguage,
  setConsoleLanguage
} from "./i18n";
import { catalogText } from "./catalogPresentation";
import { commandLabel, enumLabel } from "./presentation";
import type { SettingsCatalogEntry } from "./types";

function LanguageProbe() {
  const { t } = useTranslation();
  return <p>{t("language.label")}</p>;
}

beforeEach(async () => {
  localStorage.clear();
  await setConsoleLanguage("zh-CN");
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("console localization", () => {
  it("switches resources, updates the document language and remembers the choice", async () => {
    render(<LanguageProbe />);
    expect(screen.getByText("语言")).toBeTruthy();

    await act(() => setConsoleLanguage("en-US"));

    expect(screen.getByText("Language")).toBeTruthy();
    expect(document.documentElement.lang).toBe("en-US");
    expect(localStorage.getItem(CONSOLE_LANGUAGE_STORAGE_KEY)).toBe("en-US");
  });

  it("normalizes supported language families and formats dates with the selected locale", () => {
    expect(normalizeLanguage("en-GB")).toBe("en-US");
    expect(normalizeLanguage("zh-Hans")).toBe("zh-CN");
    const value = "2026-09-23T00:00:00Z";
    expect(formatDateTime(value, "en-US")).toBe(
      new Intl.DateTimeFormat("en-US", {
        year: "numeric",
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit"
      }).format(new Date(value))
    );
  });

  it("localizes command summaries and falls back for unknown enums", async () => {
    expect(commandLabel(i18n.t, "policy.update_global")).toBe("更新全局策略");
    expect(enumLabel(i18n.t, "category", "approval_command")).toBe("审批操作");
    expect(enumLabel(i18n.t, "status", "schema_uninitialized")).toBe("尚未初始化");
    expect(enumLabel(i18n.t, "status", "future_status")).toBe("未知");

    await setConsoleLanguage("en-US");

    expect(commandLabel(i18n.t, "policy.update_global")).toBe("Update global policy");
    expect(enumLabel(i18n.t, "status", "future_status")).toBe("Unknown");
  });

  it("resolves catalog copy in the active language instead of falling back to Chinese", async () => {
    // The Chinese catalog copy is a frontend override of the API's English source.
    // It used to be reachable from English through `fallbackLng`, which left every
    // field label and description in Chinese during an English session.
    const entry: SettingsCatalogEntry = {
      key: "lifecycle.watch_minutes",
      label: "Watch window",
      description: "Minutes a task remains active after activity.",
      help: null,
      source: "config_yaml",
      scope: "runtime",
      visibility: "normal",
      editable_v1: false,
      requires_restart: false,
      audit_behavior: "read only",
      write_boundary: null
    };

    expect(catalogText(i18n.t, entry, "label")).toBe("任务观察窗口");
    expect(catalogText(i18n.t, entry, "description")).toBe("任务在最近活动后继续保持活跃的分钟数。");

    await setConsoleLanguage("en-US");

    expect(catalogText(i18n.t, entry, "label")).toBe("Watch window");
    expect(catalogText(i18n.t, entry, "description")).toBe("Minutes a task remains active after activity.");
  });

  it("keeps the language bundles symmetric outside the catalog override", async () => {
    const { copyFor } = await import("./locales/copy");
    const zh = Object.keys(copyFor(0));
    const en = Object.keys(copyFor(1));
    expect(zh.filter((key) => !en.includes(key))).toEqual([]);
    expect(en.filter((key) => !zh.includes(key))).toEqual([]);
  });
});
