import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Database, LockKeyhole, Settings2, SlidersHorizontal } from "lucide-react";
import { getSettingsCatalog, getSettingsRuntime } from "../api";
import {
  Badge,
  EmptyState,
  ErrorState,
  FieldList,
  LoadingState,
  SectionHeader,
  statusTone
} from "../components/Primitives";
import { queryKeys } from "../queryKeys";
import type { SettingsCatalogEntry, SettingsRuntime, Tone } from "../types";

type SettingsGroup = "normal" | "advanced" | "diagnostics";

const groupLabels: Record<SettingsGroup, { eyebrow: string; title: string; icon: typeof Settings2 }> = {
  normal: { eyebrow: "Normal Settings", title: "Product and workflow fields", icon: Settings2 },
  advanced: { eyebrow: "Advanced Settings", title: "Operational controls", icon: SlidersHorizontal },
  diagnostics: { eyebrow: "Diagnostics", title: "Installation and runtime facts", icon: Database }
};

export function SettingsScreen({ token }: { token: string }) {
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
  const grouped = useMemo(() => groupSettings(visibleEntries), [visibleEntries]);

  if (catalog.isLoading || runtime.isLoading) {
    return <LoadingState title="Loading settings" />;
  }
  if (catalog.error) {
    return <ErrorState title="Settings Catalog unavailable" error={catalog.error} />;
  }
  if (runtime.error) {
    return <ErrorState title="Settings runtime unavailable" error={runtime.error} />;
  }
  if (!catalog.data || !runtime.data) {
    return <EmptyState title="Settings unavailable" detail="The local console did not return catalog and runtime values." />;
  }

  return (
    <section className="settings-screen" aria-label="Settings">
      <div className="queue-panel">
        <SectionHeader
          eyebrow="Settings Catalog"
          title="Console-exposed product fields"
          badge={<Badge tone="info">{visibleEntries.length} visible</Badge>}
        >
          <p className="section-note">
            Settings is a product field map. Product Policy edits live in Policy; config.yaml fields are readonly in v1.
          </p>
        </SectionHeader>
        <div className="policy-diff-grid">
          <FactTile label="Policy" value={runtime.data.policy_status.initialized ? "initialized" : "missing"} tone={runtime.data.policy_status.initialized ? "success" : "warning"} />
          <FactTile label="Import Diff" value={runtime.data.policy_status.policy_import_diff?.status ?? "unknown"} tone={statusTone(runtime.data.policy_status.policy_import_diff?.status)} />
          <FactTile label="Chat policy rows" value={String(runtime.data.chat_policies.length)} tone="info" />
        </div>
      </div>

      <div className="settings-groups">
        {(["normal", "advanced", "diagnostics"] as SettingsGroup[]).map((group) => (
          <SettingsCatalogSection
            entries={grouped[group]}
            group={group}
            key={group}
            runtime={runtime.data}
          />
        ))}
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
  const meta = groupLabels[group];
  const Icon = meta.icon;
  return (
    <section className="queue-panel settings-section" aria-label={meta.title}>
      <div className="subsection-title">
        <Icon aria-hidden="true" size={16} />
        <div>
          <p className="eyebrow">{meta.eyebrow}</p>
          <h2>{meta.title}</h2>
        </div>
      </div>
      {entries.length ? (
        <div className="settings-field-grid">
          {entries.map((entry) => (
            <SettingsField entry={entry} key={entry.key} runtime={runtime} />
          ))}
        </div>
      ) : (
        <EmptyState title="No fields in this section" detail="Hidden fields are intentionally omitted from the default console." />
      )}
    </section>
  );
}

function SettingsField({ entry, runtime }: { entry: SettingsCatalogEntry; runtime: SettingsRuntime }) {
  const value = settingValue(entry, runtime);
  const readonlyReason = readonlyNote(entry);
  return (
    <article className="settings-field">
      <div className="settings-field-head">
        <div>
          <h3>{entry.label}</h3>
          <p>{entry.description}</p>
        </div>
        <Badge tone={entry.editable_v1 ? "info" : "muted"}>{editableLabel(entry)}</Badge>
      </div>
      {entry.help ? <p className="field-help">{entry.help}</p> : null}
      <FieldList>
        <FactRow label="Current value" value={formatSettingValue(value)} />
        <FactRow label="Source" value={entry.source} />
        <FactRow label="Requires restart" value={entry.requires_restart ? "yes" : "no"} />
        <FactRow label="Write boundary" value={entry.write_boundary ?? "none"} />
      </FieldList>
      {readonlyReason ? (
        <div className="readonly-note">
          <LockKeyhole aria-hidden="true" size={14} />
          <span>{readonlyReason}</span>
        </div>
      ) : null}
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

function settingValue(entry: SettingsCatalogEntry, runtime: SettingsRuntime): unknown {
  if (entry.key in runtime.values) {
    return runtime.values[entry.key];
  }
  if (entry.scope === "chat_policy") {
    return runtime.chat_policies.length ? `${runtime.chat_policies.length} chat policy rows` : "no chat policy rows";
  }
  if (entry.scope === "policy_audit") {
    return `${runtime.policy_audit_history.length} recent audits`;
  }
  return null;
}

function editableLabel(entry: SettingsCatalogEntry): string {
  if (entry.editable_v1 === "command") {
    return "command";
  }
  if (entry.editable_v1 === true) {
    return entry.source === "product_policy_store" ? "edit in Policy" : "editable";
  }
  return "readonly";
}

function readonlyNote(entry: SettingsCatalogEntry): string | null {
  if (entry.source === "config_yaml") {
    if (entry.editable_v1 === "command") {
      return "Run the Policy import command from the Policy screen; the console does not write config.yaml.";
    }
    return "Readonly in v1 because config.yaml writes need a future command facade and audit path.";
  }
  if (entry.source === "product_policy_store" && entry.editable_v1 === true) {
    return "Editable through the Policy screen and OperatorCommandService.";
  }
  if (entry.editable_v1 === false) {
    return "Readonly runtime or derived field.";
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

function formatSettingValue(value: unknown): string {
  if (value === true) {
    return "yes";
  }
  if (value === false) {
    return "no";
  }
  if (value === null || value === undefined || value === "") {
    return "not set";
  }
  if (Array.isArray(value)) {
    return value.length ? value.join(", ") : "none";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}
