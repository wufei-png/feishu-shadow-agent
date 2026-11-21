import { useEffect, useState } from "react";

export type ThemePreference = "system" | "light" | "dark";
export type MaterialPreference = "glass" | "solid";
export type GlassPreference = { color: string; transparency: number };

export const GLASS_TRANSPARENCY = { min: 45, max: 90, default: 82 } as const;
export const GLASS_PALETTE = [
  { color: "#72b7cb", labelKey: "settings.appearance.paletteCyan" },
  { color: "#8ba9d4", labelKey: "settings.appearance.paletteBlue" },
  { color: "#94b3a0", labelKey: "settings.appearance.paletteSage" },
  { color: "#afa0cc", labelKey: "settings.appearance.paletteLilac" },
  { color: "#c9aa88", labelKey: "settings.appearance.paletteSand" },
  { color: "#a3abb9", labelKey: "settings.appearance.paletteSilver" }
] as const;

const STORAGE_KEY = "feishu_shadow_agent_console_theme";
const MATERIAL_STORAGE_KEY = "feishu_shadow_agent_console_material";
const GLASS_STORAGE_KEY = "feishu_shadow_agent_console_glass";
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

export function useMaterialPreference(): [MaterialPreference, (preference: MaterialPreference) => void] {
  const [material, setMaterial] = useState<MaterialPreference>(() =>
    document.documentElement.dataset.material === "solid" ? "solid" : "glass"
  );

  function chooseMaterial(next: MaterialPreference): void {
    setMaterial(next);
    document.documentElement.dataset.material = next;
    try {
      localStorage.setItem(MATERIAL_STORAGE_KEY, next);
    } catch {
      // The selected material still applies for this session.
    }
  }

  return [material, chooseMaterial];
}

export function useGlassPreference(): [GlassPreference, (preference: GlassPreference) => void] {
  const [glass, setGlass] = useState<GlassPreference>(() => {
    const { style } = document.documentElement;
    return {
      color: style.getPropertyValue("--glass-color") || GLASS_PALETTE[0].color,
      transparency: parseInt(style.getPropertyValue("--glass-transparency"), 10) || GLASS_TRANSPARENCY.default
    };
  });

  function chooseGlass(next: GlassPreference): void {
    const { style } = document.documentElement;
    style.setProperty("--glass-color", next.color);
    style.setProperty("--glass-transparency", `${next.transparency}%`);
    setGlass(next);
    try {
      localStorage.setItem(GLASS_STORAGE_KEY, JSON.stringify(next));
    } catch {
      // The selected appearance still applies for this session.
    }
  }

  return [glass, chooseGlass];
}
