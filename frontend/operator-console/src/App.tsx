import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Bell,
  ClipboardList,
  Database,
  FileText,
  HeartPulse,
  Home,
  RotateCcw,
  Send,
  Settings,
  ShieldCheck
} from "lucide-react";

const TOKEN_STORAGE_KEY = "feishu_shadow_agent_console_token";

type DashboardSnapshot = {
  daemon_liveness?: Record<string, unknown>;
  policy_status?: {
    initialized?: boolean;
    policy_import_diff?: {
      status?: string;
      message?: string;
    };
  };
  pending_approvals?: unknown[];
  failed_or_needs_review_actions?: unknown[];
  pending_actions?: unknown[];
  recent_health_warnings?: unknown[];
  last_run?: {
    last_tick_started_at?: string | null;
    last_tick_finished_at?: string | null;
  } | null;
};

type SettingsCatalog = {
  version: number;
  entries: Array<{
    key: string;
    label: string;
    source: string;
    visibility: string;
    editable_v1: boolean | string;
  }>;
};

type RouteKey = "dashboard" | "approvals" | "tasks" | "dispatch" | "policy" | "settings" | "health";

const navItems: Array<{ key: RouteKey; label: string; icon: typeof Home }> = [
  { key: "dashboard", label: "Dashboard", icon: Home },
  { key: "approvals", label: "Approvals", icon: Bell },
  { key: "tasks", label: "Tasks", icon: ClipboardList },
  { key: "dispatch", label: "Dispatch", icon: Send },
  { key: "policy", label: "Policy", icon: ShieldCheck },
  { key: "settings", label: "Settings", icon: Settings },
  { key: "health", label: "Logs / Health", icon: HeartPulse }
];

export function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem(TOKEN_STORAGE_KEY) ?? "");
  const [route, setRoute] = useState<RouteKey>(() => currentRoute());

  useEffect(() => {
    const url = new URL(window.location.href);
    const urlToken = url.searchParams.get("token");
    if (urlToken) {
      sessionStorage.setItem(TOKEN_STORAGE_KEY, urlToken);
      setToken(urlToken);
      url.searchParams.delete("token");
      window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash || "#dashboard"}`);
    }
  }, []);

  useEffect(() => {
    const handleHash = () => setRoute(currentRoute());
    window.addEventListener("hashchange", handleHash);
    return () => window.removeEventListener("hashchange", handleHash);
  }, []);

  const dashboard = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => fetchApi<DashboardSnapshot>("/api/dashboard", token),
    enabled: Boolean(token),
    refetchInterval: 15_000
  });

  const catalog = useQuery({
    queryKey: ["settings-catalog"],
    queryFn: () => fetchApi<SettingsCatalog>("/api/settings/catalog", token),
    enabled: Boolean(token) && route === "settings"
  });

  const runtimeStatus = useMemo(() => runtimeStripStatus(dashboard.data), [dashboard.data]);

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
              aria-current={route === item.key ? "page" : undefined}
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
          ) : route === "settings" ? (
            <SettingsRoute catalog={catalog.data} isLoading={catalog.isLoading} error={catalog.error} />
          ) : route === "dashboard" ? (
            <DashboardRoute snapshot={dashboard.data} isLoading={dashboard.isLoading} error={dashboard.error} />
          ) : (
            <FoundationRoute label={navItems.find((item) => item.key === route)?.label ?? "Console"} />
          )}
        </main>
      </div>
    </div>
  );
}

function RuntimeStrip({ status }: { status: Array<{ label: string; value: string; tone: string }> }) {
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
            <span className={`status-pill ${item.tone}`}>{item.value}</span>
          </div>
        ))}
      </div>
    </header>
  );
}

function DashboardRoute({
  snapshot,
  isLoading,
  error
}: {
  snapshot?: DashboardSnapshot;
  isLoading: boolean;
  error: Error | null;
}) {
  if (isLoading) {
    return <EmptyState title="Loading dashboard" detail="Reading local operator state." />;
  }
  if (error) {
    return <EmptyState title="Dashboard unavailable" detail={error.message} tone="danger" />;
  }
  const pendingApprovals = snapshot?.pending_approvals?.length ?? 0;
  const failedActions = snapshot?.failed_or_needs_review_actions?.length ?? 0;
  const pendingActions = snapshot?.pending_actions?.length ?? 0;
  const healthWarnings = snapshot?.recent_health_warnings?.length ?? 0;
  const hasQueue = pendingApprovals + failedActions + pendingActions + healthWarnings > 0;

  return (
    <section className="dashboard-grid" aria-label="Dashboard">
      <div className="queue-panel">
        <div className="section-header">
          <div>
            <p className="eyebrow">Action Queue</p>
            <h1>Operator attention</h1>
          </div>
          <span className={`status-pill ${hasQueue ? "warning" : "success"}`}>
            {hasQueue ? "Review" : "Clear"}
          </span>
        </div>
        <div className="metric-row">
          <Metric label="Pending approvals" value={pendingApprovals} tone={pendingApprovals ? "warning" : "neutral"} />
          <Metric label="Dispatch recovery" value={failedActions} tone={failedActions ? "danger" : "neutral"} />
          <Metric label="Pending actions" value={pendingActions} tone={pendingActions ? "info" : "neutral"} />
          <Metric label="Health warnings" value={healthWarnings} tone={healthWarnings ? "warning" : "neutral"} />
        </div>
        {!hasQueue ? (
          <div className="quiet-empty">
            <ShieldCheck aria-hidden="true" size={18} />
            <span>No pending operator actions</span>
          </div>
        ) : null}
      </div>

      <div className="detail-panel">
        <p className="eyebrow">Policy</p>
        <h2>Product Policy Store</h2>
        <dl className="fact-list">
          <Fact label="Initialized" value={snapshot?.policy_status?.initialized ? "Yes" : "No"} />
          <Fact label="Import Diff" value={snapshot?.policy_status?.policy_import_diff?.status ?? "unknown"} />
          <Fact label="Last tick" value={snapshot?.last_run?.last_tick_finished_at ?? "not recorded"} />
        </dl>
      </div>
    </section>
  );
}

function SettingsRoute({
  catalog,
  isLoading,
  error
}: {
  catalog?: SettingsCatalog;
  isLoading: boolean;
  error: Error | null;
}) {
  if (isLoading) {
    return <EmptyState title="Loading settings catalog" detail="Reading console field metadata." />;
  }
  if (error) {
    return <EmptyState title="Settings unavailable" detail={error.message} tone="danger" />;
  }
  const entries = catalog?.entries ?? [];
  const normal = entries.filter((entry) => entry.visibility === "normal").length;
  const advanced = entries.filter((entry) => entry.visibility === "advanced").length;
  const readonly = entries.filter((entry) => entry.visibility === "readonly").length;
  const diagnostic = entries.filter((entry) => entry.visibility === "diagnostic").length;

  return (
    <section className="dashboard-grid" aria-label="Settings">
      <div className="queue-panel">
        <div className="section-header">
          <div>
            <p className="eyebrow">Settings Catalog</p>
            <h1>Console field map</h1>
          </div>
          <span className="status-pill info">v{catalog?.version ?? 1}</span>
        </div>
        <div className="metric-row">
          <Metric label="Normal" value={normal} tone="neutral" />
          <Metric label="Advanced" value={advanced} tone="neutral" />
          <Metric label="Readonly" value={readonly} tone="neutral" />
          <Metric label="Diagnostic" value={diagnostic} tone="neutral" />
        </div>
      </div>

      <div className="detail-panel">
        <p className="eyebrow">Editable in v1</p>
        <h2>Product Policy only</h2>
        <ul className="field-list">
          {entries
            .filter((entry) => entry.editable_v1 === true)
            .slice(0, 8)
            .map((entry) => (
              <li key={entry.key}>
                <span>{entry.label}</span>
                <code>{entry.source}</code>
              </li>
            ))}
        </ul>
      </div>
    </section>
  );
}

function FoundationRoute({ label }: { label: string }) {
  return (
    <section className="dashboard-grid" aria-label={label}>
      <div className="queue-panel">
        <div className="section-header">
          <div>
            <p className="eyebrow">P15 Foundation</p>
            <h1>{label}</h1>
          </div>
          <span className="status-pill muted">Pending</span>
        </div>
        <div className="quiet-empty">
          <FileText aria-hidden="true" size={18} />
          <span>No live queue for this section</span>
        </div>
      </div>
    </section>
  );
}

function Metric({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className={`metric-card ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function EmptyState({ title, detail, tone = "muted" }: { title: string; detail: string; tone?: string }) {
  return (
    <div className="empty-state">
      <RotateCcw aria-hidden="true" className={tone} size={18} />
      <div>
        <h1>{title}</h1>
        <p>{detail}</p>
      </div>
    </div>
  );
}

async function fetchApi<T>(path: string, token: string): Promise<T> {
  const response = await fetch(path, {
    headers: {
      Authorization: `Bearer ${token}`
    }
  });
  const body = (await response.json()) as unknown;
  if (!response.ok) {
    const message = isErrorEnvelope(body) ? body.error.message : `Request failed: ${response.status}`;
    throw new Error(message);
  }
  return body as T;
}

function isErrorEnvelope(value: unknown): value is { error: { message: string } } {
  if (typeof value !== "object" || value === null || !("error" in value)) {
    return false;
  }
  const error = (value as { error?: unknown }).error;
  return (
    typeof error === "object" &&
    error !== null &&
    "message" in error &&
    typeof (error as { message?: unknown }).message === "string"
  );
}

function currentRoute(): RouteKey {
  const hash = window.location.hash.replace("#", "");
  return navItems.some((item) => item.key === hash) ? (hash as RouteKey) : "dashboard";
}

function runtimeStripStatus(snapshot?: DashboardSnapshot) {
  const daemon = String(snapshot?.daemon_liveness?.status ?? "unknown");
  const initialized = snapshot?.policy_status?.initialized;
  const importDiff = snapshot?.policy_status?.policy_import_diff?.status ?? "unknown";
  return [
    { label: "Daemon", value: daemon, tone: daemon === "live" ? "success" : daemon === "unknown" ? "muted" : "warning" },
    { label: "Policy", value: initialized ? "initialized" : "missing", tone: initialized ? "success" : "warning" },
    { label: "Import Diff", value: importDiff, tone: importDiff === "matches" ? "success" : "info" }
  ];
}
