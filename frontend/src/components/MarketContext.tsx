import type { NewsPayload } from "../types";
import { fmtDateTime, timeAgo } from "../format";
import { ErrorBlock, Skeleton } from "./Common";

export function MarketContext({ news, loading, error, onRetry }: { news: NewsPayload | null; loading: boolean; error: string | null; onRetry: () => void }) {
  const note = <p className="notice" data-testid="news-note">News is context. It is <b>not</b> an input to the numerical forecast.</p>;
  if (loading && !news) return <div>{note}<Skeleton h={18} w="70%" /><div style={{ height: 10 }} /><Skeleton h={18} w="55%" /><div style={{ height: 10 }} /><Skeleton h={18} w="62%" /></div>;
  if (error || !news || news.status === "error") {
    return <div>{note}<ErrorBlock title="Headlines unavailable" message={error ?? news?.message ?? "The news feed could not be retrieved."} onRetry={onRetry} /><p className="micro">The forecast and backtest are unaffected.</p></div>;
  }
  const active = news.themes.filter((t) => t.count > 0).sort((a, b) => b.count - a.count);
  return (
    <div data-testid="news">
      {note}
      {news.message && <p className="notice warn">{news.message}</p>}
      <ul className="news-list">
        {news.items.slice(0, 12).map((n) => (
          <li key={n.link}>
            <a href={n.link} target="_blank" rel="noopener noreferrer" className="news-row">
              <span className="nr-title">{n.title}</span>
              <span className="nr-meta mono"><span>{n.source}</span><time dateTime={n.published}>{fmtDateTime(n.published)}</time></span>
              <span className="sr"> (opens in a new tab)</span>
            </a>
          </li>
        ))}
      </ul>
      {active.length > 0 && (
        <div className="themes">
          <h3>Themes in these headlines</h3>
          <dl>
            {active.map((t) => (
              <div key={t.id}>
                <dt>{t.label} <span className="muted mono">{t.count}/{news.n_items}</span></dt>
                <dd>{t.background}</dd>
              </div>
            ))}
          </dl>
          <p className="micro">Theme counts are keyword matches on headlines. The explanatory sentences are general background written for this app, not statements from the articles, and they do not indicate direction.</p>
        </div>
      )}
      <p className="micro mono">{news.source}{news.fetched_at && ` · fetched ${timeAgo(news.fetched_at)}`} · {news.n_items} headlines</p>
      <button className="btn small" onClick={onRetry}>Reload headlines</button>
    </div>
  );
}
