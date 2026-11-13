import type { NewsPayload } from "../types";
import { fmtDate, timeAgo } from "../format";
import { CardSkeleton, ErrorCard } from "./Common";

export function NewsSection({ news, loading, error, onRetry }: { news: NewsPayload | null; loading: boolean; error: string | null; onRetry: () => void }) {
  if (loading && !news) return <CardSkeleton label="Loading news" lines={4} />;
  if (error) return <ErrorCard title="News unavailable" message={error} onRetry={onRetry} />;
  if (!news) return null;
  if (news.status === "error") return <ErrorCard title="News unavailable" message={news.message ?? "The news feed could not be retrieved."} onRetry={onRetry} />;
  const active = news.themes.filter((t) => t.count > 0).sort((a, b) => b.count - a.count);
  const quiet = news.themes.filter((t) => t.count === 0).map((t) => t.label);
  return (
    <section className="news" aria-label="News" data-testid="news">
      <div className="card">
        <div className="card-head">
          <div>
            <h2>What might affect gold</h2>
            <p className="sub">Themes found in recent headlines from {news.source}{news.fetched_at && ` · fetched ${timeAgo(news.fetched_at)}`}</p>
          </div>
          <button className="btn small" onClick={onRetry}>Reload news</button>
        </div>
        <p className="notice neutral">News is shown for context only. It is <b>not</b> used by the prediction model.</p>
        {news.message && <p className="notice">{news.message}</p>}
        <div className="theme-list">
          {active.map((t) => (
            <article key={t.id} className="theme-item">
              <h3>{t.label} <span className="muted small">· {t.count} of {news.n_items} recent headlines</span></h3>
              <p><span className="kind">General background</span> {t.background}</p>
              <ul className="ex">
                {t.examples.map((e) => (
                  <li key={e.link}><span className="kind">In the news</span> <a href={e.link} target="_blank" rel="noopener noreferrer">{e.title}</a> <span className="muted small">— {e.source}, {fmtDate(e.published, { day: "numeric", month: "short" })}</span></li>
                ))}
              </ul>
            </article>
          ))}
          {active.length === 0 && <p className="muted">No headlines matched the tracked themes.</p>}
        </div>
        {quiet.length > 0 && <p className="micro">Not prominent in the latest headlines: {quiet.join(", ")}.</p>}
        <p className="micro">“General background” is a standard explanation written for this app, not a claim made by the articles, and it does not say which way prices will move. Themes are matched by keyword.</p>
      </div>
      <div className="news-grid">
        {news.items.slice(0, 9).map((n) => (
          <a key={n.link} className="news-card" href={n.link} target="_blank" rel="noopener noreferrer">
            <div className="news-meta"><span>{n.source}</span><time dateTime={n.published}>{fmtDate(n.published, { day: "numeric", month: "short", year: "numeric" })}</time></div>
            <div className="news-title">{n.title}</div>
            <span className="sr"> (opens in a new tab)</span>
          </a>
        ))}
      </div>
    </section>
  );
}
