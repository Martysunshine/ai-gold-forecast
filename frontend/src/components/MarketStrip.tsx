import type { PricePayload } from "../types";
import { arrow, dirClass, fmtDate, num, pct, signed, usd } from "../format";
import { Info } from "./Common";

export function MarketStrip({ p }: { p: PricePayload }) {
  const l = p.latest;
  const cls = dirClass(l.change);
  return (
    <section className="strip" aria-label="Market summary">
      <div className="cell lead">
        <div className="label">Latest close <Info label="Latest close" text="Settlement-time close of the most recent completed daily session, in USD per troy ounce, for the front-month COMEX gold futures contract (continuous series). The unfinished session is excluded." /></div>
        <div className="price mono" data-testid="latest-price">{usd(l.close)}</div>
        <div className="micro">USD / troy oz · futures, not spot</div>
      </div>
      <div className="cell">
        <div className="label">Session change <Info label="Session change" text={`Latest close minus the previous completed close (${fmtDate(l.previous_date, { day: "numeric", month: "short" })}), in USD and in percent.`} /></div>
        <div className={`value mono ${cls}`} data-testid="session-change"><span aria-hidden="true">{arrow(l.change)}</span> {signed(l.change)}</div>
        <div className={`sub mono ${cls}`}>{pct(l.change_pct)}</div>
      </div>
      <div className="cell">
        <div className="label">Observation <Info label="Observation" text="Date of the latest completed daily bar, in the exchange's time zone (America/New_York). A session still in progress is never used." /></div>
        <div className="value mono" data-testid="obs-date">{fmtDate(l.date, { day: "2-digit", month: "short", year: "numeric" })}</div>
        <div className="sub">{p.session.latest_is_completed_session ? "completed session" : "in progress"}{p.session.in_progress ? ` · live ${usd(p.session.in_progress.price)}` : ""}</div>
      </div>
      <div className="cell">
        <div className="label">Realised vol <Info label="Realised volatility" text="Standard deviation of the last 20 daily log returns, annualised by √252. A measure of how much the price has actually been moving; not a forecast." /></div>
        <div className="value mono" data-testid="realised-vol">{num(p.stats.realised_vol_20d_annualised_pct, 1)}%</div>
        <div className="sub">20-session, annualised</div>
      </div>
    </section>
  );
}
