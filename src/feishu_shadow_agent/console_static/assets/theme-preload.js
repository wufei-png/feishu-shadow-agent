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
})();
