import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import * as Tooltip from "@radix-ui/react-tooltip";
import { m } from "motion/react";
import { AlertTriangle, Check, CheckCircle2, CircleHelp, Copy, Loader2, RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";
import i18n, { formatDateTime } from "../i18n";
import { listItemVariants, panelTransition, panelVariants } from "../motionTokens";
import { commandLabel, enumLabel } from "../presentation";
import type { CommandResult, Tone } from "../types";
import { useSelectionSlide } from "./useSelectionSlide";

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: Tone }) {
  return <span className={`status-pill ${tone}`}>{children}</span>;
}

export function HelpTooltip({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Tooltip.Root>
      <Tooltip.Trigger asChild>
        <button aria-label={label} className="help-trigger" type="button">
          <CircleHelp aria-hidden="true" size={15} />
        </button>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content className="tooltip-content" collisionPadding={10} sideOffset={6}>
          {children}
          <Tooltip.Arrow className="tooltip-arrow" />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

export function CopyValue({ value, label }: { value: string; label?: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const accessibleLabel = label ?? t("common.value");

  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <span className="copy-value">
      <span>{value}</span>
      <button
        aria-label={copied ? t("common.copied", { label: accessibleLabel }) : t("common.copy", { label: accessibleLabel })}
        className="copy-trigger"
        onBlur={() => setCopied(false)}
        onClick={() => void copy()}
        title={copied ? t("common.copied", { label: accessibleLabel }) : t("common.copy", { label: accessibleLabel })}
        type="button"
      >
        {copied ? <Check aria-hidden="true" size={14} /> : <Copy aria-hidden="true" size={14} />}
      </button>
    </span>
  );
}

export function TechnicalDetails({ children, summary }: { children: ReactNode; summary?: string }) {
  const { t } = useTranslation();
  return (
    <details className="technical-details">
      <summary>{summary ?? t("common.technicalDetails")}</summary>
      {children}
    </details>
  );
}

export function Button({
  children,
  disabled = false,
  onClick,
  tone = "neutral",
  type = "button"
}: {
  children: ReactNode;
  disabled?: boolean;
  onClick?: () => void;
  tone?: Tone;
  type?: "button" | "submit";
}) {
  return (
    <button className={`button ${tone}`} disabled={disabled} onClick={onClick} type={type}>
      {children}
    </button>
  );
}

/**
 * Two-step confirmation for destructive commands.
 *
 * The first click arms the action and states exactly what will happen; the
 * second commits. An armed button disarms itself after a few seconds so a
 * half-pressed destructive command cannot sit waiting on screen, and Escape
 * cancels. This replaces `window.confirm`, which blocks the page, cannot be
 * styled, cannot be tested, and reports nothing when the operator cancels.
 */
export function ConfirmButton({
  children,
  confirmLabel,
  onConfirm,
  disabled = false,
  tone = "danger"
}: {
  children: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  disabled?: boolean;
  tone?: Tone;
}) {
  const { t } = useTranslation();
  const [armed, setArmed] = useState(false);

  useEffect(() => {
    if (!armed) {
      return;
    }
    const timer = window.setTimeout(() => setArmed(false), 6000);
    return () => window.clearTimeout(timer);
  }, [armed]);

  if (!armed) {
    return (
      <Button disabled={disabled} onClick={() => setArmed(true)} tone={tone}>
        {children}
      </Button>
    );
  }

  return (
    <span
      className="confirm-group"
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          setArmed(false);
        }
      }}
    >
      <span className="confirm-prompt" role="status">{confirmLabel}</span>
      <Button
        disabled={disabled}
        onClick={() => {
          setArmed(false);
          onConfirm();
        }}
        tone={tone}
      >
        {t("common.confirm")}
      </Button>
      <Button onClick={() => setArmed(false)}>{t("common.cancel")}</Button>
    </span>
  );
}

export function EmptyState({
  title,
  detail,
  tone = "muted"
}: {
  title: string;
  detail?: string;
  tone?: Tone;
}) {
  return (
    <div className="empty-state">
      <RotateCcw aria-hidden="true" className={tone} size={18} />
      <div>
        <h2>{title}</h2>
        {detail ? <p>{detail}</p> : null}
      </div>
    </div>
  );
}

export function LoadingState({ title, detail }: { title?: string; detail?: string }) {
  const { t } = useTranslation();
  return (
    <div className="empty-state">
      <Loader2 aria-hidden="true" className="spin" size={18} />
      <div>
        <h2>{title ?? t("common.loading")}</h2>
        <p>{detail ?? t("common.loadingDetail")}</p>
      </div>
    </div>
  );
}

export function ErrorState({ title, error }: { title: string; error: unknown }) {
  const { t } = useTranslation();
  const request = requestError(error);
  return (
    <div className="empty-state">
      <AlertTriangle aria-hidden="true" className="danger" size={18} />
      <div>
        <h2>{request?.status === 401 ? t("common.sessionExpired") : request?.status === 404 ? t("common.objectNotFound") : title}</h2>
        <p>{request?.message ?? (error instanceof Error ? error.message : t("common.requestFailed"))}</p>
        {request?.code ? <small>{request.code}</small> : null}
      </div>
    </div>
  );
}

export function QueueControls({
  pagination,
  isFetching,
  updatedAt,
  error,
  onRefresh
}: {
  pagination?: {
    page: number;
    hasNext: boolean;
    onPrevious: () => void;
    onNext: () => void;
  };
  isFetching: boolean;
  updatedAt: number;
  error: unknown;
  onRefresh: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div aria-live="polite" className="queue-controls">
      <div className="command-buttons">
        {pagination && (pagination.page > 0 || pagination.hasNext) ? (
          <>
            <Button disabled={pagination.page === 0 || isFetching} onClick={pagination.onPrevious}>{t("common.previousPage")}</Button>
            <Badge tone="muted">{t("common.page", { page: pagination.page + 1 })}</Badge>
            <Button disabled={!pagination.hasNext || isFetching} onClick={pagination.onNext}>{t("common.nextPage")}</Button>
          </>
        ) : null}
        <Button disabled={isFetching} onClick={onRefresh}>{isFetching ? t("common.refreshing") : t("common.refresh")}</Button>
        <span className="queue-updated">
          {updatedAt ? t("common.updatedAt", { time: formatDateTime(updatedAt) }) : t("common.notUpdated")}
        </span>
      </div>
      {error ? <p className="detail-note danger">{t("common.cachedRefreshError", { code: requestError(error)?.code ?? "request_failed" })}</p> : null}
    </div>
  );
}

export function CommandResultPanel({ result, summary }: { result: CommandResult | null; summary?: ReactNode }) {
  const { t } = useTranslation();
  if (!result) {
    return null;
  }
  const tone = commandTone(result.status);
  return (
    // A command result is the answer to something the operator just did, so it
    // rises into place rather than appearing between two frames.
    <m.section
      animate="visible"
      className={`command-result ${tone}`}
      initial="hidden"
      transition={panelTransition}
      variants={panelVariants}
      aria-live="polite"
    >
      <div className="command-result-title">
        {tone === "success" ? <CheckCircle2 aria-hidden="true" size={16} /> : <AlertTriangle aria-hidden="true" size={16} />}
        <span title={result.command}>{commandLabel(t, result.command)}</span>
        <Badge tone={tone}>{enumLabel(t, "status", result.status)}</Badge>
      </div>
      {summary}
      {typeof result.result.error === "string" ? <p className="detail-note">{result.result.error}</p> : null}
      {result.warnings.length ? (
        <ul className="warning-list">
          {result.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}
      <TechnicalDetails>
        <dl className="compact-facts">
          <Fact label={t("common.changed")} value={result.changed ? t("common.yes") : t("common.no")} />
          <Fact label={t("common.actor")} value={result.actor} />
          {result.reason ? <Fact label={t("common.reason")} value={result.reason} /> : null}
          <Fact label={t("common.rawCommand")} value={<code>{result.command}</code>} />
        </dl>
        <JsonBlock value={result.result} />
      </TechnicalDetails>
    </m.section>
  );
}

export function SectionHeader({
  as: Heading = "h1",
  eyebrow,
  title,
  badge,
  children
}: {
  as?: "h1" | "h2";
  eyebrow?: string;
  title: string;
  badge?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="section-header">
      <div>
        {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
        <Heading>{title}</Heading>
        {children}
      </div>
      {badge}
    </div>
  );
}

export function Fact({ label, value }: { label: string; value: ReactNode }) {
  const { t } = useTranslation();
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value ?? t("common.notRecorded")}</dd>
    </div>
  );
}

export function FieldList({ children }: { children: ReactNode }) {
  return <dl className="fact-list">{children}</dl>;
}

export function JsonBlock({ value }: { value: unknown }) {
  return <pre className="json-block">{JSON.stringify(value ?? {}, null, 2)}</pre>;
}

export function ListRow({
  selected,
  title,
  meta,
  badge,
  onClick,
  children
}: {
  selected: boolean;
  title: ReactNode;
  meta?: ReactNode;
  badge?: ReactNode;
  onClick: React.MouseEventHandler<HTMLButtonElement>;
  children?: ReactNode;
}) {
  return (
    <m.button
      animate="visible"
      className="list-row"
      data-selected={selected ? "true" : undefined}
      initial="hidden"
      onClick={onClick}
      transition={panelTransition}
      type="button"
      variants={listItemVariants}
    >
      <span className="list-row-main">
        <strong>{title}</strong>
        {meta ? <small>{meta}</small> : null}
        {children}
      </span>
      {badge}
    </m.button>
  );
}

export function SegmentedControl<T extends string>({
  label,
  value,
  options,
  onChange
}: {
  label: string;
  value: T;
  options: Array<{ value: T; label: string; leading?: ReactNode }>;
  onChange: (value: T) => void;
}) {
  const indicator = useSelectionSlide(value);
  return (
    <div className="segmented-control" aria-label={label} role="group">
      {options.map((option) => {
        const selected = value === option.value;
        return (
          <button
            aria-pressed={selected}
            key={option.value}
            onClick={() => onChange(option.value)}
            type="button"
          >
            {selected ? (
              <span aria-hidden="true" className="segmented-indicator" ref={indicator}>
                <span className="selection-rim"><span className="selection-rim-glint" /></span>
              </span>
            ) : null}
            <span className="segmented-option">{option.leading}{option.label}</span>
          </button>
        );
      })}
    </div>
  );
}

export function TextareaField({
  label,
  value,
  onChange,
  placeholder,
  rows = 3
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  rows?: number;
}) {
  return (
    <label className="field-control">
      <span>{label}</span>
      <textarea onChange={(event) => onChange(event.target.value)} placeholder={placeholder} rows={rows} value={value} />
    </label>
  );
}

export function TextField({
  label,
  value,
  onChange,
  placeholder
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="field-control">
      <span>{label}</span>
      <input onChange={(event) => onChange(event.target.value)} placeholder={placeholder} value={value} />
    </label>
  );
}

export function formatDate(value: string | null | undefined): string {
  if (!value) {
    return i18n.t("common.notRecorded");
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return formatDateTime(date);
}

export function shortText(value: string | null | undefined, fallback = i18n.t("common.noPreview")): string {
  const text = (value ?? "").trim();
  if (!text) {
    return fallback;
  }
  return text.length > 160 ? `${text.slice(0, 157)}...` : text;
}

function requestError(error: unknown): { status?: number; code?: string; message?: string } | null {
  if (!error || typeof error !== "object") {
    return null;
  }
  const value = error as { status?: unknown; code?: unknown; message?: unknown };
  return {
    status: typeof value.status === "number" ? value.status : undefined,
    code: typeof value.code === "string" ? value.code : undefined,
    message: typeof value.message === "string" ? value.message : undefined
  };
}

export function statusTone(status: string | null | undefined): Tone {
  if (!status) {
    return "muted";
  }
  if (["applied", "sent", "approved", "live", "matches"].includes(status)) {
    return "success";
  }
  if (["pending", "sending", "expired", "stale", "differs"].includes(status)) {
    return "warning";
  }
  if (["failed", "failed_needs_review", "rejected", "conflict", "validation_failed", "not_found"].includes(status)) {
    return "danger";
  }
  if (["no_change", "watching"].includes(status)) {
    return "info";
  }
  return "neutral";
}

function commandTone(status: string): Tone {
  if (status === "applied" || status === "no_change") {
    return "success";
  }
  if (status === "conflict") {
    return "warning";
  }
  return "danger";
}
