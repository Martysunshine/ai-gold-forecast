import type { ForecastPayload, Horizon } from "../types";
import { fmtDate, pct } from "../format";

export function Signals({ data, horizon }: { data: ForecastPayload; horizon: Horizon }) {
  const s = data.signals[String(horizon)];
  const c = data.context;
  const volWord = c.vol_20_percentile > 66 ? "high" : c.vol_20_percentile < 33 ? "low" : "moderate";
  return (
    <section className="card" aria-label="Historical signals behind the forecast">
      <h2>What the history is saying</h2>
      <p className="sub">Largest contributions to the {horizon}-session forecast return</p>
      <ul className="drivers">
        {s.drivers.map((d) => (
          <li key={d.feature}>
            <span className={`chip-num ${d.contribution_pct >= 0 ? "up" : "down"}`}>{d.contribution_pct >= 0 ? "+" : "−"}{Math.abs(d.contribution_pct).toFixed(2)} pp</span>
            <span>{d.text}</span>
          </li>
        ))}
        <li>
          <span className={`chip-num ${s.drift_pct >= 0 ? "up" : "down"}`}>{s.drift_pct >= 0 ? "+" : "−"}{Math.abs(s.drift_pct).toFixed(2)} pp</span>
          <span>Average drift: the typical {horizon}-session move across the training history (gold has trended up over much of it). This is a baseline lean, not a signal.</span>
        </li>
      </ul>
      <div className="context">
        <div><span className="muted">Price vs 50-session average</span><b className="num">{pct(c.price_vs_ma50_pct, 1)}</b></div>
        <div><span className="muted">20-session momentum</span><b className="num">{pct(c.momentum_20_pct, 1)}</b></div>
        <div><span className="muted">20-session volatility</span><b className="num">{volWord} ({Math.round(c.vol_20_percentile)}th pct.)</b></div>
      </div>
      <p className="micro">Contributions are Ridge coefficients × standardised feature values (pp = percentage points of log return). They describe how this model arrived at its number — they are associations in past data, not causes.</p>
    </section>
  );
}

const SHORT = { beats: "Beats", worse: "Worse", inconclusive: "No edge" } as const;
const badge = (v: string) => (v === "beats" ? "ok" : v === "worse" ? "bad" : "mid");

export function PerformancePanel({ data, horizon }: { data: ForecastPayload; horizon: Horizon }) {
  const first = data.performance[0];
  return (
    <section className="card" aria-label="Model performance against baseline">
      <h2>Model performance vs “no change”</h2>
      <p className="sub">
        Walk-forward out-of-sample, {fmtDate(first.test_start)} → {fmtDate(first.test_end)} · {first.n_folds} chronological folds
      </p>
      <div className="table-wrap" tabIndex={0}>
        <table className="perf">
          <thead>
            <tr><th>Horizon</th><th className="num">MAE model</th><th className="num">MAE baseline</th><th className="num">Dir. accuracy</th><th className="num">Folds won</th><th>Result</th></tr>
          </thead>
          <tbody>
            {data.performance.map((p) => (
              <tr key={p.horizon} className={p.horizon === horizon ? "sel" : ""}>
                <td>{p.horizon}d</td>
                <td className="num">${p.mae_model.toFixed(1)}</td>
                <td className="num">${p.mae_baseline.toFixed(1)}</td>
                <td className="num">{p.directional_accuracy.toFixed(0)}%<span className="muted small block">always-up {p.always_up_accuracy.toFixed(0)}%</span></td>
                <td className="num">{p.folds_beating_baseline}/{p.n_folds}</td>
                <td><span className={`badge ${badge(p.verdict)}`} title={p.verdict_label}>{SHORT[p.verdict]}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="notes">
        <li><b>MAE</b> = mean absolute error of the predicted price in USD (lower is better). <b>Baseline</b> = price stays at the last close.</li>
        <li><b>Directional accuracy</b> is the share of moves whose direction was right. Compare it with “always up”, which would have scored the share shown simply because gold rose in much of this period.</li>
        <li>Verdict rule (fixed in advance): “beats” needs ≥2% lower MAE <i>and</i> a win in more than half of the folds; “does not beat” means higher MAE.</li>
        <li>Training rows whose target window overlaps a test block are purged; the scaler is fitted on training rows only. Multi-day test windows overlap, so effective sample sizes are smaller than the counts suggest.</li>
      </ul>
    </section>
  );
}
