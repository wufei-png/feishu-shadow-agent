import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowUpRight, Bell, CheckCircle2, ClipboardList, RefreshCw, Send, ShieldCheck } from "lucide-react";
import { expireApprovals, getDashboard } from "../api";
import {
  Badge,
  Button,
  CommandResultPanel,
  EmptyState,
  ErrorState,
  formatDate,
  ListRow,
  LoadingState,
  SectionHeader,
  shortText,
  statusTone
} from "../components/Primitives";
import { invalidateAfterMaintenanceCommand, queryKeys } from "../queryKeys";
import type { ApprovalSummary, AttentionTask, BotMembershipStatus, CommandResult, DispatchActionSummary, IngestionStatus, RouteKey } from "../types";

export function DashboardScreen({ token, navigate }: { token: string; navigate: (route: RouteKey, selectedId?: string, view?: "attention" | "all") => void }) {
  const queryClient = useQueryClient();
  const dashboard = useQuery({
    queryKey: queryKeys.dashboard(),
    queryFn: () => getDashboard(token),
    enabled: Boolean(token),
    refetchInterval: 15_000
  });
  const expire = useMutation({
    mutationFn: () => expireApprovals(token, { reason: "operator console maintenance" }),
    onSuccess: async () => invalidateAfterMaintenanceCommand(queryClient)
  });

  if (dashboard.isLoading) {
    return <LoadingState title="正在读取待办" />;
  }
  if (dashboard.error && !dashboard.data) {
    return <ErrorState title="待办工作台不可用" error={dashboard.error} />;
  }

  const snapshot = dashboard.data;
  const pendingApprovals = snapshot?.pending_approvals ?? [];
  const recoveryActions = snapshot?.failed_or_needs_review_actions ?? [];
  const staleSendingActions = snapshot?.stale_sending_actions ?? [];
  const attentionTasks = snapshot?.attention_tasks ?? [];
  const attention = snapshot?.attention_summary;
  const healthIssueCount = snapshot?.health_issue_summary?.open_issue_count ?? 0;
  const recentErrors = snapshot?.recent_errors ?? [];
  const policyStatus = snapshot?.policy_status;
  const policyDiff = policyStatus?.policy_import_diff;
  const policyNeedsAttention = policyStatus?.initialized === false || policyDiff?.status === "differs";
  const actionCount = attention?.total_item_count ?? 0;
  const sendAttention = (attention?.uncertain_action_count ?? 0) + (attention?.failed_action_count ?? 0);
  const blockedOrFailed = (attention?.blocked_processing_count ?? 0) + (attention?.failed_processing_count ?? 0);

  return (
    <section className="work-grid" aria-label="待我处理">
      <div className="work-main">
        <div className="queue-panel dashboard-overview">
          <SectionHeader
            eyebrow="人工决策"
            title="待我处理"
            badge={<Badge tone={actionCount ? "warning" : "success"}>{actionCount ? `${actionCount} 项` : "已清空"}</Badge>}
          >
            <p className="section-note">先核实发送结果，再处理审批与任务阻塞。下方数量来自完整数据，不受预览条数影响。</p>
            <div className="command-buttons">
              <Button disabled={dashboard.isFetching} onClick={() => void dashboard.refetch()}>
                <RefreshCw aria-hidden="true" size={15} />
                {dashboard.isFetching ? "刷新中…" : "刷新"}
              </Button>
              <span className="detail-note">更新于 {dashboard.dataUpdatedAt ? new Date(dashboard.dataUpdatedAt).toLocaleString("zh-CN") : "尚未更新"}</span>
            </div>
            {dashboard.error ? <p className="detail-note danger">刷新失败，当前展示缓存数据。</p> : null}
          </SectionHeader>
          <div aria-label="待办索引" className="decision-index">
            <DecisionLink count={sendAttention} hint="查看发送记录" label="发送待核实或失败" onClick={() => navigate("dispatch", undefined, "attention")} tone="danger" />
            <DecisionLink count={attention?.pending_approval_count ?? 0} hint="查看审批" label="待审批" onClick={() => navigate("approvals")} tone="warning" />
            <DecisionLink count={blockedOrFailed} hint="查看全部任务" label="处理阻塞或失败" onClick={() => navigate("tasks", undefined, "all")} tone="danger" />
          </div>
          <p className="decision-context">涉及 {attention?.affected_task_count ?? 0} 个任务</p>
          {actionCount === 0 ? <EmptyState title="当前没有待处理事项" detail="运行状态、策略和健康入口仍保留在右侧。" /> : null}
        </div>

        <AttentionTasks tasks={attentionTasks} onTask={(taskId) => navigate("tasks", taskId)} />
        <PreviewList
          approvals={pendingApprovals}
          actions={recoveryActions}
          staleActions={staleSendingActions}
          onApproval={(approvalId) => navigate("approvals", approvalId)}
          onAction={(actionId) => navigate("dispatch", String(actionId))}
        />
      </div>

      <aside className="work-detail">
        <IngestionPanel status={snapshot?.ingestion_status} />
        <MembershipPanel status={snapshot?.bot_membership_status} />

        <div className="detail-panel">
          <p className="eyebrow">运行边界</p>
          <h2>运行时策略</h2>
          <dl className="fact-list">
            <div><dt>初始化</dt><dd><Badge tone={policyStatus?.initialized ? "success" : "warning"}>{policyStatus?.initialized ? "已完成" : "缺失"}</Badge></dd></div>
            <div><dt>策略导入差异</dt><dd><Badge tone={statusTone(policyDiff?.status)}>{policyDiff?.status === "matches" ? "一致" : policyDiff?.status === "differs" ? "有差异" : "未知"}</Badge></dd></div>
            <div><dt>上次 tick</dt><dd>{formatDate(snapshot?.last_run?.last_tick_finished_at)}</dd></div>
          </dl>
          {policyDiff?.message ? <p className="detail-note">{policyDiff.message}</p> : null}
          {policyNeedsAttention ? <div className="quiet-empty"><AlertTriangle aria-hidden="true" size={18} /><span>策略需要检查</span></div> : null}
          <div className="command-buttons">
            <Button onClick={() => navigate("settings")}>打开设置</Button>
            <Button onClick={() => navigate("health")}>打开健康详情</Button>
          </div>
        </div>

        <div className="detail-panel">
          <p className="eyebrow">维护操作</p>
          <h2>审批过期处理</h2>
          <p className="detail-note">读取不会改变状态；只有点击命令才会显式过期超时审批。</p>
          <Button disabled={expire.isPending} onClick={() => expire.mutate()} tone="warning">过期超时审批</Button>
          <CommandResultPanel result={(expire.data as CommandResult | undefined) ?? null} />
        </div>

        <div className="detail-panel">
          <p className="eyebrow">近期信号</p>
          <h2>最近错误与审计信号</h2>
          {recentErrors.length ? (
            <ul className="timeline-list">
              {recentErrors.slice(0, 5).map((item, index) => (
                <li key={`${String(item.type)}-${index}`}><AlertTriangle aria-hidden="true" size={14} /><span>{String(item.message ?? item.type ?? "recent issue")}</span><small>{String(item.status ?? "")}</small></li>
              ))}
            </ul>
          ) : <div className="quiet-empty"><CheckCircle2 aria-hidden="true" size={18} /><span>没有近期失败命令</span></div>}
          <p className="detail-note">健康问题：{healthIssueCount}</p>
        </div>
      </aside>
    </section>
  );
}

function MembershipPanel({ status }: { status: BotMembershipStatus | undefined }) {
  const attention = status?.facts.filter((fact) => fact.status !== "present") ?? [];
  return (
    <div className="detail-panel">
      <p className="eyebrow">群成员状态</p>
      <div className="detail-title-row">
        <h2>机器人群成员状态</h2>
        <Badge tone={status?.summary.absent ? "danger" : status?.summary.unknown || status?.summary.unobserved ? "warning" : "success"}>
          离群 {status?.summary.absent ?? 0} / 未知 {status?.summary.unknown ?? 0} / 待探测 {status?.summary.unobserved ?? 0}
        </Badge>
      </div>
      {attention.length ? (
        <ul className="timeline-list">
          {attention.slice(0, 5).map((fact) => (
            <li key={fact.chat_id}>
              <AlertTriangle aria-hidden="true" size={14} />
              <span>{fact.chat_id}</span>
              <small>{fact.status} · {fact.source ?? "来源未知"} · {formatDate(fact.checked_at)}</small>
            </li>
          ))}
        </ul>
      ) : <div className="quiet-empty"><CheckCircle2 aria-hidden="true" size={18} /><span>没有已知的机器人离群或探测失败</span></div>}
    </div>
  );
}

function IngestionPanel({ status }: { status: IngestionStatus | undefined }) {
  const summary = status?.summary;
  const backlogs = status?.sources.filter((source) => !source.drain_complete) ?? [];
  return (
    <div className="detail-panel">
      <p className="eyebrow">消息摄取</p>
      <div className="detail-title-row">
        <h2>摄取积压</h2>
        <Badge tone={summary?.backlog_count ? "warning" : "success"}>{summary?.backlog_count ?? 0} 个来源</Badge>
      </div>
      <dl className="fact-list">
        <div><dt>最早检查点</dt><dd>{formatAge(summary?.oldest_checkpoint_age_seconds)}</dd></div>
        <div><dt>预算耗尽</dt><dd>{summary?.budget_exhausted_count ?? 0}</dd></div>
      </dl>
      {backlogs.length ? (
        <ul className="timeline-list">
          {backlogs.slice(0, 5).map((source) => (
            <li key={source.checkpoint_key}>
              <AlertTriangle aria-hidden="true" size={14} />
              <span>{source.checkpoint_key}</span>
              <small>{source.backlog?.reason ?? "deferred"} · {source.backlog?.pages_fetched ?? 0} 页 / {source.backlog?.messages_fetched ?? 0} 条</small>
            </li>
          ))}
        </ul>
      ) : <div className="quiet-empty"><CheckCircle2 aria-hidden="true" size={18} /><span>当前没有摄取积压</span></div>}
    </div>
  );
}

function formatAge(seconds: number | null | undefined): string {
  if (seconds == null) {
    return "尚无成功 checkpoint";
  }
  if (seconds < 60) {
    return `${seconds} 秒`;
  }
  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)} 分钟`;
  }
  return `${Math.floor(seconds / 3600)} 小时`;
}

function DecisionLink({ count, hint, label, onClick, tone }: {
  count: number;
  hint: string;
  label: string;
  onClick: () => void;
  tone: "danger" | "warning";
}) {
  return (
    <button className={`decision-link ${count ? tone : "quiet"}`} onClick={onClick} type="button">
      <span className="decision-label">{label}</span>
      <strong>{count}</strong>
      <span className="decision-hint">{hint}<ArrowUpRight aria-hidden="true" size={15} /></span>
    </button>
  );
}

function AttentionTasks({ tasks, onTask }: { tasks: AttentionTask[]; onTask: (taskId: string) => void }) {
  if (!tasks.length) {
    return null;
  }
  return (
    <section className="queue-panel">
      <div className="subsection-title"><ClipboardList aria-hidden="true" size={16} /><h2>按任务归集</h2></div>
      <div className="list-stack">
        {tasks.map((task) => (
          <ListRow
            badge={<Badge tone="warning">{attentionTaskCount(task)} 项</Badge>}
            key={task.task_id}
            meta={`${task.chat_id ?? "chat 未记录"} · ${formatDate(task.latest_at)}`}
            onClick={() => onTask(task.task_short_id)}
            selected={false}
            title={task.task_label || task.task_short_id}
          >
            <span className="row-preview">
              待审批 {task.pending_approval_count} · 不确定发送 {task.uncertain_action_count} · 发送失败 {task.failed_action_count} · 处理阻塞/失败 {task.blocked_processing_count + task.failed_processing_count}
            </span>
          </ListRow>
        ))}
      </div>
    </section>
  );
}

function attentionTaskCount(task: AttentionTask): number {
  return task.pending_approval_count + task.failed_action_count + task.uncertain_action_count + task.blocked_processing_count + task.failed_processing_count;
}

function PreviewList({ approvals, actions, staleActions, onApproval, onAction }: {
  approvals: ApprovalSummary[];
  actions: DispatchActionSummary[];
  staleActions: DispatchActionSummary[];
  onApproval: (approvalId: string) => void;
  onAction: (actionId: number) => void;
}) {
  const dispatchRows = [...actions, ...staleActions].filter(
    (action, index, rows) => rows.findIndex((candidate) => candidate.action_id === action.action_id) === index
  );
  return (
    <div className="split-panels">
      <section className="queue-panel">
        <div className="subsection-title"><Bell aria-hidden="true" size={16} /><h2>待审批预览</h2></div>
        {approvals.length ? (
          <div className="list-stack">
            {approvals.slice(0, 6).map((approval) => (
              <ListRow
                badge={<Badge tone={approval.is_overdue ? "danger" : statusTone(approval.status)}>{approval.is_overdue ? "已超时" : approval.status}</Badge>}
                key={approval.approval_id}
                meta={`${approval.task_short_id ?? "未关联任务"} · source ${approval.source_message_id ?? "unknown"} · ${formatDate(approval.created_at)}`}
                onClick={() => onApproval(approval.approval_id)}
                selected={false}
                title={approval.approval_id}
              ><span className="row-preview">建议回复：{shortText(approval.preview)}</span></ListRow>
            ))}
          </div>
        ) : <EmptyState title="没有待审批" detail="需要 owner 决策的回复会显示在这里。" />}
      </section>

      <section className="queue-panel">
        <div className="subsection-title"><Send aria-hidden="true" size={16} /><h2>发送异常预览</h2></div>
        {dispatchRows.length ? (
          <div className="list-stack">
            {dispatchRows.slice(0, 6).map((action) => (
              <ListRow
                badge={<Badge tone={statusTone(action.status)}>{action.status}</Badge>}
                key={action.action_id}
                meta={`${action.task_short_id ?? "未关联任务"} · 目标 ${action.target_message_id ?? "unknown"} · ${formatDate(action.updated_at)}`}
                onClick={() => onAction(action.action_id)}
                selected={false}
                title={`发送记录 ${action.action_id}`}
              ><span className="row-preview">发送状态：{action.status}</span></ListRow>
            ))}
          </div>
        ) : <EmptyState title="没有发送异常" detail="失败或结果不确定的发送会显示在这里。" />}
      </section>

      <section className="queue-panel full-span">
        <div className="subsection-title"><ShieldCheck aria-hidden="true" size={16} /><h2>处理说明</h2></div>
        <p className="detail-note">点击具体审批或发送动作进入专业详情；任务页用于查看触发消息、上下文和关联对象。</p>
      </section>
    </div>
  );
}
