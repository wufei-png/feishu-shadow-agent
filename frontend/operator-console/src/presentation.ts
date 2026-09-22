import type { TFunction } from "i18next";

function keyPart(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

export function enumLabel(
  t: TFunction,
  group: "status" | "kind" | "stage" | "identity" | "role" | "source" | "action" | "postprocess",
  value: string | null | undefined,
  fallback?: string
): string {
  if (!value) {
    return fallback ?? t("common.notRecorded");
  }
  return t(`enums.${group}.${keyPart(value)}`, { defaultValue: fallback ?? value });
}

export function commandLabel(t: TFunction, value: string): string {
  return t(`commands.${keyPart(value)}`, { defaultValue: value });
}
