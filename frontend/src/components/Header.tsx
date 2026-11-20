import type { Conn, MarketSummary } from "../types";
import { fmtEt } from "../format";

export type PageId = "forecast" | "learning" | "paper" | "alerts";
export const PAGES: { id: PageId; label: string }[] = [
  { id: "forecast", label: "Forecast" }, { id: "learning", label: "Learning" }, { id: "paper", label: "Paper trading" }, { id: "alerts", label: "Alerts & settings" },
];

export function statusOf(s: MarketSummary | null, conn: Conn): { cls: string; label: string } {
  if (conn === "offline") return { cls: "bad", label: "Offline" };
  if (conn === "reconnecting") return { cls: "warn", label: "Reconnecting…" };
  if (!s) return { cls: "off", label: "Connecting…" };
  const m: Record<string, string> = { live: "ok", delayed: "warn", stale: "bad", closed: "off", unavailable: "off" };
  return { cls: m[s.status.state] ?? "off", label: s.status.label };
}

export function Header({ page, onPage, summary, conn, onDetails }: { page: PageId; onPage: (p: PageId) => void; summary: MarketSummary | null; conn: Conn; onDetails: () => void }) {
  const st = statusOf(summary, conn);
  return (
    <header className="gx-header">
      <h1 className="gx-brand">AI Gold Forecast <span className="gx-tag2 mono">{summary?.name?.toLowerCase().includes("spot") ? "XAU/USD spot" : "GC=F / COMEX"}</span></h1>
      <nav className="gx-nav" aria-label="Pages">
        {PAGES.map((p) => (
          <a key={p.id} href={`#/${p.id}`} className={page === p.id ? "on" : ""} aria-current={page === p.id ? "page" : undefined} onClick={(e) => { e.preventDefault(); onPage(p.id); }} data-testid={`nav-${p.id}`}>{p.label}</a>
        ))}
      </nav>
      <div className="gx-status" data-testid="conn-status" title={summary?.provider ?? ""}>
        <i className={`gx-dot ${st.cls}`} aria-hidden="true" />
        <span>{st.label}</span>
        {summary?.as_of_epoch ? <span className="gx-muted mono">· quote {fmtEt(summary.as_of_epoch)} ET</span> : null}
      </div>
      <button className="gx-icon" onClick={onDetails} aria-label="Details and methodology" data-testid="open-details" title="Details: model, methodology, drivers, backtests, news">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><circle cx="12" cy="12" r="3.2" /><path d="M12 2.8v2.4M12 18.8v2.4M4.9 4.9l1.7 1.7M17.4 17.4l1.7 1.7M2.8 12h2.4M18.8 12h2.4M4.9 19.1l1.7-1.7M17.4 6.6l1.7-1.7" /></svg>
      </button>
    </header>
  );
}
