import { useMemo } from "react";
import { CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Horizon, MethodKey, Prediction } from "../types";
import { fmtDate, fmtTs, niceAxis, parseDay, pct, spacedTicks, usd } from "../format";
import { useElementWidth } from "../hooks";
import { Info } from "./Common";

interface Row { t: number; actual: number; predicted: number; baseline: number }

function Tip({ active, payload }: { active?: boolean; payload?: { payload: Row }[] }) {
  if (!active || !payload?.length) return null;
  const r = payload[0].payload;
  return (
    <div className="chart-tip mono">
      <div className="tt-date">{fmtTs(r.t, { weekday: "short", day: "2-digit", month: "short", year: "numeric" })}</div>
      <div className="tt-row"><span>Actual</span><b>{usd(r.actual)}</b></div>
      <div className="tt-row"><span className="blue">Predicted</span><span>{usd(r.predicted)}</span></div>
      <div className="tt-row muted"><span>Unchanged</span><span>{usd(r.baseline)}</span></div>
    </div>
  );
}

const ORDER: MethodKey[] = ["ridge", "random_forest", "unchanged"];

export function Backtest({ result, horizon }: { result: Prediction | null; horizon: Horizon }) {
  const [boxRef, width] = useElementWidth<HTMLDivElement>();
  const rows = useMemo<Row[]>(() => (result ? result.test_series.map((r) => ({ t: parseDay(r.date), actual: r.actual, predicted: r.predicted, baseline: r.baseline })) : []), [result]);
  if (!result) {
    return <p className="placeholder" data-testid="backtest-empty">Run the {horizon}-session forecast to load its backtest. The backtest is computed per horizon on the current data.</p>;
  }
  const t = result.test, f = result.forecast, def = f.model;
  const vals = rows.flatMap((r) => [r.actual, r.predicted, r.baseline]);
  const axis = niceAxis(Math.min(...vals), Math.max(...vals), 5);
  const ticks = spacedTicks(rows[0].t, rows[rows.length - 1].t, width < 460 ? 4 : 6);
  const sp = result.splits;
  const row2 = t.within_tolerance["2"];
  return (
    <div className="backtest" data-testid="backtest">
      <div className="bt-metrics">
        <div className="metric"><div className="label">Test MAE · {f.model_label} <Info label="MAE" text="Mean absolute error: the average absolute difference between the predicted and actual price at the target session, in USD. Lower is better." /></div>
          <div className="mono mval">{usd(t.methods[def].mae)}</div><div className="micro mono">unchanged {usd(t.methods.unchanged.mae)}</div></div>
        <div className="metric"><div className="label">Direction right <Info label="Direction" text="Share of test predictions whose up/down direction matched the actual move. 'Always up' is the naive reference." /></div>
          <div className="mono mval">{(t.methods[def].directional_accuracy ?? 0).toFixed(0)}%</div><div className="micro mono">always up {t.always_up_pct.toFixed(0)}%</div></div>
        <div className="metric"><div className="label">Range coverage <Info label="Range coverage" text="Share of test-period outcomes that fell inside the target range, against its nominal level. Calibrated on the validation period; evaluated here on data the calibration never saw." /></div>
          <div className="mono mval" data-testid="coverage">{f.interval.observed_coverage_pct.toFixed(0)}%</div><div className="micro mono">nominal {Math.round(f.interval.level * 100)}% · n={t.n}</div></div>
        <div className="metric"><div className="label">Within ±2% <Info label="Within ±2%" text="Share of test predictions within 2% of the actual price. The unchanged baseline scores almost the same, because prices rarely move far in a few sessions." /></div>
          <div className="mono mval">{row2.default.toFixed(0)}%</div><div className="micro mono">unchanged {row2.unchanged.toFixed(0)}%</div></div>
      </div>

      <div className="legend">
        <span><i className="lg solid" />Actual</span>
        <span><i className="lg dashed" />{f.model_label} prediction</span>
        <span><i className="lg dotted" />Unchanged baseline</span>
        <em className="micro">{result.horizon}-session-ahead predictions on the test period, plotted at the date being predicted</em>
      </div>
      <div className="chart-box short" ref={boxRef} role="img" aria-label={`Backtest: actual versus predicted ${result.horizon}-session-ahead price, ${fmtDate(t.period.start)} to ${fmtDate(t.period.end)}`}>
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={rows} margin={{ top: 8, right: 4, bottom: 2, left: 4 }}>
            <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
            <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} ticks={ticks} interval={0} padding={{ left: 26, right: 0 }} tickLine={false} stroke="var(--chart-axis)"
              tick={{ fill: "var(--ink-2)", fontSize: 12, fontFamily: "var(--font)" }} tickFormatter={(v: number) => fmtTs(v, { month: "short", year: "2-digit" })} />
            <YAxis domain={axis.domain} ticks={axis.ticks} orientation="right" width={58} axisLine={false} tickLine={false} allowDecimals={false}
              tick={{ fill: "var(--ink-2)", fontSize: 12, fontFamily: "var(--font)" }} tickFormatter={(v: number) => v.toLocaleString("en-US")} />
            <Tooltip content={<Tip />} cursor={{ stroke: "var(--ink-3)", strokeDasharray: "2 3" }} isAnimationActive={false} />
            <Line dataKey="baseline" stroke="var(--ink-3)" strokeWidth={1.2} strokeDasharray="1 3" dot={false} isAnimationActive={false} />
            <Line dataKey="actual" stroke="var(--gold)" strokeWidth={2} dot={false} isAnimationActive={false} />
            <Line dataKey="predicted" stroke="var(--forecast)" strokeWidth={1.8} strokeDasharray="5 4" dot={false} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      <div className="table-wrap" tabIndex={0} aria-label="Model comparison">
        <table className="data" data-testid="comparison">
          <thead>
            <tr><th>Model</th><th className="num hide-sm">Val. MAE</th><th className="num">Test MAE</th><th className="num">vs unchanged</th><th className="num">Direction</th><th className="num hide-sm">Within ±2%</th></tr>
          </thead>
          <tbody>
            {ORDER.map((k) => {
              const m = t.methods[k];
              return (
                <tr key={k} className={k === def ? "sel" : ""}>
                  <td>{m.label}{k === def && <span className="tag">selected</span>}</td>
                  <td className="num mono hide-sm">{usd(result.selection.validation_mae[k])}</td>
                  <td className="num mono">{usd(m.mae)}</td>
                  <td className="num mono">{k === "unchanged" ? "—" : pct(m.vs_baseline_pct ?? 0, 1)}</td>
                  <td className="num mono">{m.directional_accuracy === null ? "—" : `${m.directional_accuracy.toFixed(0)}%`}</td>
                  <td className="num mono hide-sm">{t.within_tolerance["2"][k].toFixed(0)}%</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <dl className="spans" data-testid="spans">
        <div><dt>Train</dt><dd className="mono">{fmtDate(sp.train.start)} → {fmtDate(sp.train.end)} <span className="muted">n={sp.train.n}</span></dd></div>
        <div><dt>Validation <span className="muted">(model choice, range calibration)</span></dt><dd className="mono">{fmtDate(sp.validation.start)} → {fmtDate(sp.validation.end)} <span className="muted">n={sp.validation.n}</span></dd></div>
        <div><dt>Test <span className="muted">(untouched)</span></dt><dd className="mono">{fmtDate(sp.test.start)} → {fmtDate(sp.test.end)} <span className="muted">n={sp.test.n}</span></dd></div>
      </dl>
      <p className="micro">
        {t.verdict}. The {t.n} test outcomes overlap in time, so only about {t.effective_n.toFixed(0)} are independent; small differences between models are within noise.
        Training samples whose target window reaches into the next period are dropped at each boundary.
      </p>
    </div>
  );
}
