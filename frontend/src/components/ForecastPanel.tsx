import type { Horizon, Prediction, PricePayload } from "../types";
import { arrow, dirClass, fmtDate, fmtDateTime, pct, usd } from "../format";
import { api } from "../api";
import { Info } from "./Common";

const HORIZONS: Horizon[] = [1, 5, 20];

export function ForecastPanel({ prices, horizon, onHorizon, onRun, running, error, result, onOpenBacktest }: {
  prices: PricePayload; horizon: Horizon; onHorizon: (h: Horizon) => void; onRun: () => void; running: boolean; error: string | null;
  result: Prediction | null; onOpenBacktest: () => void;
}) {
  const f = result?.forecast;
  const t = result?.test;
  return (
    <aside className="side" aria-label="Forecast configuration">
      <h2 className="side-title">Forecast configuration</h2>

      <div className="field">
        <div className="label" id="horizon-label">Horizon <Info label="Horizon" text="How many future daily bars ahead to forecast. A separate model is trained for each horizon." /></div>
        <div className="seg block" role="radiogroup" aria-labelledby="horizon-label">
          {HORIZONS.map((h) => (
            <button key={h} role="radio" aria-checked={h === horizon} className={h === horizon ? "on" : ""} onClick={() => onHorizon(h)} disabled={running} data-testid={`h-${h}`}>
              {h}<span className="seg-unit"> {h === 1 ? "session" : "sessions"}</span>
            </button>
          ))}
        </div>
      </div>

      <button className="btn primary run" onClick={onRun} disabled={running} data-testid="run">{running ? "Running…" : "Run forecast"}</button>

      <div className={`match ${result ? "ok" : running ? "run" : "none"}`} role="status" data-testid="match">
        {running ? <>Running {horizon}-session forecast…</>
          : result ? <><span aria-hidden="true">✓</span> Matches configuration: {result.horizon} {result.horizon === 1 ? "session" : "sessions"} · data through {fmtDate(result.base_date, { day: "2-digit", month: "short", year: "numeric" })} · run {fmtDateTime(result.generated_at)}</>
          : <>No result for {horizon} {horizon === 1 ? "session" : "sessions"} on the current data. Run the forecast.</>}
      </div>

      {error && <div className="inline-error" role="alert" data-testid="run-error">{error}</div>}

      {result && f && t && !running && (
        <div className="results" data-testid="result">
          <dl>
            <div className="res main">
              <dt>Forecast price <Info label="Forecast price" text="Model prediction of the daily close at the target session, in USD per troy ounce. The origin is the latest completed close." /></dt>
              <dd className="mono big" data-testid="pred-price">{usd(f.price)}</dd>
            </div>
            <div className="res">
              <dt>Projected change <Info label="Projected change" text={`Forecast price relative to the origin close of ${usd(result.base_price)}. A model output, not a measured edge.`} /></dt>
              <dd className={`mono ${dirClass(f.change_pct)}`} data-testid="pred-change"><span aria-hidden="true">{arrow(f.change_pct)}</span> {pct(f.change_pct)}</dd>
            </div>
            <div className="res">
              <dt>Target session <Info label="Target session" text={`The ${result.horizon}-th daily bar after the origin, taken from the exchange calendar (${result.calendar.rule}).`} /></dt>
              <dd className="mono" data-testid="pred-target">{fmtDate(result.target_date, { day: "2-digit", month: "short", year: "numeric" })}</dd>
            </div>
            <div className="res">
              <dt>Target range <Info label="Target range" text={`Nominal ${Math.round(f.interval.level * 100)}% range. Calibrated on validation-period errors (${fmtDate(f.interval.calibrated_on.start, { month: "short", year: "numeric" })}–${fmtDate(f.interval.calibrated_on.end, { month: "short", year: "numeric" })}) scaled by recent volatility, then measured on the untouched test period, where it contained ${Math.round(f.interval.observed_coverage_pct)}% of outcomes.`} /></dt>
              <dd className="mono" data-testid="pred-range">{usd(f.interval.low)} – {usd(f.interval.high)}</dd>
              <div className="micro mono">nominal {Math.round(f.interval.level * 100)}% · observed {Math.round(f.interval.observed_coverage_pct)}% on test</div>
            </div>
            <div className="res">
              <dt>Model <Info label="Model" text={`Chosen by lower validation MAE between Ridge regression and a Random Forest. ${result.selection.rule}`} /></dt>
              <dd data-testid="pred-model">{f.model_label}</dd>
            </div>
          </dl>

          <div className={`perf-note ${t.advantage ? "ok" : ""}`} data-testid="perf-note">
            <div className="pn-title">{t.advantage ? "Beat the unchanged baseline" : "No demonstrated advantage"}</div>
            <p>Test MAE {usd(t.methods[f.model].mae)} vs {usd(t.methods.unchanged.mae)} for unchanged price, {t.n} predictions {fmtDate(t.period.start, { month: "short", year: "numeric" })} – {fmtDate(t.period.end, { month: "short", year: "numeric" })}.</p>
            <button className="link" onClick={onOpenBacktest} data-testid="open-backtest">Open backtest →</button>
          </div>

          <a className="btn export" href={api.downloadUrl(result.horizon, result.data_key)} download data-testid="download">Export forecast (CSV)</a>
        </div>
      )}

      {!result && !running && (
        <div className="results idle">
          <p className="micro">Source: {prices.source}. History {fmtDate(prices.first_date, { month: "short", year: "numeric" })} – {fmtDate(prices.latest.date, { month: "short", year: "numeric" })}, {prices.count.toLocaleString()} closes.</p>
          <span className="btn export disabled" aria-disabled="true" data-testid="download-disabled" title="Run a forecast first">Export forecast (CSV)</span>
        </div>
      )}
    </aside>
  );
}
