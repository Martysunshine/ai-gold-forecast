import type { NewsPayload } from "../types";
import { fmtDate, timeAgo } from "../format";
import { CardSkeleton, ErrorCard } from "./Common";

export function NewsSection({ news, loading, error, onRetry }: { news: NewsPayload | null; loading: boolean; error: string | null; onRetry: () => void }) {
  if (loading && !news) return <CardSkeleton label="Loading current affairs" lines={4} />;
  if (error) return <ErrorCard title="Current affairs unavailable" message={error} onRetry={onRetry} />;
  if (!news) return null;
  if (news.status === "error") return <ErrorCard title="Current affairs unavailable" message={news.message ?? "The news feed could not be retrieved."} onRetry={onRetry} />;
  return (
    <section className="news" aria-label="Current affairs">
      <div className="card">
        <div className="card-head">
          <div>
            <h2>Current affairs</h2>
            <p className="sub">Recent gold headlines from {news.source}{news.fetched_at && ` · fetched ${timeAgo(news.fetched_at)}`}</p>
          </div>
          <button className="btn small" onClick={onRetry}>Reload news</button>
        </div>
        {news.message && <p className="notice">{news.message}</p>}
        <div className="themes">
          {news.themes.map((t) => (
            <div key={t.id} className={`theme ${t.count === 0 ? "none" : ""}`}>
              <div className="theme-top"><span>{t.label}</span><b className="num">{t.count}</b></div>
              <div className="bar" aria-hidden="true"><i style={{ width: `${t.share_pct}%` }} /></div>
              <div className="micro">{t.count === 0 ? "Not prominent in recent headlines" : `${t.share_pct}% of ${news.n_items} headlines`}</div>
            </div>
          ))}
        </div>
        <p className="micro">{news.method} Theme counts show what is being discussed, not whether it is bullish or bearish. The statistical forecast above does not use any news.</p>
      </div>
      <div className="news-grid">
        {news.items.slice(0, 9).map((n) => (
          <a key={n.link} className="news-card" href={n.link} target="_blank" rel="noopener noreferrer">
            <div className="news-meta"><span>{n.source}</span><time dateTime={n.published}>{fmtDate(n.published, { day: "numeric", month: "short" })}</time></div>
            <div className="news-title">{n.title}</div>
            <div className="news-tags">{n.themes.map((t) => <span key={t} className="tag">{news.themes.find((x) => x.id === t)?.label ?? t}</span>)}</div>
            <span className="sr"> (opens in a new tab)</span>
          </a>
        ))}
      </div>
    </section>
  );
}
