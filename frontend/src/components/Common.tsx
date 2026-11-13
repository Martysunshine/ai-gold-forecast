import type { ReactNode } from "react";

export const Skeleton = ({ h = 20, w = "100%" }: { h?: number; w?: number | string }) => (
  <div className="skeleton" style={{ height: h, width: w }} aria-hidden="true" />
);

export function CardSkeleton({ lines = 3, label }: { lines?: number; label: string }) {
  return (
    <div className="card" role="status" aria-busy="true" aria-label={label}>
      <Skeleton h={14} w="40%" />
      <div style={{ height: 14 }} />
      <Skeleton h={36} w="60%" />
      {Array.from({ length: lines }).map((_, i) => (<div key={i} style={{ marginTop: 12 }}><Skeleton h={12} w={`${90 - i * 15}%`} /></div>))}
    </div>
  );
}

export function ErrorCard({ title, message, onRetry }: { title: string; message: string; onRetry?: () => void }) {
  return (
    <div className="card error-card" role="alert">
      <h3>{title}</h3>
      <p>{message}</p>
      {onRetry && <button className="btn" onClick={onRetry}>Try again</button>}
    </div>
  );
}

export function Banner({ kind, children, onClose }: { kind: "warn" | "error" | "info"; children: ReactNode; onClose?: () => void }) {
  return (
    <div className={`banner ${kind}`} role={kind === "error" ? "alert" : "status"}>
      <span className="banner-icon" aria-hidden="true">{kind === "info" ? "i" : "!"}</span>
      <div>{children}</div>
      {onClose && <button className="x" onClick={onClose} aria-label="Dismiss">×</button>}
    </div>
  );
}

export function EmptyState({ message, busy, onUpload, onRetry }: { message: string; busy: boolean; onUpload: () => void; onRetry: () => void }) {
  return (
    <section className="card empty" aria-labelledby="empty-title">
      <div className="empty-mark" aria-hidden="true">Au</div>
      <h2 id="empty-title">No gold price data yet</h2>
      <p>{message}</p>
      <p className="muted">
        Upload a CSV with a <code>Date</code> column and a <code>Close</code> column (daily, USD per troy ounce, at least 400 rows).
        Nothing is substituted when data is missing.
      </p>
      <div className="row">
        <button className="btn primary" onClick={onUpload} disabled={busy}>Upload CSV</button>
        <button className="btn" onClick={onRetry} disabled={busy}>{busy ? "Retrying…" : "Retry live data"}</button>
      </div>
    </section>
  );
}
