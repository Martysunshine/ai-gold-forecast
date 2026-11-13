import type { ForecastPayload, Horizon, Scenario } from "../types";
import { arrow, dirClass, fmtDate, pct, usd } from "../format";
import { SCENARIO_SIGMA, scenarioPrice } from "../scenario";

const HORIZONS: Horizon[] = [1, 5, 20];
const SCENARIOS: { id: Scenario; label: string }[] = [
  { id: "bearish", label: "Bearish" },
  { id: "base", label: "Base" },
  { id: "bullish", label: "Bullish" },
];

export function ForecastPanel({ data, horizon, onHorizon, scenario, onScenario }: {
  data: ForecastPayload; horizon: Horizon; onHorizon: (h: Horizon) => void; scenario: Scenario; onScenario: (s: Scenario) => void;
}) {
  const f = data.forecasts.find((x) => x.horizon === horizon)!;
  const perf = data.performance.find((x) => x.horizon === horizon)!;
  const cls = dirClass(f.expected_change_pct);
  const sp = scenarioPrice(f, scenario);
  return (
    <section className="card forecast-card" aria-label="Forecast">
      <div className="card-head tight">
        <div className="eyebrow">Model forecast</div>
        <div className="seg" role="tablist" aria-label="Forecast horizon">
          {HORIZONS.map((h) => (
            <button key={h} role="tab" aria-selected={h === horizon} className={h === horizon ? "on" : ""} onClick={() => onHorizon(h)}>
              {h} {h === 1 ? "session" : "sessions"}
            </button>
          ))}
        </div>
      </div>

      <div className="fc-main">
        <div>
          <div className="big num">{usd(f.forecast_price)}</div>
          <div className={`delta ${cls}`}><span aria-hidden="true">{arrow(f.expected_change_pct)}</span> {pct(f.expected_change_pct)} expected vs {usd(f.base_price)}</div>
          <div className="muted small">for ≈ {fmtDate(f.target_date)} · from close of {fmtDate(f.base_date)}</div>
        </div>
        <div className="range">
          <div className="eyebrow">{Math.round(f.interval.level * 100)}% indicative range</div>
          <div className="num range-vals">{usd(f.interval.low, 0)} <span className="muted">to</span> {usd(f.interval.high, 0)}</div>
          <div className="muted small">{pct(f.interval.low_pct, 1)} to {pct(f.interval.high_pct, 1)} · from out-of-sample errors, not a guarantee</div>
        </div>
      </div>

      <div className={`verdict ${perf.verdict}`} role="note">
        <b>{perf.verdict_label}</b> at {horizon} {horizon === 1 ? "session" : "sessions"}: in walk-forward testing the model's average error was{" "}
        {usd(perf.mae_model, 0)} vs {usd(perf.mae_baseline, 0)} for “price stays unchanged” ({pct(-perf.mae_improvement_pct, 1)} error change).{" "}
        {perf.verdict !== "beats" && "Treat this as a statistical extrapolation of recent behaviour, not a dependable prediction."}
      </div>

      <div className="scenario">
        <div className="scenario-head">
          <div>
            <div className="eyebrow">Scenario assumption <span className="pill">not learned</span></div>
            <p className="micro">Shifts the forecast by ±{SCENARIO_SIGMA}× the typical out-of-sample error. A user-chosen “what if” — it is not produced or validated by the model, and it is not linked to the news.</p>
          </div>
          <div className="seg" role="radiogroup" aria-label="Scenario">
            {SCENARIOS.map((s) => (
              <button key={s.id} role="radio" aria-checked={s.id === scenario} className={s.id === scenario ? "on" : ""} onClick={() => onScenario(s.id)}>{s.label}</button>
            ))}
          </div>
        </div>
        {scenario !== "base" && (
          <div className="scenario-out num">
            {scenario === "bullish" ? "Bullish" : "Bearish"} assumption: <b>{usd(sp)}</b> <span className="muted">({pct((sp / f.base_price - 1) * 100)} vs last close)</span>
          </div>
        )}
      </div>
    </section>
  );
}
