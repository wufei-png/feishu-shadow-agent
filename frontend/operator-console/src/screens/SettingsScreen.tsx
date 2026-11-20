import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { ArrowUpRight, Database, LockKeyhole, Search, Settings2, ShieldCheck, SlidersHorizontal, SunMoon } from "lucide-react";
import { AnimatePresence, m } from "motion/react";
import { useTranslation } from "react-i18next";
import { getSettingsCatalog, getSettingsRuntime } from "../api";
import {
  Badge,
  EmptyState,
  ErrorState,
  Fact,
  FieldList,
  HelpTooltip,
  LoadingState,
  SectionHeader,
  SegmentedControl
} from "../components/Primitives";
import { catalogText, formatSettingValue } from "../catalogPresentation";
import type { ConsoleLanguage } from "../i18n";
import { groupTransition, groupVariants } from "../motionTokens";
import { usePreferences } from "../preferences";
import { enumLabel } from "../presentation";
import { queryKeys } from "../queryKeys";
import type { MaterialPreference, ThemePreference } from "../theme";
import type { SettingsCatalogEntry, SettingsRuntime } from "../types";

type SettingsGroup = "normal" | "advanced" | "policy" | "diagnostics";

const groupLabels: Record<SettingsGroup, { eyebrowKey: string; titleKey: string; labelKey: string; icon: typeof Settings2 }> = {
  normal: { eyebrowKey: "settings.group.normal.eyebrow", titleKey: "settings.group.normal.title", labelKey: "settings.group.normal", icon: Settings2 },
  advanced: { eyebrowKey: "settings.group.advanced.eyebrow", titleKey: "settings.group.advanced.title", labelKey: "settings.group.advanced", icon: SlidersHorizontal },
  policy: { eyebrowKey: "settings.group.policy.eyebrow", titleKey: "settings.group.policy.title", labelKey: "settings.group.policy", icon: ShieldCheck },
  diagnostics: { eyebrowKey: "settings.group.diagnostics.eyebrow", titleKey: "settings.group.diagnostics.title", labelKey: "settings.group.diagnostics", icon: Database }
};

const settingsGroups: SettingsGroup[] = ["normal", "advanced", "policy", "diagnostics"];

/**
 * Scopes whose write path belongs to the Policy screen. They are collected into
 * their own group because grouping by visibility alone left the default group
 * 69% policy-owned: the operator landed on a screen of cards they could not act
 * on, most of which only said "edit this elsewhere".
 */
const policyScopes = new Set(["global_policy", "chat_policy", "policy_status", "policy_audit", "policy_import"]);

export function SettingsScreen({ token }: { token: string }) {
  const { t } = useTranslation();
  const [activeGroup, setActiveGroup] = useState<SettingsGroup>("normal");
  const [search, setSearch] = useState("");
  const catalog = useQuery({
    queryKey: queryKeys.settingsCatalog(),
    queryFn: () => getSettingsCatalog(token),
    enabled: Boolean(token)
  });
  const runtime = useQuery({
    queryKey: queryKeys.settingsRuntime(),
    queryFn: () => getSettingsRuntime(token),
    enabled: Boolean(token),
    refetchInterval: 15_000
  });
  const visibleEntries = useMemo(() => {
    return (catalog.data?.entries ?? []).filter((entry) => entry.visibility !== "hidden");
  }, [catalog.data]);
  const grouped = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    const matched = query
      ? visibleEntries.filter((entry) =>
          [entry.key, catalogText(t, entry, "label"), catalogText(t, entry, "description"), catalogText(t, entry, "help")]
            .join(" ")
            .toLocaleLowerCase()
            .includes(query)
        )
      : visibleEntries;
    return groupSettings(matched);
  }, [search, t, visibleEntries]);

  useEffect(() => {
    if (!search.trim() || grouped[activeGroup].length) {
      return;
    }
    const firstMatchingGroup = settingsGroups.find((group) => grouped[group].length > 0);
    if (firstMatchingGroup) {
      setActiveGroup(firstMatchingGroup);
    }
  }, [activeGroup, grouped, search]);

  if (catalog.isLoading || runtime.isLoading) {
    return <LoadingState title={t("settings.loading")} />;
  }
  if (catalog.error) {
    return <ErrorState title={t("settings.catalogError")} error={catalog.error} />;
  }
  if (runtime.error) {
    return <ErrorState title={t("settings.runtimeError")} error={runtime.error} />;
  }
  if (!catalog.data || !runtime.data) {
    return <EmptyState title={t("settings.unavailable")} detail={t("settings.unavailableDetail")} />;
  }

  return (
    <section className="settings-screen" aria-label={t("settings.aria")}>
      <div className="queue-panel">
        <SectionHeader
          eyebrow={t("settings.eyebrow")}
          title={t("settings.title")}
          badge={<Badge tone="info">{t("settings.itemCount", { count: visibleEntries.length })}</Badge>}
        >
          <p className="section-note">
            {t("settings.intro")}
          </p>
        </SectionHeader>
      </div>

      <SettingsPreferences />

      <div className="settings-toolbar queue-panel">
        <SegmentedControl
          label={t("settings.groupLabel")}
          onChange={setActiveGroup}
          options={settingsGroups.map((group) => ({ value: group, label: `${t(groupLabels[group].labelKey)} (${grouped[group].length})` }))}
          value={activeGroup}
        />
        <label className="settings-search">
          <span>{t("settings.searchLabel")}</span>
          <span className="settings-search-input">
            <Search aria-hidden="true" size={15} />
            <input onChange={(event) => setSearch(event.target.value)} placeholder={t("settings.searchPlaceholder")} type="search" value={search} />
          </span>
        </label>
      </div>

      <div className="settings-groups">
        {/*
          Cross-fade between groups: the outgoing group clears first so the two
          field grids never overlap and reflow against each other.
        */}
        <AnimatePresence initial={false} mode="wait">
          <m.div
            animate="visible"
            exit="exit"
            initial="hidden"
            key={activeGroup}
            transition={groupTransition}
            variants={groupVariants}
          >
            <SettingsCatalogSection entries={grouped[activeGroup]} group={activeGroup} runtime={runtime.data} />
          </m.div>
        </AnimatePresence>
      </div>
    </section>
  );
}

/**
 * The operator's own preferences, and the only editable controls in the console.
 *
 * They sit above the catalog rather than inside it: a preference is not a
 * product field, it is never searched or filtered, and it must stay reachable
 * while the catalog is filtered down to nothing.
 */
function SettingsPreferences() {
  const { t } = useTranslation();
  const { theme, setTheme, material, setMaterial, language, setLanguage } = usePreferences();

  const themeOptions: Array<{ value: ThemePreference; label: string }> = [
    { value: "system", label: t("app.themeSystem") },
    { value: "light", label: t("app.themeLight") },
    { value: "dark", label: t("app.themeDark") }
  ];
  const materialOptions: Array<{ value: MaterialPreference; label: string }> = [
    { value: "glass", label: t("settings.appearance.glass") },
    { value: "solid", label: t("settings.appearance.solid") }
  ];
  const languageOptions: Array<{ value: ConsoleLanguage; label: string }> = [
    { value: "zh-CN", label: t("language.chinese") },
    { value: "en-US", label: t("language.english") }
  ];

  return (
    <section className="queue-panel settings-preferences" aria-label={t("settings.appearance.title")}>
      <div className="subsection-title">
        <SunMoon aria-hidden="true" size={16} />
        <div>
          <p className="eyebrow">{t("settings.appearance.eyebrow")}</p>
          <h2>{t("settings.appearance.title")}</h2>
        </div>
      </div>
      <div className="preference-list">
        <PreferenceRow description={t("settings.appearance.materialDetail")} label={t("settings.appearance.material")}>
          <SegmentedControl label={t("settings.appearance.material")} onChange={setMaterial} options={materialOptions} value={material} />
        </PreferenceRow>
        <PreferenceRow description={t("settings.appearance.themeDetail")} label={t("app.appearanceLabel")}>
          <SegmentedControl label={t("app.appearanceLabel")} onChange={setTheme} options={themeOptions} value={theme} />
        </PreferenceRow>
        <PreferenceRow description={t("settings.appearance.languageDetail")} label={t("language.label")}>
          <SegmentedControl label={t("language.label")} onChange={setLanguage} options={languageOptions} value={language} />
        </PreferenceRow>
      </div>
    </section>
  );
}

function PreferenceRow({ label, description, children }: { label: string; description: string; children: ReactNode }) {
  return (
    <div className="preference-row">
      <div className="preference-copy">
        <h3>{label}</h3>
        <p>{description}</p>
      </div>
      {children}
    </div>
  );
}

function SettingsCatalogSection({
  entries,
  group,
  runtime
}: {
  entries: SettingsCatalogEntry[];
  group: SettingsGroup;
  runtime: SettingsRuntime;
}) {
  const { t } = useTranslation();
  const meta = groupLabels[group];
  const Icon = meta.icon;
  return (
    <section className="queue-panel settings-section" aria-label={t(meta.titleKey)}>
      <div className="subsection-title">
        <Icon aria-hidden="true" size={16} />
        <div>
          <p className="eyebrow">{t(meta.eyebrowKey)}</p>
          <h2>{t(meta.titleKey)}</h2>
        </div>
      </div>
      {entries.length ? (
        <div className="settings-field-grid">
          {entries.map((entry) => (
            <SettingsField entry={entry} key={entry.key} runtime={runtime} />
          ))}
        </div>
      ) : (
        <EmptyState title={t("settings.noFields")} detail={t("settings.noFieldsDetail")} />
      )}
    </section>
  );
}

function SettingsField({ entry, runtime }: { entry: SettingsCatalogEntry; runtime: SettingsRuntime }) {
  const { t } = useTranslation();
  const value = settingValue(t, entry, runtime);
  const readonlyReason = readonlyNote(t, entry);
  const label = catalogText(t, entry, "label");
  const description = catalogText(t, entry, "description");
  const help = catalogText(t, entry, "help");
  return (
    <article className="settings-field">
      <div className="settings-field-head">
        <span className="field-label-row">
          <h3>{label}</h3>
          <HelpTooltip label={t("settings.helpFor", { label })}>
            <span>{description}</span>
            {help ? <><br />{help}</> : null}
          </HelpTooltip>
        </span>
        {isPolicyOwned(entry) ? (
          // A card whose only content is "edit this elsewhere" is a dead end, so
          // the badge becomes the route to the screen that owns the write path.
          <a className="settings-field-link" href="#policy">
            {t("settings.editPolicy")}
            <ArrowUpRight aria-hidden="true" size={13} />
          </a>
        ) : (
          <Badge tone={entry.editable_v1 ? "info" : "muted"}>{editableLabel(t, entry)}</Badge>
        )}
      </div>
      <div className="settings-current-value">
        <span>{t("settings.currentValue")}</span>
        <strong>{formatSettingValue(t, value)}</strong>
      </div>
      <details className="settings-technical-details">
        <summary>{t("settings.technicalDetails")}</summary>
        <FieldList>
          <Fact label={t("settings.settingKey")} value={entry.key} />
          <Fact label={t("settings.source")} value={enumLabel(t, "source", entry.source)} />
          <Fact label={t("settings.scope")} value={entry.scope} />
          <Fact label={t("settings.requiresRestart")} value={entry.requires_restart ? t("common.yes") : t("common.no")} />
          <Fact label={t("settings.writeBoundary")} value={entry.write_boundary ?? t("common.none")} />
          <Fact label={t("settings.auditBehavior")} value={entry.audit_behavior} />
        </FieldList>
        {readonlyReason ? (
          <div className="readonly-note">
            <LockKeyhole aria-hidden="true" size={14} />
            <span>{readonlyReason}</span>
          </div>
        ) : null}
      </details>
    </article>
  );
}

function groupSettings(entries: SettingsCatalogEntry[]): Record<SettingsGroup, SettingsCatalogEntry[]> {
  return entries.reduce<Record<SettingsGroup, SettingsCatalogEntry[]>>(
    (groups, entry) => {
      groups[groupForEntry(entry)].push(entry);
      return groups;
    },
    { normal: [], advanced: [], policy: [], diagnostics: [] }
  );
}

function groupForEntry(entry: SettingsCatalogEntry): SettingsGroup {
  if (policyScopes.has(entry.scope)) {
    return "policy";
  }
  if (entry.visibility === "advanced") {
    return "advanced";
  }
  if (entry.visibility === "diagnostic" || entry.visibility === "readonly") {
    return "diagnostics";
  }
  return "normal";
}

function settingValue(t: TFunction, entry: SettingsCatalogEntry, runtime: SettingsRuntime): unknown {
  const stored = runtime.values[entry.key];
  // Presence is not enough: the API lists derived fields with a null value, so
  // checking only for the key made the audit card claim "not set" while records
  // existed.
  if (stored !== undefined && stored !== null) {
    return stored;
  }
  // Per-chat fields have no single current value, so the copy says where the
  // value lives rather than presenting a chat count as if it were the value.
  if (entry.scope === "chat_policy") {
    return runtime.chat_policies.length ? t("settings.chatPolicyValue", { count: runtime.chat_policies.length }) : t("settings.noChatPolicyValue");
  }
  if (entry.scope === "policy_audit") {
    return t("settings.auditValue", { count: runtime.policy_audit_history.length });
  }
  return null;
}

/** True when the Policy screen owns this field's write path. */
function isPolicyOwned(entry: SettingsCatalogEntry): boolean {
  return entry.source === "product_policy_store" && entry.editable_v1 === true;
}

function editableLabel(t: TFunction, entry: SettingsCatalogEntry): string {
  if (entry.editable_v1 === "command") {
    return t("settings.editCommand");
  }
  if (entry.editable_v1 === true) {
    return entry.source === "product_policy_store" ? t("settings.editPolicy") : t("settings.editable");
  }
  return t("settings.readOnly");
}

function readonlyNote(t: TFunction, entry: SettingsCatalogEntry): string | null {
  if (entry.source === "config_yaml") {
    if (entry.editable_v1 === "command") {
      return t("settings.noteImport");
    }
    return t("settings.noteConfig");
  }
  if (entry.source === "product_policy_store" && entry.editable_v1 === true) {
    return t("settings.notePolicy");
  }
  if (entry.editable_v1 === false) {
    return t("settings.noteRuntime");
  }
  return null;
}
