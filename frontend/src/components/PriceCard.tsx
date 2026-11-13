import type { PricePayload } from "../types";
import { arrow, dirClass, fmtDate, pct, signed, timeAgo, usd } from "../format";

export function PriceCard({ p }: { p: PricePayload }) {
  const l = p.latest;
  const cls = dirClass(l.change);
  const notes = p.warnings.filter((w) => !w.startsWith("Live refresh failed"));
  return (
    <section className="card price-card" aria-label="Latest gold price">
      <div className="card-head tight">
        <div className="eyebrow">Gold futures · USD per troy ounce</div>
        <span className={`status ${p.status.mode}`} data-testid="data-status"><i />{p.status.label}</span>
      </div>
      <div className="big num" data-testid="latest-price">{usd(l.close)}</div>
      <div className={`delta ${cls}`}>
        <span aria-hidden="true">{arrow(l.change)}</span> {signed(l.change)} ({pct(l.change_pct)})
        <span className="muted"> · vs {fmtDate(l.previous_date, { day: "numeric", month: "short" })}</span>
      </div>
      <dl className="facts">
        <div><dt>Observation</dt><dd data-testid="obs-date">{fmtDate(l.date, { weekday: "short", day: "numeric", month: "short", year: "numeric" })} · {p.freshness.age_days === 0 ? "today" : `${p.freshness.age_days} d ago`} · completed session</dd></div>
        <div><dt>Instrument</dt><dd>{p.instrument.name}{p.instrument.kind === "futures" ? " — futures, not spot" : ""}</dd></div>
        <div><dt>Source</dt><dd>{p.source}{p.fetched_at && <span className="muted"> · fetched {timeAgo(p.fetched_at)}</span>}</dd></div>
      </dl>
      {p.session.in_progress && (
        <p className="micro">Session in progress: last trade {usd(p.session.in_progress.price)} (not used by the model).</p>
      )}
      {notes.length > 0 && <ul className="micro data-notes" aria-label="Data notes">{notes.map((w) => <li key={w}>{w}</li>)}</ul>}
    </section>
  );
}
