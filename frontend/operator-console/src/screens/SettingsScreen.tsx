import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { Database, LockKeyhole, Search, Settings2, SlidersHorizontal } from "lucide-react";
import { useTranslation } from "react-i18next";
import { getSettingsCatalog, getSettingsRuntime } from "../api";
import {
  Badge,
  EmptyState,
  ErrorState,
  FieldList,
  HelpTooltip,
  LoadingState,
  SectionHeader,
  SegmentedControl,
  statusTone
} from "../components/Primitives";
import { catalogText } from "../catalogPresentation";
import { enumLabel } from "../presentation";
import { queryKeys } from "../queryKeys";
import type { SettingsCatalogEntry, SettingsRuntime, Tone } from "../types";

type SettingsGroup = "normal" | "advanced" | "diagnostics";

const groupLabels: Record<SettingsGroup, { eyebrowKey: string; titleKey: string; labelKey: string; icon: typeof Settings2 }> = {
  normal: { eyebrowKey: "settings.group.normal.eyebrow", titleKey: "settings.group.normal.title", labelKey: "settings.group.normal", icon: Settings2 },
  advanced: { eyebrowKey: "settings.group.advanced.eyebrow", titleKey: "settings.group.advanced.title", labelKey: "settings.group.advanced", icon: SlidersHorizontal },
  diagnostics: { eyebrowKey: "settings.group.diagnostics.eyebrow", titleKey: "settings.group.diagnostics.title", labelKey: "settings.group.diagnostics", icon: Database }
};

const settingsGroups: SettingsGroup[] = ["normal", "advanced", "diagnostics"];

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
        <div className="policy-diff-grid">
          <FactTile label={t("settings.policy")} value={runtime.data.policy_status.initialized ? t("policy.initialized") : t("policy.missing")} tone={runtime.data.policy_status.initialized ? "success" : "warning"} />
          <FactTile label={t("settings.importDiff")} value={enumLabel(t, "status", runtime.data.policy_status.policy_import_diff?.status)} tone={statusTone(runtime.data.policy_status.policy_import_diff?.status)} />
          <FactTile label={t("settings.chatPolicyCount")} value={String(runtime.data.chat_policies.length)} tone="info" />
        </div>
      </div>

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
        <SettingsCatalogSection entries={grouped[activeGroup]} group={activeGroup} runtime={runtime.data} />
      </div>
    </section>
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
        <Badge tone={entry.editable_v1 ? "info" : "muted"}>{editableLabel(t, entry)}</Badge>
      </div>
      <div className="settings-current-value">
        <span>{t("settings.currentValue")}</span>
        <strong>{formatSettingValue(t, value)}</strong>
      </div>
      <details className="settings-technical-details">
        <summary>{t("settings.technicalDetails")}</summary>
        <FieldList>
          <FactRow label={t("settings.settingKey")} value={entry.key} />
          <FactRow label={t("settings.source")} value={enumLabel(t, "source", entry.source)} />
          <FactRow label={t("settings.scope")} value={entry.scope} />
          <FactRow label={t("settings.requiresRestart")} value={entry.requires_restart ? t("common.yes") : t("common.no")} />
          <FactRow label={t("settings.writeBoundary")} value={entry.write_boundary ?? t("common.none")} />
          <FactRow label={t("settings.auditBehavior")} value={entry.audit_behavior} />
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
    { normal: [], advanced: [], diagnostics: [] }
  );
}

function groupForEntry(entry: SettingsCatalogEntry): SettingsGroup {
  if (entry.visibility === "advanced") {
    return "advanced";
  }
  if (entry.visibility === "diagnostic" || entry.visibility === "readonly") {
    return "diagnostics";
  }
  return "normal";
}

function settingValue(t: TFunction, entry: SettingsCatalogEntry, runtime: SettingsRuntime): unknown {
  if (entry.key in runtime.values) {
    return runtime.values[entry.key];
  }
  if (entry.scope === "chat_policy") {
    return runtime.chat_policies.length ? t("settings.chatPolicyValue", { count: runtime.chat_policies.length }) : t("settings.noChatPolicyValue");
  }
  if (entry.scope === "policy_audit") {
    return t("settings.auditValue", { count: runtime.policy_audit_history.length });
  }
  return null;
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

function FactTile({ label, value, tone }: { label: string; value: string; tone: Tone }) {
  return (
    <div className={`metric-card ${tone} compact`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function FactRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function formatSettingValue(t: TFunction, value: unknown): string {
  if (value === true) {
    return t("common.yes");
  }
  if (value === false) {
    return t("common.no");
  }
  if (value === null || value === undefined || value === "") {
    return t("settings.notSet");
  }
  if (Array.isArray(value)) {
    return value.length ? value.join(", ") : t("common.none");
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}
