import { useState } from "react";
import type { Prediction } from "../types";
import { fmtDate } from "../format";

const TOLS = ["1", "2", "5"] as const;

export function AccuracyPanel({ p }: { p: Prediction }) {
  const [tol, setTol] = useState<(typeof TOLS)[number]>("2");
  const t = p.test;
  const row = t.within_tolerance[tol];
  const dir = t.methods[p.forecast.model].directional_accuracy ?? 0;
  const tr = p.training;
  return (
    <section className="card accuracy" aria-label="Accuracy score" data-testid="accuracy">
      <div className="card-head">
        <div>
          <h2>Accuracy score</h2>
          <p className="sub">Measured on {t.n} predictions the model had never seen ({fmtDate(t.period.start, { month: "short", year: "numeric" })} – {fmtDate(t.period.end, { month: "short", year: "numeric" })})</p>
        </div>
        <div className="seg" role="radiogroup" aria-label="Price tolerance">
          {TOLS.map((x) => (
            <button key={x} role="radio" aria-checked={x === tol} className={x === tol ? "on" : ""} onClick={() => setTol(x)}>±{x}%</button>
          ))}
        </div>
      </div>

      <div className="acc-grid">
        <div className="acc-card">
          <div className="eyebrow">Price accuracy · within ±{tol}%</div>
          <div className="acc-num" data-testid="acc-price">{row.default.toFixed(0)}%</div>
          <div className="meter" aria-hidden="true"><i style={{ width: `${row.default}%` }} /><b style={{ left: `${row.unchanged}%` }} /></div>
          <p className="micro">{p.forecast.model_label}: predicted price landed within ±{tol}% of the real price {row.default.toFixed(0)}% of the time. “Unchanged price” scored {row.unchanged.toFixed(0)}% (marker).</p>
        </div>
        <div className="acc-card">
          <div className="eyebrow">Direction accuracy · up or down</div>
          <div className="acc-num" data-testid="acc-dir">{dir.toFixed(0)}%</div>
          <div className="meter" aria-hidden="true"><i style={{ width: `${dir}%` }} /><b style={{ left: `${t.always_up_pct}%` }} /></div>
          <p className="micro">Share of moves whose direction was called correctly. Always guessing “up” would have scored {t.always_up_pct.toFixed(0)}% (marker).</p>
        </div>
        <div className="acc-card">
          <div className="eyebrow">Trained on</div>
          <div className="acc-num" data-testid="acc-n">{tr.closes.toLocaleString()}</div>
          <p className="micro">
            daily closes, {fmtDate(tr.first_close, { month: "short", year: "numeric" })} – {fmtDate(tr.last_close, { month: "short", year: "numeric" })}. For {p.horizon} {p.horizon === 1 ? "session" : "sessions"} ahead that is {p.splits.train.n} training, {p.splits.validation.n} validation and {t.n} test samples, using {tr.n_features} features.
          </p>
        </div>
      </div>
      <p className="acc-note">
        How to read this: price accuracy measures how close the predicted price was; at a wide tolerance almost any forecast scores high, so compare it with the marker. Direction accuracy is the stricter test of whether the model adds information.
      </p>
    </section>
  );
}
