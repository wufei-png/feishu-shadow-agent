import type { TFunction } from "i18next";
import { currentLanguage } from "./i18n";
import type { CatalogTranslation } from "./locales/catalog-zh";
import { enumLabel } from "./presentation";
import type { SettingsCatalogEntry } from "./types";

/**
 * Resolves the operator-facing copy for a catalog field.
 *
 * Chinese copy is a frontend override; the API's English copy is the source.
 * The override is scoped to Chinese explicitly, because `fallbackLng: "zh-CN"`
 * would otherwise resolve a key that is missing from the English bundle to the
 * Chinese string -- which left every field label and description in Chinese
 * during an English session.
 */
export function catalogText(t: TFunction, entry: SettingsCatalogEntry, field: keyof CatalogTranslation): string {
  if (currentLanguage() !== "zh-CN") {
    return entry[field] ?? "";
  }
  return t(catalogResourceKey(entry.key, field), { defaultValue: entry[field] ?? "" });
}

function catalogResourceKey(entryKey: string, field: keyof CatalogTranslation): string {
  return `catalog.${entryKey}.${field}`;
}

/**
 * Renders a catalog value for display. Settings shows these read-only while
 * Policy shows the same values as edit targets and in before/after diffs, so the
 * formatting has to live in one place or the two screens drift apart on what a
 * value looks like.
 */
export function formatSettingValue(t: TFunction, value: unknown): string {
  if (value === true) {
    return t("common.yes");
  }
  if (value === false) {
    return t("common.no");
  }
  if (value === null || value === undefined || value === "") {
    return t("common.notSet");
  }
  if (Array.isArray(value)) {
    return value.length ? value.join(", ") : t("common.none");
  }
  if (typeof value === "object") {
    // A status object reads as its status. Serializing it puts a wall of JSON in
    // a slot the operator scans, which is worse than useless.
    const status = (value as { status?: unknown }).status;
    if (typeof status === "string") {
      return enumLabel(t, "status", status);
    }
    return JSON.stringify(value);
  }
  return String(value);
}
