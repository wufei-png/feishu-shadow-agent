import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileDiff, History, Save, Trash2, Upload } from "lucide-react";
import {
  deleteChatPolicy,
  getSettingsCatalog,
  getSettingsRuntime,
  importPolicyConfig,
  listPolicyAudits,
  previewChatPolicy,
  previewDeleteChatPolicy,
  previewGlobalPolicy,
  updateChatPolicy,
  updateGlobalPolicy
} from "../api";
import {
  Badge,
  Button,
  CommandResultPanel,
  EmptyState,
  ErrorState,
  FieldList,
  formatDate,
  JsonBlock,
  ListRow,
  LoadingState,
  SectionHeader,
  shortText,
  statusTone,
  TextareaField
} from "../components/Primitives";
import { invalidateAfterPolicyCommand, queryKeys } from "../queryKeys";
import type {
  ChatPolicy,
  CommandResult,
  PolicyImpactPreview,
  ProductPolicy,
  ReplyIdentity,
  SettingsCatalogEntry,
  SettingsRuntime
} from "../types";

type PolicyScope = "global" | "new-chat" | `chat:${string}`;
type AuditScopeFilter = "all" | "global" | "chat";

type GlobalPolicyForm = {
  p2p_auto_reply: boolean;
  unknown_group_auto_reply: boolean;
  bot_joined: boolean;
  reply_identity: ReplyIdentity;
  allow_user_fallback: boolean;
  resource_download: boolean;
};

type ChatPolicyForm = {
  name: string;
  auto_reply: boolean;
  bot_joined: boolean;
  reply_identity: ReplyIdentity;
  allow_user_fallback: boolean;
  resource_download: boolean;
};

const globalFields: Array<{ key: keyof GlobalPolicyForm; catalogKey: string; type: "boolean" | "identity" }> = [
  { key: "p2p_auto_reply", catalogKey: "policy.global.p2p_auto_reply", type: "boolean" },
  { key: "unknown_group_auto_reply", catalogKey: "policy.global.unknown_group_auto_reply", type: "boolean" },
  { key: "bot_joined", catalogKey: "policy.global.default_bot_joined", type: "boolean" },
  { key: "reply_identity", catalogKey: "policy.global.default_reply_identity", type: "identity" },
  { key: "allow_user_fallback", catalogKey: "policy.global.default_allow_user_fallback", type: "boolean" },
  { key: "resource_download", catalogKey: "policy.global.default_resource_download", type: "boolean" }
];

const chatFields: Array<{ key: keyof ChatPolicyForm; catalogKey: string; type: "boolean" | "identity" | "text" }> = [
  { key: "name", catalogKey: "policy.chat.name", type: "text" },
  { key: "auto_reply", catalogKey: "policy.chat.auto_reply", type: "boolean" },
  { key: "bot_joined", catalogKey: "policy.chat.bot_joined", type: "boolean" },
  { key: "reply_identity", catalogKey: "policy.chat.reply_identity", type: "identity" },
  { key: "allow_user_fallback", catalogKey: "policy.chat.allow_user_fallback", type: "boolean" },
  { key: "resource_download", catalogKey: "policy.chat.resource_download", type: "boolean" }
];

export function PolicyScreen({ token, selectedId }: { token: string; selectedId: string | null }) {
  const queryClient = useQueryClient();
  const [scope, setScope] = useState<PolicyScope>("global");
  const [reason, setReason] = useState("");
  const [commandResult, setCommandResult] = useState<CommandResult | null>(null);
  const [globalForm, setGlobalForm] = useState<GlobalPolicyForm>(() => globalFormFromPolicy(null));
  const [globalDirty, setGlobalDirty] = useState(false);
  const [chatForm, setChatForm] = useState<ChatPolicyForm>(() => chatFormFromPolicy(null));
  const [chatIdDraft, setChatIdDraft] = useState("");
  const [chatDirty, setChatDirty] = useState(false);
  const [lastChatScope, setLastChatScope] = useState<PolicyScope | null>(null);
  const [auditScope, setAuditScope] = useState<AuditScopeFilter>("all");
  const [auditPolicyKey, setAuditPolicyKey] = useState("");
  const runtime = useQuery({
    queryKey: queryKeys.settingsRuntime(),
    queryFn: () => getSettingsRuntime(token),
    enabled: Boolean(token),
    refetchInterval: 15_000
  });
  const catalog = useQuery({
    queryKey: queryKeys.settingsCatalog(),
    queryFn: () => getSettingsCatalog(token),
    enabled: Boolean(token)
  });
  const auditFilters = {
    limit: 50,
    offset: 0,
    scope: auditScope === "all" ? undefined : auditScope,
    policy_key: clean(auditPolicyKey)
  };
  const audits = useQuery({
    queryKey: queryKeys.policyAudits(auditFilters),
    queryFn: () => listPolicyAudits(token, auditFilters),
    enabled: Boolean(token)
  });
  const entries = useMemo(() => catalog.data?.entries ?? [], [catalog.data]);
  const selectedChatId = scope.startsWith("chat:") ? scope.slice("chat:".length) : null;
  const selectedChat = useMemo(
    () => runtime.data?.chat_policies.find((policy) => policy.chat_id === selectedChatId) ?? null,
    [runtime.data?.chat_policies, selectedChatId]
  );
  const globalBaseline = globalFormFromPolicy(runtime.data?.global_policy ?? null);
  const chatBaseline = chatFormFromPolicy(selectedChat);
  const globalChanges = diffObject(globalBaseline, globalForm);
  const chatChanges = scope === "new-chat" ? { ...chatForm } : diffObject(chatBaseline, chatForm);
  const policyInitialized = runtime.data?.policy_status.initialized === true;
  const activeChatId = scope === "new-chat" ? chatIdDraft.trim() : selectedChat?.chat_id ?? "";
  const hasGlobalChanges = Object.keys(globalChanges).length > 0;
  const hasChatChanges = Object.keys(chatChanges).length > 0;
  const globalPreview = useQuery({
    queryKey: queryKeys.policyImpactPreview("global", "reply_policy", globalChanges as Record<string, unknown>),
    queryFn: () => previewGlobalPolicy(token, globalChanges),
    enabled: Boolean(token && policyInitialized && scope === "global" && hasGlobalChanges)
  });
  const chatPreview = useQuery({
    queryKey: queryKeys.policyImpactPreview("chat", activeChatId, chatChanges as Record<string, unknown>),
    queryFn: () => previewChatPolicy(token, activeChatId, chatChanges),
    enabled: Boolean(token && policyInitialized && scope !== "global" && activeChatId && hasChatChanges)
  });
  const deletePreview = useQuery({
    queryKey: queryKeys.policyImpactPreview("chat-delete", activeChatId, {}),
    queryFn: () => previewDeleteChatPolicy(token, activeChatId),
    enabled: Boolean(token && scope.startsWith("chat:") && activeChatId)
  });

  useEffect(() => {
    setScope(selectedId ? `chat:${selectedId}` : "global");
  }, [selectedId]);

  useEffect(() => {
    if (!globalDirty) {
      setGlobalForm(globalFormFromPolicy(runtime.data?.global_policy ?? null));
    }
  }, [globalDirty, runtime.data?.global_policy]);

  useEffect(() => {
    if (scope === "global") {
      return;
    }
    if (scope !== lastChatScope) {
      setChatForm(chatFormFromPolicy(selectedChat));
      setChatIdDraft(selectedChat?.chat_id ?? "");
      setChatDirty(false);
      setLastChatScope(scope);
      return;
    }
    if (!chatDirty) {
      setChatForm(chatFormFromPolicy(selectedChat));
      setChatIdDraft(selectedChat?.chat_id ?? "");
    }
  }, [chatDirty, lastChatScope, scope, selectedChat]);

  const afterPolicyCommand = async (result: CommandResult, clearDirty: "global" | "chat" | null) => {
    setCommandResult(result);
    if (result.status === "applied" || result.status === "no_change") {
      if (clearDirty === "global") {
        setGlobalDirty(false);
      }
      if (clearDirty === "chat") {
        setChatDirty(false);
      }
    }
    await invalidateAfterPolicyCommand(queryClient);
  };

  const importConfig = useMutation({
    mutationFn: (replace: boolean) => importPolicyConfig(token, { replace, reason: clean(reason) }),
    onSuccess: (result) => afterPolicyCommand(result, null),
    onError: (error) => setCommandResult(errorResult("policy.import_config", error))
  });
  const saveGlobal = useMutation({
    mutationFn: () =>
      updateGlobalPolicy(token, {
        ...globalChanges,
        reason: clean(reason)
      }),
    onSuccess: (result) => afterPolicyCommand(result, "global"),
    onError: (error) => setCommandResult(errorResult("policy.update_global", error))
  });
  const saveChat = useMutation({
    mutationFn: () =>
      updateChatPolicy(token, activeChatId, {
        ...chatChanges,
        reason: clean(reason)
      }),
    onSuccess: (result) => afterPolicyCommand(result, "chat"),
    onError: (error) => setCommandResult(errorResult("policy.update_chat", error))
  });
  const deleteChat = useMutation({
    mutationFn: () => deleteChatPolicy(token, activeChatId, { reason: clean(reason) }),
    onSuccess: async (result) => {
      await afterPolicyCommand(result, "chat");
      if (result.status === "applied" || result.status === "no_change") {
        setScope("global");
        setChatForm(chatFormFromPolicy(null));
        setChatIdDraft("");
        setLastChatScope(null);
      }
    },
    onError: (error) => setCommandResult(errorResult("policy.delete_chat", error))
  });

  if (runtime.isLoading || catalog.isLoading) {
    return <LoadingState title="Loading policy" />;
  }
  if (runtime.error) {
    return <ErrorState title="Policy runtime unavailable" error={runtime.error} />;
  }
  if (catalog.error) {
    return <ErrorState title="Settings Catalog unavailable" error={catalog.error} />;
  }

  const data = runtime.data;
  if (!data) {
    return <EmptyState title="Policy unavailable" detail="The local console did not return policy runtime state." />;
  }

  return (
    <section className="work-grid policy-layout" aria-label="Policy">
      <div className="work-main">
        <PolicyStatusPanel
          data={data}
          importPending={importConfig.isPending}
          onImport={(replace) => importConfig.mutate(replace)}
        />
        <PolicyScopeList
          data={data}
          scope={scope}
          setScope={setScope}
        />
      </div>

      <aside className="work-detail">
        <div className="detail-panel">
          <p className="eyebrow">Command Note</p>
          <h2>Policy change reason</h2>
          <TextareaField label="Reason" onChange={setReason} placeholder="Optional policy audit reason" rows={2} value={reason} />
        </div>

        {scope === "global" ? (
          <GlobalPolicyEditor
            changes={globalChanges}
            disabled={!policyInitialized || saveGlobal.isPending}
            entries={entries}
            form={globalForm}
            onChange={(next) => {
              setGlobalDirty(true);
              setGlobalForm(next);
            }}
            onSave={() => saveGlobal.mutate()}
            preview={globalPreview.data ?? null}
            previewError={globalPreview.error}
            previewLoading={globalPreview.isFetching}
          />
        ) : (
          <ChatPolicyEditor
            changes={chatChanges}
            deleteDisabled={!activeChatId || deleteChat.isPending}
            deletePreview={deletePreview.data ?? null}
            deletePreviewError={deletePreview.error}
            deletePreviewLoading={deletePreview.isFetching}
            entries={entries}
            form={chatForm}
            isNew={scope === "new-chat"}
            onChange={(next) => {
              setChatDirty(true);
              setChatForm(next);
            }}
            onChatIdChange={(value) => {
              setChatDirty(true);
              setChatIdDraft(value);
            }}
            onDelete={() => deleteChat.mutate()}
            onSave={() => saveChat.mutate()}
            saveDisabled={!policyInitialized || !activeChatId || saveChat.isPending}
            selectedChatId={activeChatId}
            updatePreview={chatPreview.data ?? null}
            updatePreviewError={chatPreview.error}
            updatePreviewLoading={chatPreview.isFetching}
          />
        )}

        <CommandResultPanel result={commandResult} />
        <PolicyAuditHistory
          audits={audits.data ?? data.policy_audit_history}
          error={audits.error}
          filterKey={auditPolicyKey}
          filterScope={auditScope}
          isLoading={audits.isLoading}
          onFilterKeyChange={setAuditPolicyKey}
          onFilterScopeChange={setAuditScope}
          usingFallback={!audits.data}
        />
      </aside>
    </section>
  );
}

function PolicyStatusPanel({
  data,
  importPending,
  onImport
}: {
  data: SettingsRuntime;
  importPending: boolean;
  onImport: (replace: boolean) => void;
}) {
  const diff = data.policy_status.policy_import_diff;
  return (
    <div className="queue-panel">
      <SectionHeader
        eyebrow="Product Policy"
        title="Runtime policy state"
        badge={<Badge tone={data.policy_status.initialized ? "success" : "warning"}>{data.policy_status.initialized ? "Initialized" : "Missing"}</Badge>}
      >
        <p className="section-note">Product Policy Store is runtime truth. Policy Import Source remains read-only input from config.yaml.</p>
      </SectionHeader>
      <div className="policy-diff-grid">
        <FactTile label="Policy Import Diff" value={<Badge tone={statusTone(diff?.status)}>{diff?.status ?? "unknown"}</Badge>} />
        <FactTile label="Chat policies" value={String(data.policy_status.chat_policy_count ?? data.chat_policies.length)} />
        <FactTile label="Global updated" value={formatDate(data.policy_status.global_policy_updated_at)} />
      </div>
      {diff?.message ? <p className="detail-note">{diff.message}</p> : null}
      <ImportDiffDetails diff={diff} />
      <div className="command-buttons">
        <Button disabled={importPending} onClick={() => onImport(false)} tone="info">
          <Upload aria-hidden="true" size={15} />
          Import missing
        </Button>
        <Button disabled={importPending} onClick={() => onImport(true)} tone="warning">
          <FileDiff aria-hidden="true" size={15} />
          Replace config-listed
        </Button>
      </div>
    </div>
  );
}

function PolicyScopeList({
  data,
  scope,
  setScope
}: {
  data: SettingsRuntime;
  scope: PolicyScope;
  setScope: (scope: PolicyScope) => void;
}) {
  return (
    <div className="queue-panel">
      <SectionHeader
        eyebrow="Policy Scopes"
        title="Global and chat policy"
        badge={<Badge tone={data.chat_policies.length ? "info" : "muted"}>{data.chat_policies.length} chats</Badge>}
      />
      <div className="list-stack">
        <ListRow
          badge={<Badge tone={data.global_policy ? "success" : "warning"}>{data.global_policy ? "ready" : "missing"}</Badge>}
          meta="Default policy for P2P and chats without explicit rows"
          onClick={() => setScope("global")}
          selected={scope === "global"}
          title="Global policy"
        />
        <ListRow
          badge={<Badge tone="info">new</Badge>}
          meta="Create or replace one chat policy through the command facade"
          onClick={() => setScope("new-chat")}
          selected={scope === "new-chat"}
          title="New chat policy"
        />
        {data.chat_policies.length ? (
          data.chat_policies.map((policy) => (
            <ListRow
              badge={<Badge tone={policy.auto_reply ? "success" : "muted"}>{policy.auto_reply ? "auto" : "manual"}</Badge>}
              key={policy.chat_id}
              meta={`${policy.reply_identity} · ${formatDate(policy.updated_at)}`}
              onClick={() => setScope(`chat:${policy.chat_id}`)}
              selected={scope === `chat:${policy.chat_id}`}
              title={policy.name || policy.chat_id}
            >
              <span className="row-preview">{policy.chat_id}</span>
            </ListRow>
          ))
        ) : (
          <EmptyState title="No chat policy rows" detail="Config import or a direct chat policy update will create rows here." />
        )}
      </div>
    </div>
  );
}

function GlobalPolicyEditor({
  changes,
  disabled,
  entries,
  form,
  onChange,
  onSave,
  preview,
  previewError,
  previewLoading
}: {
  changes: Record<string, unknown>;
  disabled: boolean;
  entries: SettingsCatalogEntry[];
  form: GlobalPolicyForm;
  onChange: (form: GlobalPolicyForm) => void;
  onSave: () => void;
  preview: PolicyImpactPreview | null;
  previewError: unknown;
  previewLoading: boolean;
}) {
  return (
    <div className="detail-panel">
      <p className="eyebrow">Global Policy Editor</p>
      <div className="detail-title-row">
        <h2>Default runtime policy</h2>
        <Badge tone={Object.keys(changes).length ? "warning" : "success"}>{Object.keys(changes).length ? "Unsaved" : "Current"}</Badge>
      </div>
      <div className="policy-form-grid">
        {globalFields.map((field) => (
          <PolicyField
            entry={entryFor(entries, field.catalogKey)}
            key={field.key}
            type={field.type}
            value={form[field.key]}
            onChange={(value) => onChange({ ...form, [field.key]: value })}
          />
        ))}
      </div>
      <ChangePreview changes={changes} entries={entries} fieldDefs={globalFields} />
      <PolicyImpactPreviewPanel
        error={previewError}
        isLoading={previewLoading}
        preview={preview}
        title="Runtime impact preview"
      />
      <Button disabled={disabled || Object.keys(changes).length === 0} onClick={onSave} tone="success">
        <Save aria-hidden="true" size={15} />
        Save global policy
      </Button>
      {!disabled ? null : <p className="detail-note">Global policy updates require initialized Product Policy Store.</p>}
    </div>
  );
}

function ChatPolicyEditor({
  changes,
  deleteDisabled,
  deletePreview,
  deletePreviewError,
  deletePreviewLoading,
  entries,
  form,
  isNew,
  onChange,
  onChatIdChange,
  onDelete,
  onSave,
  saveDisabled,
  selectedChatId,
  updatePreview,
  updatePreviewError,
  updatePreviewLoading
}: {
  changes: Record<string, unknown>;
  deleteDisabled: boolean;
  deletePreview: PolicyImpactPreview | null;
  deletePreviewError: unknown;
  deletePreviewLoading: boolean;
  entries: SettingsCatalogEntry[];
  form: ChatPolicyForm;
  isNew: boolean;
  onChange: (form: ChatPolicyForm) => void;
  onChatIdChange: (value: string) => void;
  onDelete: () => void;
  onSave: () => void;
  saveDisabled: boolean;
  selectedChatId: string;
  updatePreview: PolicyImpactPreview | null;
  updatePreviewError: unknown;
  updatePreviewLoading: boolean;
}) {
  return (
    <div className="detail-panel">
      <p className="eyebrow">Chat Policy Editor</p>
      <div className="detail-title-row">
        <h2>{isNew ? "New chat policy" : selectedChatId}</h2>
        <Badge tone={Object.keys(changes).length ? "warning" : "success"}>{Object.keys(changes).length ? "Unsaved" : "Current"}</Badge>
      </div>
      {isNew ? (
        <label className="field-control">
          <span>Chat ID</span>
          <input onChange={(event) => onChatIdChange(event.target.value)} placeholder="oc_xxx" value={selectedChatId} />
        </label>
      ) : null}
      <div className="policy-form-grid">
        {chatFields.map((field) => (
          <PolicyField
            entry={entryFor(entries, field.catalogKey)}
            key={field.key}
            type={field.type}
            value={form[field.key]}
            onChange={(value) => onChange({ ...form, [field.key]: value })}
          />
        ))}
      </div>
      <ChangePreview changes={changes} entries={entries} fieldDefs={chatFields} />
      <PolicyImpactPreviewPanel
        error={updatePreviewError}
        isLoading={updatePreviewLoading}
        preview={updatePreview}
        title="Save impact preview"
      />
      {!isNew ? (
        <PolicyImpactPreviewPanel
          error={deletePreviewError}
          isLoading={deletePreviewLoading}
          preview={deletePreview}
          title="Delete fallback preview"
        />
      ) : null}
      <div className="command-buttons">
        <Button disabled={saveDisabled || Object.keys(changes).length === 0} onClick={onSave} tone="success">
          <Save aria-hidden="true" size={15} />
          Save chat policy
        </Button>
        {!isNew ? (
          <Button disabled={deleteDisabled} onClick={onDelete} tone="danger">
            <Trash2 aria-hidden="true" size={15} />
            Delete chat policy
          </Button>
        ) : null}
      </div>
      {!saveDisabled ? null : <p className="detail-note">Chat policy updates require initialized Product Policy Store and a chat ID.</p>}
    </div>
  );
}

function PolicyField({
  entry,
  type,
  value,
  onChange
}: {
  entry: SettingsCatalogEntry;
  type: "boolean" | "identity" | "text";
  value: boolean | string;
  onChange: (value: boolean | string) => void;
}) {
  if (type === "boolean") {
    return (
      <label className="toggle-field">
        <input checked={Boolean(value)} onChange={(event) => onChange(event.target.checked)} type="checkbox" />
        <FieldText entry={entry} />
      </label>
    );
  }
  if (type === "identity") {
    return (
      <label className="select-field">
        <FieldText entry={entry} />
        <select onChange={(event) => onChange(event.target.value)} value={String(value)}>
          <option value="bot_preferred">bot_preferred</option>
          <option value="bot">bot</option>
          <option value="user">user</option>
        </select>
      </label>
    );
  }
  return (
    <label className="field-control">
      <FieldText entry={entry} />
      <input onChange={(event) => onChange(event.target.value)} value={String(value)} />
    </label>
  );
}

function FieldText({ entry }: { entry: SettingsCatalogEntry }) {
  return (
    <span className="field-copy">
      <strong>{entry.label}</strong>
      <small>{entry.description}</small>
      {entry.help ? <em>{entry.help}</em> : null}
    </span>
  );
}

function ChangePreview({
  changes,
  entries,
  fieldDefs
}: {
  changes: Record<string, unknown>;
  entries: SettingsCatalogEntry[];
  fieldDefs: Array<{ key: string; catalogKey: string }>;
}) {
  const rows = Object.entries(changes);
  return (
    <div className="policy-change-preview">
      <div className="subsection-title">
        <FileDiff aria-hidden="true" size={16} />
        <h2>Unsaved policy diff</h2>
      </div>
      {rows.length ? (
        <ul className="change-list">
          {rows.map(([key, value]) => {
            const catalogKey = fieldDefs.find((field) => field.key === key)?.catalogKey ?? key;
            return (
              <li key={key}>
                <span>{entryFor(entries, catalogKey).label}</span>
                <strong>{formatSettingValue(value)}</strong>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="detail-note">No local changes. Background refresh will keep the editor current while the form is untouched.</p>
      )}
    </div>
  );
}

function PolicyImpactPreviewPanel({
  title,
  preview,
  isLoading,
  error
}: {
  title: string;
  preview: PolicyImpactPreview | null;
  isLoading: boolean;
  error: unknown;
}) {
  if (!preview && !isLoading && !error) {
    return null;
  }
  return (
    <div className="policy-impact-preview">
      <div className="subsection-title">
        <FileDiff aria-hidden="true" size={16} />
        <h2>{title}</h2>
      </div>
      {isLoading ? <p className="detail-note">Loading deterministic policy impact...</p> : null}
      {error ? <p className="detail-note">Preview unavailable: {errorMessage(error)}</p> : null}
      {preview ? (
        <>
          <ImpactChangeList title="Field changes" changes={preview.field_changes} />
          <ImpactChangeList title="Behavior changes" changes={preview.behavior_changes} />
          <ImpactSummary summary={preview.affected_summary} />
          {preview.warnings.length ? (
            <ul className="warning-list">
              {preview.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function ImpactChangeList({
  title,
  changes
}: {
  title: string;
  changes: PolicyImpactPreview["behavior_changes"];
}) {
  return (
    <div className="impact-section">
      <h3>{title}</h3>
      {changes.length ? (
        <ul className="change-list">
          {changes.map((change) => (
            <li key={`${change.subject ?? "field"}:${change.field}`}>
              <span>{impactLabel(change)}</span>
              <strong>
                {formatSettingValue(change.before)}
                {" -> "}
                {formatSettingValue(change.after)}
              </strong>
            </li>
          ))}
        </ul>
      ) : (
        <p className="detail-note">No deterministic behavior change detected.</p>
      )}
    </div>
  );
}

function ImpactSummary({ summary }: { summary: Record<string, unknown> }) {
  const rows = Object.entries(summary);
  if (!rows.length) {
    return null;
  }
  return (
    <div className="impact-section">
      <h3>Affected summary</h3>
      <FieldList>
        {rows.map(([key, value]) => (
          <FactRow key={key} label={key} value={formatSettingValue(value)} />
        ))}
      </FieldList>
    </div>
  );
}

function ImportDiffDetails({ diff }: { diff: SettingsRuntime["policy_status"]["policy_import_diff"] }) {
  return (
    <div className="policy-import-detail">
      <div className="subsection-title">
        <FileDiff aria-hidden="true" size={16} />
        <h2>Policy Import Diff detail</h2>
      </div>
      <FieldList>
        <FactRow label="Missing global" value={diff?.missing_global ? "yes" : "no"} />
        <FactRow label="Changed global" value={diff?.changed_global ? "yes" : "no"} />
        <FactRow label="Missing chats" value={(diff?.missing_chats ?? []).join(", ") || "none"} />
        <FactRow label="Changed chats" value={(diff?.changed_chats ?? []).join(", ") || "none"} />
      </FieldList>
    </div>
  );
}

function impactLabel(change: PolicyImpactPreview["behavior_changes"][number]): string {
  return change.subject ? `${change.subject}.${change.field}` : change.field;
}

function PolicyAuditHistory({
  audits,
  error,
  filterKey,
  filterScope,
  isLoading,
  onFilterKeyChange,
  onFilterScopeChange,
  usingFallback
}: {
  audits: SettingsRuntime["policy_audit_history"];
  error: unknown;
  filterKey: string;
  filterScope: AuditScopeFilter;
  isLoading: boolean;
  onFilterKeyChange: (value: string) => void;
  onFilterScopeChange: (value: AuditScopeFilter) => void;
  usingFallback: boolean;
}) {
  return (
    <div className="detail-panel">
      <div className="subsection-title">
        <History aria-hidden="true" size={16} />
        <h2>Policy audit history</h2>
      </div>
      <div className="audit-filter-row" aria-label="Policy audit filters">
        <label>
          <span>Scope</span>
          <select onChange={(event) => onFilterScopeChange(event.target.value as AuditScopeFilter)} value={filterScope}>
            <option value="all">All scopes</option>
            <option value="global">Global</option>
            <option value="chat">Chat</option>
          </select>
        </label>
        <label>
          <span>Policy key</span>
          <input onChange={(event) => onFilterKeyChange(event.target.value)} placeholder="reply_policy or chat:oc_xxx" value={filterKey} />
        </label>
      </div>
      {isLoading ? <p className="detail-note">Loading filtered policy audits...</p> : null}
      {error ? <p className="detail-note">Filtered audits unavailable. Showing recent runtime history.</p> : null}
      {usingFallback ? <p className="detail-note">Showing recent runtime history until filtered audits load.</p> : null}
      {audits.length ? (
        <ul className="audit-list">
          {audits.map((audit) => (
            <li key={audit.id}>
              <div className="audit-row-head">
                <Badge tone="info">{audit.scope}</Badge>
                <strong>{audit.policy_key}</strong>
                <span>{formatDate(audit.created_at)}</span>
              </div>
              <p>{audit.reason || "No reason recorded"}</p>
              <FieldList>
                <FactRow label="Actor" value={audit.actor} />
                <FactRow label="Old" value={shortText(formatSettingValue(audit.old_summary), "empty")} />
                <FactRow label="New" value={shortText(formatSettingValue(audit.new_summary), "empty")} />
              </FieldList>
              <details>
                <summary>Summary JSON</summary>
                <JsonBlock value={{ old_summary: audit.old_summary, new_summary: audit.new_summary }} />
              </details>
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState title="No policy audits" detail="Import and policy update commands will appear here after they write audit records." />
      )}
    </div>
  );
}

function FactTile({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="metric-card neutral compact">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function FactRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function entryFor(entries: SettingsCatalogEntry[], key: string): SettingsCatalogEntry {
  return (
    entries.find((entry) => entry.key === key) ?? {
      key,
      label: key,
      description: "Catalog metadata unavailable.",
      help: null,
      source: "derived",
      scope: "policy",
      visibility: "normal",
      editable_v1: true,
      requires_restart: false,
      audit_behavior: "policy_audits",
      write_boundary: null
    }
  );
}

function globalFormFromPolicy(policy: ProductPolicy | null): GlobalPolicyForm {
  return {
    p2p_auto_reply: policy?.reply_policy?.p2p_auto_reply ?? false,
    unknown_group_auto_reply: policy?.reply_policy?.unknown_group_auto_reply ?? false,
    bot_joined: policy?.default_chat_policy?.bot_joined ?? false,
    reply_identity: policy?.default_chat_policy?.reply_identity ?? "bot_preferred",
    allow_user_fallback: policy?.default_chat_policy?.allow_user_fallback ?? true,
    resource_download: policy?.default_chat_policy?.resource_download ?? true
  };
}

function chatFormFromPolicy(policy: ChatPolicy | null): ChatPolicyForm {
  return {
    name: policy?.name ?? "",
    auto_reply: policy?.auto_reply ?? false,
    bot_joined: policy?.bot_joined ?? false,
    reply_identity: policy?.reply_identity ?? "bot_preferred",
    allow_user_fallback: policy?.allow_user_fallback ?? true,
    resource_download: policy?.resource_download ?? true
  };
}

function diffObject<T extends Record<string, unknown>>(baseline: T, current: T): Partial<T> {
  const changes: Partial<T> = {};
  for (const key of Object.keys(current) as Array<keyof T>) {
    if (current[key] !== baseline[key]) {
      changes[key] = current[key];
    }
  }
  return changes;
}

function clean(value: string): string | undefined {
  const trimmed = value.trim();
  return trimmed || undefined;
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
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Request failed.";
}

function errorResult(command: string, error: unknown): CommandResult {
  return {
    status: "failed",
    command,
    actor: "local_console",
    reason: null,
    target: {},
    changed: false,
    result: { error: error instanceof Error ? error.message : "Request failed." },
    warnings: [],
    next_actions: []
  };
}
