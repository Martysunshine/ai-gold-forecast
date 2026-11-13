import type { Horizon, Prediction } from "../types";
import { arrow, dirClass, fmtDate, pct, usd } from "../format";

const HORIZONS: Horizon[] = [1, 5, 20];

export function PredictionPanel({ horizon, onHorizon, onPredict, loading, error, prediction }: {
  horizon: Horizon; onHorizon: (h: Horizon) => void; onPredict: () => void; loading: boolean; error: string | null; prediction: Prediction | null;
}) {
  const f = prediction?.forecast;
  return (
    <section className="card forecast-card" aria-label="Prediction">
      <div className="eyebrow">Predict</div>
      <div className="predict-row">
        <div className="seg" role="radiogroup" aria-label="Forecast horizon">
          {HORIZONS.map((h) => (
            <button key={h} role="radio" aria-checked={h === horizon} className={h === horizon ? "on" : ""} onClick={() => onHorizon(h)} disabled={loading}>
              {h} {h === 1 ? "session" : "sessions"}
            </button>
          ))}
        </div>
        <button className="btn primary big-btn" onClick={onPredict} disabled={loading} data-testid="predict">{loading ? "Predicting…" : prediction ? "Predict again" : "Predict"}</button>
      </div>

      {error && <div className="inline-error" role="alert">{error}</div>}

      {!prediction && !loading && !error && (
        <p className="placeholder">Choose how many trading sessions ahead to look, then click <b>Predict</b>. The model is trained on the loaded price history only — no news is used.</p>
      )}
      {loading && <p className="placeholder" role="status">Training and testing the models…</p>}

      {prediction && f && !loading && (
        <div data-testid="result">
          <div className="fc-main">
            <div>
              <div className="eyebrow">Predicted close · ≈{fmtDate(prediction.target_date)}</div>
              <div className="big num" data-testid="pred-price">{usd(f.price)}</div>
              <div className={`delta ${dirClass(f.change_pct)}`} data-testid="pred-change"><span aria-hidden="true">{arrow(f.change_pct)}</span> {pct(f.change_pct)} vs {usd(prediction.base_price)} ({fmtDate(prediction.base_date, { day: "numeric", month: "short" })})</div>
              <div className="muted small">Model: {f.model_label} (chosen on validation data)</div>
            </div>
            <div className="range">
              <div className="eyebrow">Uncertainty · nominal {Math.round(f.interval.level * 100)}% range</div>
              <div className="num range-vals">{usd(f.interval.low, 0)} <span className="muted">to</span> {usd(f.interval.high, 0)}</div>
              <div className="muted small">
                Built from validation-period errors. On the untouched test period it contained the true price <b>{Math.round(f.interval.observed_coverage_pct)}%</b> of the time (nominal {Math.round(f.interval.level * 100)}%).
              </div>
            </div>
          </div>
          <div className={`verdict ${prediction.test.advantage ? "beats" : "worse"}`} role="note" data-testid="verdict">
            <b>{prediction.test.verdict}.</b> On the test period ({fmtDate(prediction.test.period.start, { month: "short", year: "numeric" })} – {fmtDate(prediction.test.period.end, { month: "short", year: "numeric" })}) the model's average error was {usd(prediction.test.methods[f.model].mae, 0)} vs {usd(prediction.test.methods.unchanged.mae, 0)} for “price stays unchanged”.
            {!prediction.test.advantage && " The number above is a model output, not evidence of an edge."}
          </div>
        </div>
      )}
    </section>
  );
}
