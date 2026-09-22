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
  normal: { eyebrow: "常规设置", title: "产品与流程字段", icon: Settings2 },
  advanced: { eyebrow: "高级设置", title: "运行控制字段", icon: SlidersHorizontal },
  diagnostics: { eyebrow: "诊断信息", title: "安装与运行状态", icon: Database }
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
    return <LoadingState title="正在读取设置" />;
  }
  if (catalog.error) {
    return <ErrorState title="无法读取设置目录" error={catalog.error} />;
  }
  if (runtime.error) {
    return <ErrorState title="无法读取运行设置" error={runtime.error} />;
  }
  if (!catalog.data || !runtime.data) {
    return <EmptyState title="设置暂不可用" detail="本地控制台没有返回设置目录或运行值。" />;
  }

  return (
    <section className="settings-screen" aria-label="设置">
      <div className="queue-panel">
        <SectionHeader
          eyebrow="设置目录"
          title="可查看的产品设置"
          badge={<Badge tone="info">{visibleEntries.length} 项</Badge>}
        >
          <p className="section-note">
            此处展示产品字段。策略修改请前往“策略”；config.yaml 字段当前只读。
          </p>
        </SectionHeader>
        <div className="policy-diff-grid">
          <FactTile label="策略" value={runtime.data.policy_status.initialized ? "已初始化" : "缺失"} tone={runtime.data.policy_status.initialized ? "success" : "warning"} />
          <FactTile label="导入差异" value={runtime.data.policy_status.policy_import_diff?.status ?? "未知"} tone={statusTone(runtime.data.policy_status.policy_import_diff?.status)} />
          <FactTile label="会话策略数" value={String(runtime.data.chat_policies.length)} tone="info" />
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
        <EmptyState title="此分类暂无字段" detail="隐藏字段不会显示在默认控制台中。" />
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
        <FactRow label="当前值" value={formatSettingValue(value)} />
        <FactRow label="来源" value={entry.source} />
        <FactRow label="需要重启" value={entry.requires_restart ? "是" : "否"} />
        <FactRow label="写入边界" value={entry.write_boundary ?? "无"} />
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
    return runtime.chat_policies.length ? `${runtime.chat_policies.length} 条会话策略` : "没有会话策略";
  }
  if (entry.scope === "policy_audit") {
    return `${runtime.policy_audit_history.length} 条近期审计记录`;
  }
  return null;
}

function editableLabel(entry: SettingsCatalogEntry): string {
  if (entry.editable_v1 === "command") {
    return "通过命令修改";
  }
  if (entry.editable_v1 === true) {
    return entry.source === "product_policy_store" ? "在策略页修改" : "可编辑";
  }
  return "只读";
}

function readonlyNote(entry: SettingsCatalogEntry): string | null {
  if (entry.source === "config_yaml") {
    if (entry.editable_v1 === "command") {
      return "可在策略页运行导入命令；控制台不会写入 config.yaml。";
    }
    return "当前只读；修改 config.yaml 仍需专门的命令和审计路径。";
  }
  if (entry.source === "product_policy_store" && entry.editable_v1 === true) {
    return "可在策略页通过受审计的操作修改。";
  }
  if (entry.editable_v1 === false) {
    return "运行时或派生字段，只读。";
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
    return "是";
  }
  if (value === false) {
    return "否";
  }
  if (value === null || value === undefined || value === "") {
    return "未设置";
  }
  if (Array.isArray(value)) {
    return value.length ? value.join(", ") : "无";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}
