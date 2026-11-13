import type { PricePayload } from "../types";
import { fmtDateTime, timeAgo } from "../format";

export function TopBar({ prices, busy, onRefresh, onUseLive, onUpload }: {
  prices: PricePayload | null; busy: null | "refresh" | "upload"; onRefresh: () => void; onUseLive: () => void; onUpload: () => void;
}) {
  const upload = prices?.source_kind === "upload";
  const refreshLabel = prices?.status.mode === "cached" ? "last successful refresh" : upload ? "loaded" : "refreshed";
  return (
    <header className="topbar">
      <div className="brand">
        <img className="mark" src="/mark.png" alt="" width={30} height={30} />
        <span className="wordmark">AI Gold Forecast</span>
        <span className="sym" title="COMEX gold futures, front-month continuous">GC=F / COMEX</span>
      </div>
      <div className="top-right">
        {prices && (
          <div className="top-status" data-testid="top-status">
            <span className={`status ${prices.status.mode}`} data-testid="data-status"><i />{prices.status.label}</span>
            <span className="mono muted small" data-testid="refresh-time" title={prices.fetched_at}>
              {refreshLabel} {fmtDateTime(prices.fetched_at)} · {timeAgo(prices.fetched_at)}
            </span>
          </div>
        )}
        <div className="actions">
          {upload
            ? <button className="btn" onClick={onUseLive} disabled={busy !== null} data-testid="use-live">{busy === "refresh" ? "Switching…" : "Use live data"}</button>
            : <button className="btn" onClick={onRefresh} disabled={busy !== null} data-testid="refresh">{busy === "refresh" ? "Refreshing…" : "Refresh"}</button>}
          <button className="btn" onClick={onUpload} disabled={busy !== null} data-testid="upload">{busy === "upload" ? "Uploading…" : "Upload CSV"}</button>
        </div>
      </div>
    </header>
  );
}
