import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { Activity, Bell, ClipboardList, Database, FileText, HeartPulse, Home, Languages, MessageSquareDiff, Send, Settings, ShieldCheck, SunMoon, Wrench } from "lucide-react";
import { getDashboard } from "./api";
import { Badge, EmptyState } from "./components/Primitives";
import { bootstrapTokenFromHash, decodeHashSegment } from "./consoleSession";
import { normalizeLanguage, setConsoleLanguage, type ConsoleLanguage } from "./i18n";
import { queryKeys } from "./queryKeys";
import { useThemePreference, type ThemePreference } from "./theme";
import { ApprovalsScreen } from "./screens/ApprovalsScreen";
import { DashboardScreen } from "./screens/DashboardScreen";
import { DispatchScreen } from "./screens/DispatchScreen";
import { FeedbackScreen } from "./screens/FeedbackScreen";
import { HealthScreen } from "./screens/HealthScreen";
import { MaintenanceScreen } from "./screens/MaintenanceScreen";
import { PolicyScreen } from "./screens/PolicyScreen";
import { SettingsScreen } from "./screens/SettingsScreen";
import { TasksScreen } from "./screens/TasksScreen";
import type { DashboardSnapshot, RouteKey, Tone } from "./types";

const TOKEN_STORAGE_KEY = "feishu_shadow_agent_console_token";

const navItems: Array<{ key: RouteKey; labelKey: string; icon: typeof Home }> = [
  { key: "dashboard", labelKey: "navigation.dashboard", icon: Home },
  { key: "approvals", labelKey: "navigation.approvals", icon: Bell },
  { key: "tasks", labelKey: "navigation.tasks", icon: ClipboardList },
  { key: "dispatch", labelKey: "navigation.dispatch", icon: Send },
  { key: "feedback", labelKey: "navigation.feedback", icon: MessageSquareDiff },
  { key: "policy", labelKey: "navigation.policy", icon: ShieldCheck },
  { key: "settings", labelKey: "navigation.settings", icon: Settings },
  { key: "health", labelKey: "navigation.health", icon: HeartPulse },
  { key: "maintenance", labelKey: "navigation.maintenance", icon: Wrench }
];

export function App() {
  const { i18n, t } = useTranslation();
  const [token, setToken] = useState(() => sessionStorage.getItem(TOKEN_STORAGE_KEY) ?? "");
  const [location, setLocation] = useState(() => currentLocation());
  const [themePreference, setThemePreference] = useThemePreference();
  const dashboard = useQuery({
    queryKey: queryKeys.dashboard(),
    queryFn: () => getDashboard(token),
    enabled: Boolean(token),
    refetchInterval: 15_000
  });

  useEffect(() => {
    const url = new URL(window.location.href);
    const fragmentToken = bootstrapTokenFromHash(url.hash);
    const hadQueryToken = url.searchParams.has("token");
    url.searchParams.delete("token");
    if (fragmentToken) {
      sessionStorage.setItem(TOKEN_STORAGE_KEY, fragmentToken);
      setToken(fragmentToken);
    }
    if (fragmentToken || hadQueryToken) {
      window.history.replaceState(null, "", `${url.pathname}${url.search}#dashboard`);
    }
  }, []);

  useEffect(() => {
    document.title = t("app.title");
  }, [i18n.resolvedLanguage, t]);

  useEffect(() => {
    const handleHash = () => setLocation(currentLocation());
    window.addEventListener("hashchange", handleHash);
    return () => window.removeEventListener("hashchange", handleHash);
  }, []);

  const runtimeStatus = useMemo(
    () => runtimeStripStatus(t, dashboard.data, dashboard.error, dashboard.dataUpdatedAt),
    [dashboard.data, dashboard.dataUpdatedAt, dashboard.error, t]
  );

  function navigate(route: RouteKey, selectedId?: string, view?: "attention" | "all") {
    const path = selectedId ? `${route}/${encodeURIComponent(selectedId)}` : route;
    window.location.hash = view ? `${path}?view=${view}` : path;
  }

  return (
    <div className="app-shell">
      <aside className="side-nav" aria-label={t("app.mainNavigation")}>
        <div className="brand-lockup">
          <Database aria-hidden="true" size={18} />
          <div>
            <span className="brand-title">Shadow Agent</span>
            <span className="brand-subtitle">{t("app.brandSubtitle")}</span>
          </div>
        </div>
        <nav className="nav-list">
          {navItems.map((item) => (
            <a
              aria-current={location.route === item.key ? "page" : undefined}
              className="nav-item"
              href={`#${item.key}`}
              key={item.key}
              title={t(item.labelKey)}
            >
              <item.icon aria-hidden="true" size={17} />
              <span>{t(item.labelKey)}</span>
            </a>
          ))}
        </nav>
      </aside>

      <div className="workspace">
        <RuntimeStrip
          language={normalizeLanguage(i18n.resolvedLanguage)}
          onLanguageChange={(language) => void setConsoleLanguage(language)}
          onThemeChange={setThemePreference}
          preference={themePreference}
          status={runtimeStatus}
        />
        <main className="main-surface">
          {!token ? (
            <EmptyState title={t("app.missingToken")} detail={t("app.missingTokenDetail")} />
          ) : location.route === "dashboard" ? (
            <DashboardScreen navigate={navigate} token={token} />
          ) : location.route === "approvals" ? (
            <ApprovalsScreen selectedId={location.selectedId} token={token} />
          ) : location.route === "tasks" ? (
            <TasksScreen initialFilter={location.view === "all" ? "all" : undefined} selectedId={location.selectedId} token={token} />
          ) : location.route === "dispatch" ? (
            <DispatchScreen initialFilter={location.view === "attention" ? "attention" : undefined} selectedId={location.selectedId} token={token} />
          ) : location.route === "feedback" ? (
            <FeedbackScreen token={token} />
          ) : location.route === "policy" ? (
            <PolicyScreen selectedId={location.selectedId} token={token} />
          ) : location.route === "settings" ? (
            <SettingsScreen token={token} />
          ) : location.route === "health" ? (
            <HealthScreen token={token} />
          ) : location.route === "maintenance" ? (
            <MaintenanceScreen token={token} />
          ) : (
            <FollowUpScreen route={location.route} />
          )}
        </main>
      </div>
    </div>
  );
}

function RuntimeStrip({
  status,
  language,
  onLanguageChange,
  preference,
  onThemeChange
}: {
  status: Array<{ label: string; value: string; tone: Tone }>;
  language: ConsoleLanguage;
  onLanguageChange: (language: ConsoleLanguage) => void;
  preference: ThemePreference;
  onThemeChange: (preference: ThemePreference) => void;
}) {
  const { t } = useTranslation();
  return (
    <header className="runtime-strip">
      <div className="runtime-heading">
        <Activity aria-hidden="true" size={16} />
        <span>{t("app.runtime")}</span>
      </div>
      <div className="runtime-items">
        {status.map((item) => (
          <div className="runtime-item" key={item.label}>
            <span className="runtime-label">{item.label}</span>
            <Badge tone={item.tone}>{item.value}</Badge>
          </div>
        ))}
      </div>
      <label className="theme-control locale-control">
        <Languages aria-hidden="true" size={16} />
        <span>{t("language.label")}</span>
        <select aria-label={t("language.label")} onChange={(event) => onLanguageChange(event.target.value as ConsoleLanguage)} value={language}>
          <option value="zh-CN">{t("language.chinese")}</option>
          <option value="en-US">{t("language.english")}</option>
        </select>
      </label>
      <label className="theme-control">
        <SunMoon aria-hidden="true" size={16} />
        <span>{t("app.appearance")}</span>
        <select aria-label={t("app.appearanceLabel")} onChange={(event) => onThemeChange(event.target.value as ThemePreference)} value={preference}>
          <option value="system">{t("app.themeSystem")}</option>
          <option value="light">{t("app.themeLight")}</option>
          <option value="dark">{t("app.themeDark")}</option>
        </select>
      </label>
    </header>
  );
}

function FollowUpScreen({ route }: { route: RouteKey }) {
  const { t } = useTranslation();
  const label = t(navItems.find((item) => item.key === route)?.labelKey ?? "navigation.console");
  return (
    <section className="work-grid" aria-label={label}>
      <div className="queue-panel">
        <div className="section-header">
          <div>
            <p className="eyebrow">{t("app.featureState")}</p>
            <h1>{label}</h1>
          </div>
          <Badge tone="muted">{t("app.unavailable")}</Badge>
        </div>
        <div className="quiet-empty">
          <FileText aria-hidden="true" size={18} />
          <span>{t("app.noWorkflow")}</span>
        </div>
      </div>
    </section>
  );
}

function currentLocation(): { route: RouteKey; selectedId: string | null; view: string | null } {
  const hash = window.location.hash.replace("#", "");
  const [path, query] = hash.split("?");
  const [routeText, selectedId] = path.split("/");
  const route = navItems.some((item) => item.key === routeText) ? (routeText as RouteKey) : "dashboard";
  return {
    route,
    selectedId: decodeHashSegment(selectedId),
    view: new URLSearchParams(query).get("view")
  };
}

function runtimeStripStatus(t: TFunction, snapshot: DashboardSnapshot | undefined, error: unknown, updatedAt: number) {
  const daemon = String(snapshot?.daemon_liveness?.status ?? "unknown");
  const initialized = snapshot?.policy_status?.initialized;
  const importDiff = snapshot?.policy_status?.policy_import_diff?.status ?? "unknown";
  const dataStale = updatedAt > 0 && Date.now() - updatedAt > 30_000;
  return [
    { label: t("runtime.daemon"), value: daemon === "live" ? t("runtime.running") : daemon === "unknown" ? t("common.unknown") : daemon, tone: daemon === "live" ? "success" : daemon === "unknown" ? "muted" : "warning" },
    { label: t("runtime.policy"), value: initialized ? t("runtime.initialized") : t("runtime.missing"), tone: initialized ? "success" : "warning" },
    { label: t("runtime.importDiff"), value: importDiff === "matches" ? t("runtime.matches") : importDiff === "differs" ? t("runtime.differs") : t("common.unknown"), tone: importDiff === "matches" ? "success" : "info" },
    { label: t("runtime.data"), value: error ? t("runtime.readFailed") : dataStale ? t("runtime.stale") : updatedAt ? t("runtime.latest") : t("runtime.waiting"), tone: error ? "danger" : dataStale ? "warning" : updatedAt ? "success" : "muted" }
  ] satisfies Array<{ label: string; value: string; tone: Tone }>;
}
