(() => {
  const root = globalThis.document.documentElement;
  let preference = "system";
  let material = "glass";
  try {
    const saved = globalThis.localStorage.getItem("feishu_shadow_agent_console_theme");
    if (saved === "light" || saved === "dark") preference = saved;
    if (globalThis.localStorage.getItem("feishu_shadow_agent_console_material") === "solid") material = "solid";
  } catch {
    // Private browsing may block storage; the system theme still works.
  }
  root.dataset.themePreference = preference;
  root.dataset.material = material;
  root.dataset.theme = preference === "system"
    ? (globalThis.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
    : preference;

  try {
    const glass = JSON.parse(globalThis.localStorage.getItem("feishu_shadow_agent_console_glass"));
    if (typeof glass?.color === "string" && /^#[\da-f]{6}$/i.test(glass.color)) {
      root.style.setProperty("--glass-color", glass.color.toLowerCase());
    }
    if (Number.isFinite(glass?.transparency)) {
      const transparency = Math.min(90, Math.max(45, Math.round(glass.transparency)));
      root.style.setProperty("--glass-transparency", `${transparency}%`);
    }
  } catch {
    // The stylesheet supplies the default tint and transparency.
  }
})();
