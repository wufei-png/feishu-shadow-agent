import type { ReactNode } from "react";
import { AlertTriangle, CheckCircle2, Loader2, RotateCcw } from "lucide-react";
import type { CommandResult, Tone } from "../types";

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: Tone }) {
  return <span className={`status-pill ${tone}`}>{children}</span>;
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

export function EmptyState({
  title,
  detail,
  tone = "muted"
}: {
  title: string;
  detail: string;
  tone?: Tone;
}) {
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

export function LoadingState({ title = "Loading", detail = "Reading local operator state." }) {
  return (
    <div className="empty-state">
      <Loader2 aria-hidden="true" className="spin" size={18} />
      <div>
        <h1>{title}</h1>
        <p>{detail}</p>
      </div>
    </div>
  );
}

export function ErrorState({ title, error }: { title: string; error: unknown }) {
  return (
    <div className="empty-state">
      <AlertTriangle aria-hidden="true" className="danger" size={18} />
      <div>
        <h1>{title}</h1>
        <p>{error instanceof Error ? error.message : "Request failed."}</p>
      </div>
    </div>
  );
}

export function CommandResultPanel({ result }: { result: CommandResult | null }) {
  if (!result) {
    return null;
  }
  const tone = commandTone(result.status);
  return (
    <section className={`command-result ${tone}`} aria-live="polite">
      <div className="command-result-title">
        {tone === "success" ? <CheckCircle2 aria-hidden="true" size={16} /> : <AlertTriangle aria-hidden="true" size={16} />}
        <span>{result.command}</span>
        <Badge tone={tone}>{result.status}</Badge>
      </div>
      <dl className="compact-facts">
        <Fact label="Changed" value={result.changed ? "yes" : "no"} />
        <Fact label="Actor" value={result.actor} />
        {result.reason ? <Fact label="Reason" value={result.reason} /> : null}
      </dl>
      {result.warnings.length ? (
        <ul className="warning-list">
          {result.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}
      <JsonBlock value={result.result} />
    </section>
  );
}

export function SectionHeader({
  eyebrow,
  title,
  badge,
  children
}: {
  eyebrow: string;
  title: string;
  badge?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="section-header">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        {children}
      </div>
      {badge}
    </div>
  );
}

export function Fact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value ?? "not recorded"}</dd>
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
  onClick: () => void;
  children?: ReactNode;
}) {
  return (
    <button className="list-row" data-selected={selected ? "true" : undefined} onClick={onClick} type="button">
      <span className="list-row-main">
        <strong>{title}</strong>
        {meta ? <small>{meta}</small> : null}
        {children}
      </span>
      {badge}
    </button>
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
  options: Array<{ value: T; label: string }>;
  onChange: (value: T) => void;
}) {
  return (
    <div className="segmented-control" aria-label={label}>
      {options.map((option) => (
        <button
          aria-pressed={value === option.value}
          key={option.value}
          onClick={() => onChange(option.value)}
          type="button"
        >
          {option.label}
        </button>
      ))}
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
    return "not recorded";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

export function shortText(value: string | null | undefined, fallback = "No preview"): string {
  const text = (value ?? "").trim();
  if (!text) {
    return fallback;
  }
  return text.length > 160 ? `${text.slice(0, 157)}...` : text;
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
