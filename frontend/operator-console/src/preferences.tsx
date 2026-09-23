import { createContext, useContext, useMemo } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { normalizeLanguage, setConsoleLanguage, type ConsoleLanguage } from "./i18n";
import { useThemePreference, type ThemePreference } from "./theme";

/**
 * Console preferences are the operator's own settings, not runtime state. They
 * live here rather than in the runtime strip so the shell can report on the
 * machine without also carrying controls, and so Settings owns every preference
 * the console stores locally.
 */
export type ConsolePreferences = {
  theme: ThemePreference;
  setTheme: (preference: ThemePreference) => void;
  language: ConsoleLanguage;
  setLanguage: (language: ConsoleLanguage) => void;
};

const PreferencesContext = createContext<ConsolePreferences | null>(null);

export function PreferencesProvider({ children }: { children: ReactNode }) {
  const { i18n } = useTranslation();
  const [theme, setTheme] = useThemePreference();
  const language = normalizeLanguage(i18n.resolvedLanguage);

  const value = useMemo<ConsolePreferences>(
    () => ({
      theme,
      setTheme,
      language,
      setLanguage: (next) => void setConsoleLanguage(next)
    }),
    [language, setTheme, theme]
  );

  return <PreferencesContext.Provider value={value}>{children}</PreferencesContext.Provider>;
}

export function usePreferences(): ConsolePreferences {
  const value = useContext(PreferencesContext);
  if (!value) {
    throw new Error("usePreferences must be used inside a PreferencesProvider");
  }
  return value;
}
