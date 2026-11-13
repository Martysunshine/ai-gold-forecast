import { useMemo } from "react";
import { CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { MethodKey, Prediction } from "../types";
import { fmtDate, fmtTs, parseDay, pct, usd } from "../format";

interface Row { t: number; actual: number; predicted: number; baseline: number }

function Tip({ active, payload }: { active?: boolean; payload?: { payload: Row }[] }) {
  if (!active || !payload?.length) return null;
  const r = payload[0].payload;
  return (
    <div className="tip">
      <div className="tip-date">{fmtTs(r.t, { weekday: "short", day: "numeric", month: "short", year: "numeric" })}</div>
      <div className="tip-row"><span>Actual</span><b>{usd(r.actual)}</b></div>
      <div className="tip-row"><span>Predicted</span><span>{usd(r.predicted)}</span></div>
      <div className="tip-row muted"><span>Unchanged baseline</span><span>{usd(r.baseline)}</span></div>
    </div>
  );
}

const ORDER: MethodKey[] = ["ridge", "random_forest", "unchanged"];

export function TestPanel({ p }: { p: Prediction }) {
  const rows = useMemo<Row[]>(() => p.test_series.map((r) => ({ t: parseDay(r.date), actual: r.actual, predicted: r.predicted, baseline: r.baseline })), [p]);
  const domain = useMemo<[number, number]>(() => {
    const v = rows.flatMap((r) => [r.actual, r.predicted, r.baseline]);
    const lo = Math.min(...v), hi = Math.max(...v), pad = (hi - lo) * 0.06;
    return [Math.floor(lo - pad), Math.ceil(hi + pad)];
  }, [rows]);
  const t = p.test;
  const def = p.selection.default;
  return (
    <section className="card" aria-label="Test-period evaluation" data-testid="test-panel">
      <div className="card-head">
        <div>
          <h2>How well did it do? Actual vs predicted</h2>
          <p className="sub">{p.horizon}-session-ahead predictions on the untouched test period, {fmtDate(t.period.start)} → {fmtDate(t.period.end)} · {t.n} predictions</p>
        </div>
      </div>
      <div className="legend">
        <span><i className="sw line" /> Actual price</span>
        <span><i className="sw dash" /> {p.forecast.model_label} prediction</span>
        <span><i className="sw base" /> Unchanged baseline (price at forecast date)</span>
      </div>
      <ResponsiveContainer width="100%" height={300}>
        <ComposedChart data={rows} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="var(--grid)" vertical={false} />
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} stroke="var(--grid)" tick={{ fill: "var(--muted)", fontSize: 12 }} tickLine={false}
            ticks={[0, 1, 2, 3, 4].map((i) => Math.round(rows[0].t + ((rows[rows.length - 1].t - rows[0].t) * i) / 4))}
            tickFormatter={(v: number) => fmtTs(v, { month: "short", year: "2-digit" })} />
          <YAxis domain={domain} orientation="right" width={56} tick={{ fill: "var(--muted)", fontSize: 12 }} axisLine={false} tickLine={false} tickFormatter={(v: number) => v.toLocaleString("en-US")} />
          <Tooltip content={<Tip />} cursor={{ stroke: "var(--muted)", strokeDasharray: "3 3" }} isAnimationActive={false} />
          <Line dataKey="baseline" stroke="var(--muted)" strokeWidth={1.5} strokeDasharray="1 3" dot={false} isAnimationActive={false} />
          <Line dataKey="actual" stroke="var(--gold)" strokeWidth={2} dot={false} isAnimationActive={false} />
          <Line dataKey="predicted" stroke="var(--ivory)" strokeWidth={2} strokeDasharray="5 4" dot={false} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
      <p className="foot-note">Each point is plotted at the date being predicted: the model sees prices up to {p.horizon} {p.horizon === 1 ? "session" : "sessions"} earlier. The dotted baseline is therefore the price {p.horizon} {p.horizon === 1 ? "session" : "sessions"} before.</p>

      <h3 className="h3">Comparison with the baseline</h3>
      <div className="table-wrap" tabIndex={0}>
        <table className="perf" data-testid="comparison">
          <thead>
            <tr><th>Method</th><th className="num" title="Mean absolute error on the validation period">Val. MAE</th><th className="num" title="Mean absolute error on the untouched test period">Test MAE</th><th className="num" title="Test MAE relative to the unchanged baseline">vs base</th><th className="num" title="Share of moves whose direction was right">Direction</th></tr>
          </thead>
          <tbody>
            {ORDER.map((k) => {
              const m = t.methods[k];
              return (
                <tr key={k} className={k === def ? "sel" : ""}>
                  <td>{m.label}{k === def && <span className="tag">default</span>}</td>
                  <td className="num">${p.selection.validation_mae[k].toFixed(1)}</td>
                  <td className="num">${m.mae.toFixed(1)}</td>
                  <td className="num">{k === "unchanged" ? "—" : pct(m.vs_baseline_pct ?? 0, 1)}</td>
                  <td className="num">{m.directional_accuracy === null ? "—" : `${m.directional_accuracy.toFixed(0)}%`}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <ul className="notes">
        <li><b>MAE</b> = mean absolute error of the predicted price in USD (lower is better). The default model is the one with the lower <b>validation</b> MAE; the test period was not used to choose it.</li>
        <li><b>Direction right</b> = share of moves whose direction was predicted correctly. Guessing “always up” would have scored {t.always_up_pct.toFixed(0)}% in this test period.</li>
        <li>The test period holds {t.n} overlapping {p.horizon}-session outcomes — only about <b>{t.effective_n.toFixed(0)}</b> are independent, so small differences are not reliable evidence either way.</li>
        <li>Split by date: train {fmtDate(p.splits.train.start, { month: "short", year: "numeric" })}–{fmtDate(p.splits.train.end, { month: "short", year: "numeric" })} · validation {fmtDate(p.splits.validation.start, { month: "short", year: "numeric" })}–{fmtDate(p.splits.validation.end, { month: "short", year: "numeric" })} · test {fmtDate(p.splits.test.start, { month: "short", year: "numeric" })}–{fmtDate(p.splits.test.end, { month: "short", year: "numeric" })}. Samples whose target window reaches into the next period are dropped.</li>
      </ul>
    </section>
  );
}
