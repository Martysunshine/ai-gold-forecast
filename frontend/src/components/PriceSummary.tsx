import type { MarketSummary } from "../types";
import { arrow, dirClass, num, pct, signed } from "../format";
import { Info } from "./Common";

/** The market strip: latest price, session change, previous close and open, session range. One bordered row, as in the research-terminal layout. */
export function PriceSummary({ s, err }: { s: MarketSummary | null; err: string | null }) {
  if (!s) return <section className="gx-statrow" aria-busy="true"><div className="gx-statcell"><span>Latest</span><div className="gx-price mono gx-skel">——.——</div><small className="gx-muted">{err ?? "Loading price…"}</small></div></section>;
  const cls = s.change != null ? dirClass(s.change) : "flat";
  const prevMissing = s.previous_close == null;
  const dash = (v: number | null) => (v != null ? num(v) : "—");
  return (
    <section className="gx-statrow" aria-label="Price summary">
      <div className="gx-statcell wide">
        <span>Latest {s.name.toLowerCase().includes("spot") ? "spot price" : "futures price"}</span>
        <div className="gx-price-row"><div className="gx-price mono" data-testid="price">{num(s.price)}</div><div className="gx-cur">USD<br /><small>per troy oz</small></div></div>
        <small className="gx-muted">{s.name} · {s.provider}</small>
      </div>
      <div className="gx-statcell">
        <span>Session change</span>
        {s.change != null ? (
          <div className={`gx-change mono ${cls}`} data-testid="change"><span aria-hidden="true">{arrow(s.change)}</span> {signed(s.change)}<small>{pct(s.change_pct ?? 0)}</small></div>
        ) : <div className="gx-change gx-muted" data-testid="change">Unavailable<small>no previous close recorded yet</small></div>}
      </div>
      <div className="gx-statcell">
        <span>Previous close</span>
        {prevMissing
          ? <b className="gx-unavail" data-testid="prev-close-missing">Unavailable from provider<Info label="Previous close" text={s.previous_close_note} /></b>
          : <b className="mono">{dash(s.previous_close)}</b>}
        <small className="gx-muted mono">open {dash(s.session.open)}{s.session.open_note ? <sup>*</sup> : null}{s.previous_close_date ? ` · ${s.previous_close_date} settlement` : ""}</small>
      </div>
      <div className="gx-statcell">
        <span>Session range <Info label="Session definition" text={`${s.session.convention}${s.session.open_note ? " * " + s.session.open_note : ""}`} /></span>
        <b className="mono">{dash(s.session.low)} – {dash(s.session.high)}</b>
        <small className="gx-muted">low – high</small>
      </div>
    </section>
  );
}
