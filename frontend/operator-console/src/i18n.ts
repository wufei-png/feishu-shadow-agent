import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import { enUS } from "./locales/en-US";
import { zhCN } from "./locales/zh-CN";

export type ConsoleLanguage = "zh-CN" | "en-US";

export const CONSOLE_LANGUAGE_STORAGE_KEY = "feishu_shadow_agent_console_language";
export const supportedLanguages: ConsoleLanguage[] = ["zh-CN", "en-US"];

export function normalizeLanguage(value: string | null | undefined): ConsoleLanguage {
  return value?.toLowerCase().startsWith("en") ? "en-US" : "zh-CN";
}

function initialLanguage(): ConsoleLanguage {
  try {
    const stored = localStorage.getItem(CONSOLE_LANGUAGE_STORAGE_KEY);
    return supportedLanguages.includes(stored as ConsoleLanguage)
      ? (stored as ConsoleLanguage)
      : "zh-CN";
  } catch {
    return "zh-CN";
  }
}

const language = initialLanguage();

void i18n.use(initReactI18next).init({
  resources: {
    "zh-CN": { translation: zhCN },
    "en-US": { translation: enUS }
  },
  lng: language,
  fallbackLng: "zh-CN",
  supportedLngs: supportedLanguages,
  interpolation: { escapeValue: false },
  returnNull: false
});

document.documentElement.lang = language;

export async function setConsoleLanguage(language: ConsoleLanguage): Promise<void> {
  localStorage.setItem(CONSOLE_LANGUAGE_STORAGE_KEY, language);
  document.documentElement.lang = language;
  await i18n.changeLanguage(language);
}

export function currentLanguage(): ConsoleLanguage {
  return normalizeLanguage(i18n.resolvedLanguage ?? i18n.language);
}

export function formatDateTime(value: string | Date | number, language = currentLanguage()): string {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return new Intl.DateTimeFormat(language, {
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  }).format(date);
}

export function formatNumber(value: number, language = currentLanguage()): string {
  return new Intl.NumberFormat(language).format(value);
}

export default i18n;
