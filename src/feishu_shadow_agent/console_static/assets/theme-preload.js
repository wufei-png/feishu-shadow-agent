(() => {
  const root = globalThis.document.documentElement;
  let preference = "system";
  try {
    const saved = globalThis.localStorage.getItem("feishu_shadow_agent_console_theme");
    if (saved === "light" || saved === "dark") preference = saved;
  } catch {
    // Private browsing may block storage; the system theme still works.
  }
  root.dataset.themePreference = preference;
  root.dataset.theme = preference === "system"
    ? (globalThis.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
    : preference;
})();
