import type { ReactNode } from "react";
import type { MethodKey, Prediction, PricePayload } from "../types";
import { fmtDate, num, pct, usd } from "../format";

const MODELS: MethodKey[] = ["ridge", "random_forest"];

function Stage({ n, title, tone, children }: { n: number; title: string; tone?: "verdict" | "verdict ok"; children: ReactNode }) {
  return (
    <section className={`stage ${tone ?? ""}`} data-testid={`stage-${n}`}>
      <span className="stage-n" aria-hidden="true">{n}</span>
      <h3>{title}</h3>
      {children}
    </section>
  );
}

const Fact = ({ k, v, win }: { k: string; v: ReactNode; win?: boolean }) => (
  <span className={`fact ${win ? "win" : ""}`}>{k} <b>{v}</b></span>
);

/** The forecast, stage by stage. Every number comes from the data and the run on screen; nothing here is written down once. */
export function Methodology({ prices, result }: { prices: PricePayload; result: Prediction | null }) {
  const r = result;
  const f = r?.forecast, t = r?.test, sp = r?.splits, cal = r?.calendar;
  const total = sp ? sp.train.n + sp.validation.n + sp.test.n : 0;
  const share = (n: number) => `${(n / total) * 100}%`;
  const sessions = (h: number) => `${h} ${h === 1 ? "session" : "sessions"}`;

  return (
    <div className="method" data-testid="methodology">
      <p className="method-intro" data-testid="method-intro">
        {r
          ? `How the ${r.horizon}-session forecast on screen was made, stage by stage, with the numbers of that run.`
          : "How a forecast is made, stage by stage. Run a forecast and every stage fills in with the numbers of that run."}
      </p>

      <Stage n={1} title="Data">
        <p>{prices.instrument.name}, {prices.instrument.unit}. This is a futures series, not spot gold; contract rolls can add small jumps. Bars are dated in the exchange time zone (America/New_York) with daylight saving handled per timestamp. The unfinished session is excluded, so the latest value is always a completed close. Provider data is cached for 15 minutes; a failed refresh keeps the last valid data and shows its age. An uploaded CSV stays active until you switch back to live data.</p>
        <div className="facts">
          <Fact k="Source" v={prices.source} />
          <Fact k="Daily closes" v={prices.count.toLocaleString()} />
          <Fact k="From" v={fmtDate(prices.first_date)} />
          <Fact k="To" v={fmtDate(prices.latest.date)} />
          <Fact k="Latest close" v={usd(prices.latest.close)} />
        </div>
      </Stage>

      <Stage n={2} title="Features and target">
        <p>Every feature uses prices up to the forecast origin only: the last five daily returns, 5/10/20-session momentum, price relative to its 5- and 20-session average, and 10/20-session realised volatility. The target is the forward log return over the horizon, converted back to a price. Each horizon (1, 5, 20 sessions) has its own model.</p>
        {r && (
          <div className="facts">
            <Fact k="Features" v={r.features.length} />
            <Fact k="Usable samples" v={r.training.usable_samples.toLocaleString()} />
            {r.features.map((x) => <span className="fact" key={x.name}>{x.label}</span>)}
          </div>
        )}
      </Stage>

      <Stage n={3} title="Split">
        <p>One chronological split: 60% train, 20% validation, 20% test. A sample whose target window (t, t+h] reaches into the next period is dropped at each boundary, so no label looks into a later period.</p>
        {sp && (
          <>
            <div className="splitbar" role="img" aria-label={`Train ${sp.train.n} samples, validation ${sp.validation.n}, test ${sp.test.n}`}>
              <i className="tr" style={{ width: share(sp.train.n) }} /><i className="va" style={{ width: share(sp.validation.n) }} /><i className="te" style={{ width: share(sp.test.n) }} />
            </div>
            <div className="splitkey" data-testid="method-split">
              <span><i className="tr" />Train {fmtDate(sp.train.start)} → {fmtDate(sp.train.end)} · n={sp.train.n}</span>
              <span><i className="va" />Validation {fmtDate(sp.validation.start)} → {fmtDate(sp.validation.end)} · n={sp.validation.n}</span>
              <span><i className="te" />Test {fmtDate(sp.test.start)} → {fmtDate(sp.test.end)} · n={sp.test.n}</span>
            </div>
          </>
        )}
      </Stage>

      <Stage n={4} title="Model choice">
        <p>Ridge regression and a Random Forest are fitted on the train period. The one with the lower mean absolute error (MAE) on the validation period is selected. The test period plays no part in the choice. The settings of both models are fixed in advance.</p>
        {r && (
          <div className="facts" data-testid="method-choice">
            {MODELS.map((k) => <Fact key={k} k={`${r.test.methods[k].label}, validation MAE`} v={usd(r.selection.validation_mae[k])} win={k === r.selection.default} />)}
            <Fact k="Unchanged price, validation MAE" v={usd(r.selection.validation_mae.unchanged)} />
            <Fact k="Selected" v={r.forecast.model_label} win />
          </div>
        )}
      </Stage>

      <Stage n={5} title="Test" tone={t ? (t.advantage ? "verdict ok" : "verdict") : undefined}>
        <p>Both models are refitted on everything before the test period and scored on it once, against the baseline that predicts no change in price. A model counts as better only if its test MAE is at least 2% below the baseline.</p>
        {r && t && f && (
          <>
            <div className="facts" data-testid="method-test">
              {MODELS.map((k) => <Fact key={k} k={`${t.methods[k].label}, test MAE`} v={`${usd(t.methods[k].mae)} (${pct(t.methods[k].vs_baseline_pct ?? 0, 1)} vs unchanged)`} win={k === f.model} />)}
              <Fact k="Unchanged price, test MAE" v={usd(t.methods.unchanged.mae)} />
              <Fact k="Direction right" v={`${num(t.methods[f.model].directional_accuracy ?? 0, 0)}% (always up ${num(t.always_up_pct, 0)}%)`} />
              <Fact k="Test predictions" v={`${t.n} (about ${num(t.effective_n, 0)} independent)`} />
            </div>
            <p data-testid="method-verdict"><b>{t.verdict}.</b></p>
          </>
        )}
      </Stage>

      <Stage n={6} title="Range">
        <p>The target range uses the 10th and 90th percentiles of the selected model's validation errors, measured in units of recent volatility (20-session daily volatility × √horizon) and rescaled at the forecast origin. That is nominally an 80% range. Its real coverage is then measured on the test period, which the calibration never saw.</p>
        {r && f && (
          <div className="facts" data-testid="method-range">
            <Fact k="Lower bound" v={`${num(f.interval.z_low, 2)} × volatility`} />
            <Fact k="Upper bound" v={`${num(f.interval.z_high, 2)} × volatility`} />
            <Fact k="Nominal" v={`${Math.round(f.interval.level * 100)}%`} />
            <Fact k="Observed on test" v={`${num(f.interval.observed_coverage_pct, 0)}%`} />
            <Fact k="Range on screen" v={`${usd(f.interval.low)} – ${usd(f.interval.high)}`} />
          </div>
        )}
      </Stage>

      <Stage n={7} title="Calendar">
        <p>A target date is the h-th daily bar after the origin: weekends and full-closure holidays are skipped, and so are US exchange holidays, because the data provider records no separate daily bar for most of them. The rule is checked against the bars of the data on screen; <code>python -m app.sessions</code> in <code>backend/</code> lists the dates.</p>
        {r && cal && (
          <div className="facts" data-testid="method-calendar">
            <Fact k="Origin" v={fmtDate(r.base_date)} />
            <Fact k={`Target, ${sessions(r.horizon)} later`} v={fmtDate(r.target_date)} />
            <Fact k="Rule sessions" v={cal.check.rule_sessions.toLocaleString()} />
            <Fact k="Rule sessions without a bar" v={cal.check.rule_sessions_without_bar} />
            <Fact k="Bars outside the rule" v={cal.check.bars_not_in_rule} />
            <Fact k="Calendar" v={`${cal.name} (${cal.library})`} />
          </div>
        )}
      </Stage>
    </div>
  );
}
