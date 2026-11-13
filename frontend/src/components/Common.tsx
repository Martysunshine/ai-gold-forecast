import type { ReactNode } from "react";

export const Skeleton = ({ h = 16, w = "100%" }: { h?: number; w?: number | string }) => (
  <div className="skeleton" style={{ height: h, width: w }} aria-hidden="true" />
);

/** Focusable "?" that defines a metric. Works on hover, keyboard focus and touch (tap focuses it). */
export function Info({ text, label }: { text: string; label: string }) {
  return (
    <span className="info" tabIndex={0} role="note" aria-label={`${label}: ${text}`}>
      <span aria-hidden="true">?</span>
      <span className="info-tip" role="tooltip">{text}</span>
    </span>
  );
}

export function Banner({ kind, children, onClose }: { kind: "warn" | "error" | "info"; children: ReactNode; onClose?: () => void }) {
  return (
    <div className={`banner ${kind}`} role={kind === "error" ? "alert" : "status"}>
      <span className="banner-mark" aria-hidden="true">{kind === "info" ? "i" : "!"}</span>
      <div>{children}</div>
      {onClose && <button className="x" onClick={onClose} aria-label="Dismiss">×</button>}
    </div>
  );
}

export function ErrorBlock({ title, message, onRetry }: { title: string; message: string; onRetry?: () => void }) {
  return (
    <div className="error-block" role="alert">
      <strong>{title}</strong>
      <p>{message}</p>
      {onRetry && <button className="btn" onClick={onRetry}>Retry</button>}
    </div>
  );
}

export function EmptyState({ message, busy, onUpload, onRetry }: { message: string; busy: boolean; onUpload: () => void; onRetry: () => void }) {
  return (
    <section className="empty panel" aria-labelledby="empty-title">
      <div className="empty-sym">GC=F</div>
      <h2 id="empty-title">No price data loaded</h2>
      <p>{message}</p>
      <p className="muted small">
        Upload a CSV with a <code>Date</code> column and a <code>Close</code> column (daily, USD per troy ounce, at least 500 rows).
        Nothing is substituted when data is missing.
      </p>
      <div className="row">
        <button className="btn primary" onClick={onUpload} disabled={busy}>Upload CSV</button>
        <button className="btn" onClick={onRetry} disabled={busy}>{busy ? "Retrying…" : "Retry live data"}</button>
      </div>
    </section>
  );
}
