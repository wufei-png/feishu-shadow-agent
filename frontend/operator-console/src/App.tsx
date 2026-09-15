import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, Bell, ClipboardList, Database, FileText, HeartPulse, Home, MessageSquareDiff, Send, Settings, ShieldCheck, Wrench } from "lucide-react";
import { getDashboard } from "./api";
import { Badge, EmptyState } from "./components/Primitives";
import { bootstrapTokenFromHash, decodeHashSegment } from "./consoleSession";
import { queryKeys } from "./queryKeys";
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

const navItems: Array<{ key: RouteKey; label: string; icon: typeof Home }> = [
  { key: "dashboard", label: "待我处理", icon: Home },
  { key: "approvals", label: "审批", icon: Bell },
  { key: "tasks", label: "任务", icon: ClipboardList },
  { key: "dispatch", label: "发送", icon: Send },
  { key: "feedback", label: "反馈", icon: MessageSquareDiff },
  { key: "policy", label: "策略", icon: ShieldCheck },
  { key: "settings", label: "设置", icon: Settings },
  { key: "health", label: "健康", icon: HeartPulse },
  { key: "maintenance", label: "维护", icon: Wrench }
];

export function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem(TOKEN_STORAGE_KEY) ?? "");
  const [location, setLocation] = useState(() => currentLocation());
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
    const handleHash = () => setLocation(currentLocation());
    window.addEventListener("hashchange", handleHash);
    return () => window.removeEventListener("hashchange", handleHash);
  }, []);

  const runtimeStatus = useMemo(
    () => runtimeStripStatus(dashboard.data, dashboard.error, dashboard.dataUpdatedAt),
    [dashboard.data, dashboard.dataUpdatedAt, dashboard.error]
  );

  function navigate(route: RouteKey, selectedId?: string) {
    window.location.hash = selectedId ? `${route}/${encodeURIComponent(selectedId)}` : route;
  }

  return (
    <div className="app-shell">
      <aside className="side-nav" aria-label="Primary navigation">
        <div className="brand-lockup">
          <Database aria-hidden="true" size={18} />
          <div>
            <span className="brand-title">Shadow Agent</span>
            <span className="brand-subtitle">Operator Console</span>
          </div>
        </div>
        <nav className="nav-list">
          {navItems.map((item) => (
            <a
              aria-current={location.route === item.key ? "page" : undefined}
              className="nav-item"
              href={`#${item.key}`}
              key={item.key}
              title={item.label}
            >
              <item.icon aria-hidden="true" size={17} />
              <span>{item.label}</span>
            </a>
          ))}
        </nav>
      </aside>

      <div className="workspace">
        <RuntimeStrip status={runtimeStatus} />
        <main className="main-surface">
          {!token ? (
            <EmptyState title="Session token required" detail="Open the local console URL printed by the server." />
          ) : location.route === "dashboard" ? (
            <DashboardScreen navigate={navigate} token={token} />
          ) : location.route === "approvals" ? (
            <ApprovalsScreen selectedId={location.selectedId} token={token} />
          ) : location.route === "tasks" ? (
            <TasksScreen selectedId={location.selectedId} token={token} />
          ) : location.route === "dispatch" ? (
            <DispatchScreen selectedId={location.selectedId} token={token} />
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

function RuntimeStrip({ status }: { status: Array<{ label: string; value: string; tone: Tone }> }) {
  return (
    <header className="runtime-strip">
      <div className="runtime-heading">
        <Activity aria-hidden="true" size={16} />
        <span>Runtime</span>
      </div>
      <div className="runtime-items">
        {status.map((item) => (
          <div className="runtime-item" key={item.label}>
            <span className="runtime-label">{item.label}</span>
            <Badge tone={item.tone}>{item.value}</Badge>
          </div>
        ))}
      </div>
    </header>
  );
}

function FollowUpScreen({ route }: { route: RouteKey }) {
  const label = navItems.find((item) => item.key === route)?.label ?? "Console";
  return (
    <section className="work-grid" aria-label={label}>
      <div className="queue-panel">
        <div className="section-header">
          <div>
            <p className="eyebrow">Follow-up phase</p>
            <h1>{label}</h1>
          </div>
          <Badge tone="muted">Unavailable</Badge>
        </div>
        <div className="quiet-empty">
          <FileText aria-hidden="true" size={18} />
          <span>No active workflow available.</span>
        </div>
      </div>
    </section>
  );
}

function currentLocation(): { route: RouteKey; selectedId: string | null } {
  const hash = window.location.hash.replace("#", "");
  const [routeText, selectedId] = hash.split("/");
  const route = navItems.some((item) => item.key === routeText) ? (routeText as RouteKey) : "dashboard";
  return {
    route,
    selectedId: decodeHashSegment(selectedId)
  };
}

function runtimeStripStatus(snapshot: DashboardSnapshot | undefined, error: unknown, updatedAt: number) {
  const daemon = String(snapshot?.daemon_liveness?.status ?? "unknown");
  const initialized = snapshot?.policy_status?.initialized;
  const importDiff = snapshot?.policy_status?.policy_import_diff?.status ?? "unknown";
  const dataStale = updatedAt > 0 && Date.now() - updatedAt > 30_000;
  return [
    { label: "Daemon", value: daemon, tone: daemon === "live" ? "success" : daemon === "unknown" ? "muted" : "warning" },
    { label: "策略", value: initialized ? "已初始化" : "缺失", tone: initialized ? "success" : "warning" },
    { label: "导入差异", value: importDiff, tone: importDiff === "matches" ? "success" : "info" },
    { label: "数据", value: error ? "读取失败" : dataStale ? "缓存过期" : updatedAt ? "最新" : "等待中", tone: error ? "danger" : dataStale ? "warning" : updatedAt ? "success" : "muted" }
  ] satisfies Array<{ label: string; value: string; tone: Tone }>;
}
