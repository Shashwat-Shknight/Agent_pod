import { ReactNode, useEffect } from "react";
import { ApiError, errorMessage } from "../api/client";

export function PageHead({ eyebrow, title, subtitle, children }: { eyebrow: string; title: string; subtitle?: string; children?: ReactNode }) {
  return (
    <header className="page-head">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        {subtitle && <p>{subtitle}</p>}
      </div>
      {children && <div className="actions">{children}</div>}
    </header>
  );
}

export function Card({ title, subtitle, trailing, children, className = "" }: {
  title?: string; subtitle?: string; trailing?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || trailing) && (
        <div className="card-title">
          <div>
            {title && <h2>{title}</h2>}
            {subtitle && <p>{subtitle}</p>}
          </div>
          {trailing}
        </div>
      )}
      {children}
    </section>
  );
}

export type Tone = "green" | "amber" | "red" | "violet" | "blue" | "gray";

export function Badge({ tone = "gray", children }: { tone?: Tone | string; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function Kpi({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: string; tone: string }) {
  return (
    <div className={`kpi ${tone}`}>
      <small>{label}</small>
      <b>{value}</b>
      {hint && <span>{hint}</span>}
    </div>
  );
}

export function Banner({ tone, title, children, action }: { tone: "ok" | "warn" | "error" | "info"; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className={`banner ${tone}`} role={tone === "error" ? "alert" : "status"}>
      <div>
        <b>{title}</b>
        {children && <p>{children}</p>}
      </div>
      {action}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="row" aria-live="polite">
      <span className="spinner" aria-hidden />
      {label && <span className="muted small">{label}</span>}
    </span>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="center">
      <span className="spinner" />
      <span>{label}</span>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

/** Error panel for failed loads: explains, offers retry, and says what to do for common cases. */
export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const api = error instanceof ApiError ? error : null;
  const title =
    api?.status === 403 ? "You don't have access to this" :
    api?.status === 404 ? "Not found" :
    api?.code === "network" || api?.code === "server_unreachable" ? "Server not reachable" :
    api?.code === "database_unavailable" ? "Database unavailable" : "Could not load this page";
  return (
    <Banner tone={api?.status === 403 ? "warn" : "error"} title={title}
      action={onRetry && api?.status !== 403 && api?.status !== 404 ? <button className="btn sm" onClick={onRetry}>Retry</button> : undefined}>
      {errorMessage(error)}
    </Banner>
  );
}

const CHECK_TONES: Record<string, string> = { pass: "green", warn: "amber", fail: "red" };

export function Checklist({ items, empty }: { items: { label: string; status: string; tone?: string; detail: string }[]; empty: string }) {
  if (!items.length) return <p className="muted small">{empty}</p>;
  return (
    <div>
      {items.map((item, index) => (
        <div className="check-row" key={`${item.label}-${index}`}>
          <Badge tone={item.tone ?? CHECK_TONES[item.status] ?? "gray"}>{item.status}</Badge>
          <div>
            <div className="label">{item.label}</div>
            <div className="detail">{item.detail}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { id: T; label: string }[]; value: T; onChange: (id: T) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((tab) => (
        <button key={tab.id} role="tab" aria-selected={value === tab.id} className={`tab ${value === tab.id ? "active" : ""}`} onClick={() => onChange(tab.id)}>
          {tab.label}
        </button>
      ))}
    </div>
  );
}

export function Modal({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-back" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal aria-label={title}>
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  );
}

export function initials(name: string) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "?";
}

export const STATUS_TONE: Record<string, Tone> = {
  Pending: "amber", Approved: "green", "Ambient Scribe": "gray", "Clinical Review": "amber", Coding: "violet", Billed: "green",
};

export function money(value: number) {
  return value.toLocaleString("en-US", { style: "currency", currency: "USD" });
}
