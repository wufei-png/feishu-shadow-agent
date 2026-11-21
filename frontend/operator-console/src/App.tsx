import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { Activity, Bell, Bot, ChevronDown, ClipboardList, Database, FileText, HeartPulse, Home, MessageSquareDiff, Send, Settings, ShieldCheck, Wrench } from "lucide-react";
import { LazyMotion, MotionConfig, m } from "motion/react";
import { getDashboard } from "./api";
import { GlassLens } from "./components/GlassLens";
import { Badge, EmptyState, LoadingState } from "./components/Primitives";
import { useSelectionSlide } from "./components/useSelectionSlide";
import { bootstrapTokenFromHash, decodeHashSegment } from "./consoleSession";
import { routeTransition, routeVariants } from "./motionTokens";
import { enumLabel } from "./presentation";
import { PreferencesProvider } from "./preferences";
import { queryKeys } from "./queryKeys";
import type { DashboardSnapshot, RouteKey, Tone } from "./types";

const TOKEN_STORAGE_KEY = "feishu_shadow_agent_console_token";

const loadMotionFeatures = () => import("motion/react").then((module) => module.domAnimation);

type NavItem = { key: RouteKey; labelKey: string; icon: typeof Home };

/**
 * Group destinations by work, configuration, and system status.
 */
const navSections: Array<{ key: string; labelKey: string; items: NavItem[] }> = [
  {
    key: "duty",
    labelKey: "navigation.section.duty",
    items: [
      { key: "dashboard", labelKey: "navigation.dashboard", icon: Home },
      { key: "coworker", labelKey: "navigation.coworker", icon: Bot },
      { key: "approvals", labelKey: "navigation.approvals", icon: Bell },
      { key: "tasks", labelKey: "navigation.tasks", icon: ClipboardList },
      { key: "dispatch", labelKey: "navigation.dispatch", icon: Send },
      { key: "feedback", labelKey: "navigation.feedback", icon: MessageSquareDiff }
    ]
  },
  {
    key: "configure",
    labelKey: "navigation.section.configure",
    items: [
      { key: "policy", labelKey: "navigation.policy", icon: ShieldCheck },
      { key: "settings", labelKey: "navigation.settings", icon: Settings }
    ]
  },
  {
    key: "system",
    labelKey: "navigation.section.system",
    items: [
      { key: "health", labelKey: "navigation.health", icon: HeartPulse },
      { key: "maintenance", labelKey: "navigation.maintenance", icon: Wrench }
    ]
  }
];

const navItems: NavItem[] = navSections.flatMap((section) => section.items);

/**
 * One screen renders at a time, so bundling every screen into the first payload makes
 * every visit pay for the screens it will not open. Each loads on first visit and
 * is cached by the browser afterwards; on a loopback console that fetch is free.
 */
const ApprovalsScreen = lazy(() => import("./screens/ApprovalsScreen").then((module) => ({ default: module.ApprovalsScreen })));
const DashboardScreen = lazy(() => import("./screens/DashboardScreen").then((module) => ({ default: module.DashboardScreen })));
const CoworkerScreen = lazy(() => import("./screens/CoworkerScreen").then((module) => ({ default: module.CoworkerScreen })));
const DispatchScreen = lazy(() => import("./screens/DispatchScreen").then((module) => ({ default: module.DispatchScreen })));
const FeedbackScreen = lazy(() => import("./screens/FeedbackScreen").then((module) => ({ default: module.FeedbackScreen })));
const HealthScreen = lazy(() => import("./screens/HealthScreen").then((module) => ({ default: module.HealthScreen })));
const MaintenanceScreen = lazy(() => import("./screens/MaintenanceScreen").then((module) => ({ default: module.MaintenanceScreen })));
const PolicyScreen = lazy(() => import("./screens/PolicyScreen").then((module) => ({ default: module.PolicyScreen })));
const SettingsScreen = lazy(() => import("./screens/SettingsScreen").then((module) => ({ default: module.SettingsScreen })));
const TasksScreen = lazy(() => import("./screens/TasksScreen").then((module) => ({ default: module.TasksScreen })));

export function App() {
  return (
    /*
     * LazyMotion keeps the animation feature set lazily loaded, and
     * `reducedMotion="user"` makes the OS setting authoritative: transform
     * animations are dropped while opacity transitions still report that state
     * changed. Both live here rather than in the entry point so tests exercise
     * the same tree the app runs.
     */
    <LazyMotion features={loadMotionFeatures} strict>
      <MotionConfig reducedMotion="user">
        <PreferencesProvider>
          <ConsoleShell />
        </PreferencesProvider>
      </MotionConfig>
    </LazyMotion>
  );
}

function ConsoleShell() {
  const { i18n, t } = useTranslation();
  const [token, setToken] = useState(() => sessionStorage.getItem(TOKEN_STORAGE_KEY) ?? "");
  const [location, setLocation] = useState(() => currentLocation());
  const main = useRef<HTMLElement>(null);
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
      <a className="skip-link" href="#console-main" onClick={(event) => {
        event.preventDefault();
        main.current?.focus({ preventScroll: true });
        main.current?.scrollIntoView?.({ block: "start" });
      }}>{t("app.skipToMain")}</a>
      <ConsoleNavigation route={location.route} />
      <div className="workspace">
        <RuntimeStrip mode={dashboard.data?.runtime_mode} status={runtimeStatus} storeAvailable={dashboard.data?.store_status?.available} />
        <main className="main-surface" id="console-main" ref={main} tabIndex={-1}>
          {/*
            Keyed so each destination mounts fresh and fades in. There is no
            exit animation: holding the outgoing screen on screen for even 100ms
            would make a high-frequency sidebar click feel slow, and a real
            cross-fade would require overlapping both screens in the layout.
            Suspense sits outside the animated wrapper so a first visit shows the
            loading state immediately and the loaded screen still fades in.
          */}
          <Suspense fallback={<LoadingState />}>
          <m.div animate="visible" initial="hidden" key={location.route} variants={routeVariants} transition={routeTransition}>
            {!token ? (
              <EmptyState title={t("app.missingToken")} detail={t("app.missingTokenDetail")} />
            ) : location.route === "dashboard" ? (
              <DashboardScreen navigate={navigate} token={token} />
            ) : location.route === "coworker" ? (
              <CoworkerScreen token={token} />
            ) : location.route === "approvals" ? (
              <ApprovalsScreen selectedId={location.selectedId} token={token} />
            ) : location.route === "tasks" ? (
              <TasksScreen initialFilter={location.view === "all" ? "all" : undefined} selectedId={location.selectedId} selectedMessageId={location.messageId} token={token} />
            ) : location.route === "dispatch" ? (
              <DispatchScreen initialFilter={location.view === "attention" ? "attention" : undefined} selectedId={location.selectedId} token={token} />
            ) : location.route === "feedback" ? (
              <FeedbackScreen token={token} runtimeMode={dashboard.data?.runtime_mode} />
            ) : location.route === "policy" ? (
              <PolicyScreen selectedId={location.selectedId} token={token} />
            ) : location.route === "settings" ? (
              <SettingsScreen token={token} />
            ) : location.route === "health" ? (
              <HealthScreen token={token} selectedId={location.selectedId} />
            ) : location.route === "maintenance" ? (
              <MaintenanceScreen token={token} />
            ) : (
              <FollowUpScreen route={location.route} />
            )}
          </m.div>
          </Suspense>
        </main>
      </div>
    </div>
  );
}

function ConsoleNavigation({ route }: { route: RouteKey }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [touchPressed, setTouchPressed] = useState(false);
  const sidebar = useRef<HTMLElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const indicator = useSelectionSlide(route);
  const page = t(navItems.find((item) => item.key === route)?.labelKey ?? "navigation.console");

  useEffect(() => {
    if (!open) return;
    function dismissOutside(event: PointerEvent) {
      if (event.target instanceof Node && !sidebar.current?.contains(event.target)) setOpen(false);
    }
    document.addEventListener("pointerdown", dismissOutside);
    return () => document.removeEventListener("pointerdown", dismissOutside);
  }, [open]);

  function closeMenu() {
    setOpen(false);
    trigger.current?.focus();
  }

  return (
    <aside
      className="side-nav floating-surface"
      data-glass-pressed={touchPressed || undefined}
      data-menu-open={open}
      ref={sidebar}
      onBlur={(event) => {
        if (open && !event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
      onKeyDown={(event) => {
        if (open && event.key === "Escape") {
          event.preventDefault();
          closeMenu();
        }
      }}
    >
      <GlassLens />
      <div className="nav-top">
        <div className="brand-lockup">
          <span className="brand-mark"><Database aria-hidden="true" size={19} strokeWidth={1.7} /></span>
          <div>
            <span className="brand-title">Shadow Agent</span>
            <span className="brand-subtitle">{t("app.brandSubtitle")}</span>
          </div>
        </div>
        <button
          aria-controls="console-navigation"
          aria-expanded={open}
          aria-label={t("app.navigationMenu", { page })}
          className="nav-trigger"
          onClick={() => setOpen((current) => !current)}
          onPointerDown={(event) => {
            if (event.pointerType === "touch") setTouchPressed(true);
          }}
          onLostPointerCapture={() => setTouchPressed(false)}
          onPointerCancel={() => setTouchPressed(false)}
          onPointerUp={() => setTouchPressed(false)}
          ref={trigger}
          type="button"
        >
          <span>{page}</span>
          <ChevronDown aria-hidden="true" size={16} />
        </button>
      </div>
      <nav aria-label={t("app.mainNavigation")} className="nav-list" id="console-navigation">
        {navSections.map((section) => (
          <div className="nav-section" key={section.key}>
            <p className="nav-section-label">{t(section.labelKey)}</p>
            {section.items.map((item) => (
              <a
                aria-current={route === item.key ? "page" : undefined}
                className="nav-item"
                href={`#${item.key}`}
                key={item.key}
                onClick={open ? closeMenu : undefined}
              >
                {route === item.key ? (
                  <span aria-hidden="true" className="nav-active-pill" ref={indicator}>
                    <span className="selection-rim"><span className="selection-rim-glint" /></span>
                  </span>
                ) : null}
                <item.icon aria-hidden="true" size={17} strokeWidth={1.8} />
                <span className="nav-item-label">{t(item.labelKey)}</span>
              </a>
            ))}
          </div>
        ))}
      </nav>
    </aside>
  );
}

type RuntimeReading = { label: string; value: string; tone: Tone; href?: string };

function RuntimeStrip({ mode, status, storeAvailable }: {
  mode: DashboardSnapshot["runtime_mode"];
  status: RuntimeReading[];
  storeAvailable?: boolean;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const primary: RuntimeReading = storeAvailable === false
    ? { label: t("health.storage"), value: enumLabel(t, "status", "unavailable"), tone: "danger", href: "#health" }
    : status.find((item) => item.href && item.tone === "danger") ?? status.find((item) => item.href) ?? status[3];
  return (
    <header aria-label={t("app.runtime")} className="runtime-strip floating-surface">
      <GlassLens />
      <button
        aria-controls="runtime-readings"
        aria-expanded={expanded}
        className="runtime-heading"
        onClick={() => setExpanded((current) => !current)}
        type="button"
      >
        <Activity aria-hidden="true" size={16} />
        <span>{t("app.runtime")}</span>
        <ChevronDown aria-hidden="true" size={13} />
      </button>
      {mode ? (
        <Badge tone={mode === "dry_run" ? "info" : "success"}>
          {t(mode === "dry_run" ? "app.modeDryRun" : "app.modeProduction")}
        </Badge>
      ) : null}
      <RuntimeItem item={primary} />
      <div className="runtime-items" hidden={!expanded} id="runtime-readings">
        {status.map((item) => <RuntimeItem item={item} key={item.label} />)}
      </div>
    </header>
  );
}

function RuntimeItem({ item }: { item: RuntimeReading }) {
  return (
    <div className="runtime-item">
      <span aria-hidden="true" className={`tell-tale ${item.tone}`} />
      <span className="runtime-label">{item.label}</span>
      {item.href ? <a className="runtime-value" href={item.href}>{item.value}</a> : <span className="runtime-value">{item.value}</span>}
    </div>
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

function currentLocation() {
  const hash = window.location.hash.replace("#", "");
  const [path, query] = hash.split("?");
  const [routeText, selectedId] = path.split("/");
  const route = navItems.some((item) => item.key === routeText) ? (routeText as RouteKey) : "dashboard";
  const params = new URLSearchParams(query);
  return {
    route,
    selectedId: decodeHashSegment(selectedId),
    view: params.get("view"),
    messageId: params.get("message")
  };
}

function runtimeStripStatus(t: TFunction, snapshot: DashboardSnapshot | undefined, error: unknown, updatedAt: number): RuntimeReading[] {
  const daemon = snapshot?.daemon_liveness?.status ?? "unknown";
  const initialized = snapshot?.policy_status?.initialized;
  const importDiff = snapshot?.policy_status?.policy_import_diff?.status ?? "unknown";
  const dataStale = updatedAt > 0 && Date.now() - updatedAt > 30_000;
  const daemonTone = daemon === "live" ? "success" : daemon === "startup_failed" || daemon === "stale" ? "danger" : daemon === "starting" ? "info" : daemon === "unknown" ? "muted" : "warning";
  return [
    {
      label: t("runtime.daemon"),
      value: enumLabel(t, "status", daemon),
      tone: daemonTone,
      href: daemonTone === "danger" || daemonTone === "warning" ? `#health/daemon-${daemon.replace(/_/g, "-")}` : undefined
    },
    {
      label: t("runtime.policy"),
      value: initialized === true ? t("runtime.initialized") : initialized === false ? t("runtime.missing") : t("common.unknown"),
      tone: initialized === true ? "success" : initialized === false ? "warning" : "muted",
      href: initialized === false ? "#policy" : undefined
    },
    {
      label: t("runtime.importDiff"),
      value: importDiff === "matches" ? t("runtime.matches") : importDiff === "differs" ? t("runtime.differs") : t("common.unknown"),
      tone: importDiff === "matches" ? "success" : importDiff === "differs" ? "warning" : "muted",
      href: importDiff === "differs" ? "#policy" : undefined
    },
    {
      label: t("runtime.data"),
      value: error ? t("runtime.readFailed") : dataStale ? t("runtime.stale") : updatedAt ? t("runtime.latest") : t("runtime.waiting"),
      tone: error ? "danger" : dataStale ? "warning" : updatedAt ? "success" : "muted",
      href: error || dataStale ? "#health" : undefined
    }
  ];
}
