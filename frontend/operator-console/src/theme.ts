import { useEffect, useState } from "react";

export type ThemePreference = "system" | "light" | "dark";

const STORAGE_KEY = "feishu_shadow_agent_console_theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

function initialPreference(): ThemePreference {
  const value = document.documentElement.dataset.themePreference;
  return value === "light" || value === "dark" ? value : "system";
}

export function useThemePreference(): [ThemePreference, (preference: ThemePreference) => void] {
  const [preference, setPreference] = useState<ThemePreference>(initialPreference);

  useEffect(() => {
    const media = window.matchMedia(DARK_QUERY);
    const update = () => {
      document.documentElement.dataset.theme = preference === "system"
        ? (media.matches ? "dark" : "light")
        : preference;
    };
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [preference]);

  function chooseTheme(next: ThemePreference): void {
    setPreference(next);
    document.documentElement.dataset.themePreference = next;
    document.documentElement.dataset.theme = next === "system"
      ? (window.matchMedia(DARK_QUERY).matches ? "dark" : "light")
      : next;
    try {
      if (next === "system") {
        localStorage.removeItem(STORAGE_KEY);
      } else {
        localStorage.setItem(STORAGE_KEY, next);
      }
    } catch {
      // A storage failure must not block the current appearance change.
    }
  }

  return [preference, chooseTheme];
}
