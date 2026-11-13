import type { PricePayload } from "../types";
import { arrow, dirClass, fmtDate, pct, signed, timeAgo, usd } from "../format";

export function PriceCard({ p }: { p: PricePayload }) {
  const l = p.latest;
  const cls = dirClass(l.change);
  return (
    <section className="card price-card" aria-label="Latest gold price">
      <div className="eyebrow">{p.instrument.name}</div>
      <div className="big num">{usd(l.close)}<span className="unit"> / oz</span></div>
      <div className={`delta ${cls}`}>
        <span aria-hidden="true">{arrow(l.change)}</span> {signed(l.change)} ({pct(l.change_pct)})
        <span className="muted"> · {fmtDate(l.date, { day: "numeric", month: "short" })} vs {fmtDate(l.previous_date, { day: "numeric", month: "short" })}</span>
      </div>
      <dl className="facts">
        <div><dt>Freshness</dt><dd><span className={`dot ${p.freshness.status}`} />{p.freshness.message}</dd></div>
        <div><dt>Source</dt><dd>{p.source}{p.fetched_at && <span className="muted"> · fetched {timeAgo(p.fetched_at)}</span>}</dd></div>
        <div><dt>Instrument</dt><dd>{p.instrument.kind === "futures" ? "Gold futures — not spot gold" : "User-supplied series"}</dd></div>
      </dl>
      <p className="micro">{p.instrument.note}</p>
      {p.warnings.filter((w) => !w.startsWith("Live refresh failed")).length > 0 && (
        <ul className="micro data-notes" aria-label="Data notes">
          {p.warnings.filter((w) => !w.startsWith("Live refresh failed")).map((w) => <li key={w}>{w}</li>)}
        </ul>
      )}
    </section>
  );
}
