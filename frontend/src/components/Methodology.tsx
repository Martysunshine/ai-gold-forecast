import type { Prediction, PricePayload } from "../types";
import { fmtDate } from "../format";

export function Methodology({ prices, result }: { prices: PricePayload; result: Prediction | null }) {
  const sp = result?.splits;
  return (
    <div className="method" data-testid="methodology">
      <section>
        <h3>Data</h3>
        <p>{prices.instrument.name}, {prices.instrument.unit}. Source: {prices.source}. {prices.count.toLocaleString()} daily closes, {fmtDate(prices.first_date)} → {fmtDate(prices.latest.date)}. This is a futures series, not spot gold; contract rolls can add small jumps.</p>
        <p>Bars are dated in the exchange time zone (America/New_York) with daylight saving handled per timestamp. The unfinished session is excluded, so the latest value is always a completed close. Provider data is cached for 15 minutes; a failed refresh keeps the last valid data and shows its age. An uploaded CSV stays active until you switch back to live data.</p>
      </section>
      <section>
        <h3>Features and target</h3>
        <p>All features use prices up to the forecast origin only: the last five daily returns, 5/10/20-session momentum, price relative to its 5- and 20-session average, and 10/20-session realised volatility (12 features). The target is the forward log return over the horizon, converted to a price. One model per horizon (1, 5, 20 sessions).</p>
      </section>
      <section>
        <h3>Evaluation</h3>
        <p>One chronological split per horizon: 60% train, 20% validation, 20% test. Ridge regression and a Random Forest are fitted on train; the lower validation MAE selects the model. The test period is scored once, after refitting on everything before it. A sample whose target window (t, t+h] reaches into a later period is dropped at each boundary. Baseline: price unchanged.</p>
        {sp && <p className="mono small">train {fmtDate(sp.train.start)} → {fmtDate(sp.train.end)} · validation {fmtDate(sp.validation.start)} → {fmtDate(sp.validation.end)} · test {fmtDate(sp.test.start)} → {fmtDate(sp.test.end)}</p>}
      </section>
      <section>
        <h3>Calendar</h3>
        <p>{result ? `${result.calendar.name} (${result.calendar.library}). ` : ""}Target dates are the h-th daily bar after the origin: weekends and full-closure holidays are skipped, and so are US exchange holidays, because the data provider records no separate daily bar for most of them. Checked on 13 Nov 2026 against five years of observed bars: all 1,254 rule sessions had a bar and 3 observed bars fell outside the rule (reproduce with python -m app.sessions).</p>
      </section>
      <section>
        <h3>Uncertainty</h3>
        <p>The target range uses the 10th and 90th percentiles of the selected model's validation errors, expressed in units of recent volatility (20-session daily volatility × √horizon) and rescaled at the forecast origin. It is calibrated on the validation period and its coverage is measured on the untouched test period; both are reported. It is nominally an 80% range.</p>
      </section>
    </div>
  );
}
