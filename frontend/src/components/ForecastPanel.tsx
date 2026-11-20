import type { ForwardPayload, TodayPayload } from "../types";
import { arrow, dirClass, etDate, num, pct } from "../format";
import { api } from "../api";
import { TARGETS, cardTitle, coverageLine, inputLine, issuedAt, modelLine, perfNote, resultLine, settlementLine, targetLine, usd, type ForecastTarget, type Shown } from "../forecastText";
import type { DetailsTab } from "./Details";
import { Info } from "./Common";

/**
 * "Forecast configuration": choose a horizon, press Predict, read the stored forecast. The same record is drawn on the chart, shown here and exported.
 * Only what the forecast IS is shown (price, change, target, range, what produced it, when it was issued and from which inputs).
 * Experimental estimates and evaluation text live in Details.
 */
export function ForecastPanel({ canPredict, target, onTarget, onPredict, predicting, predictErr, shown, newerId, onDetails }: {
  canPredict: boolean; target: ForecastTarget; onTarget: (t: ForecastTarget) => void; onPredict: () => void; predicting: boolean; predictErr: string | null;
  shown: Shown | null; newerId: string | null; onDetails: (tab: DetailsTab) => void;
}) {
  const p = shown?.payload;
  const f = p?.forecast ?? null;
  const today = shown?.target === "next" ? (p as TodayPayload) : null;
  const completed = p?.status === "completed";
  const m = f ? modelLine(f) : null;
  const cov = f ? coverageLine(f, p!.calibration) : null;
  const baseIso = shown && shown.target !== "next" ? (p as ForwardPayload).base_date : today?.base_date;
  const dir = f ? dirClass(f.change) : "flat";
  const newer = !!newerId && !!f && newerId !== f.forecast_id;

  return (
    <aside className="gx-fp" aria-label="Forecast configuration" data-testid="forecast-panel"><div data-testid="predict-bar" className="gx-fp-bar">
      <h2 className="gx-fp-h">Forecast configuration</h2>
      <div className="gx-fp-label">Horizon</div>
      <div className="gx-seg gx-fp-seg" role="tablist" aria-label="Forecast horizon">
        {TARGETS.map((t) => <button key={String(t.id)} role="tab" aria-selected={target === t.id} className={target === t.id ? "on" : ""} disabled={!canPredict} onClick={() => onTarget(t.id)} data-testid={`target-${t.id}`}>{t.label}</button>)}
      </div>
      <button className="gx-predict gx-fp-go" onClick={onPredict} disabled={!canPredict || predicting} title={canPredict ? "Draw this stored forecast on the chart" : "Forecasts are for gold futures. Switch to Gold futures to predict."} data-testid="predict">
        {predicting ? "Predicting…" : "Predict"}
      </button>
      {!canPredict && <p className="gx-micro" data-testid="futures-note">Forecasts are for <b>gold futures</b>, the instrument the models are trained and measured on. They have not been evaluated for spot gold, so they are not drawn on the spot chart.</p>}
      {predictErr && <p className="gx-micro down" role="alert" data-testid="predict-error">{predictErr}</p>}

      </div>
      {!canPredict ? null : !f || !m ? (
        <section className="gx-fp-empty" data-testid="result-strip" data-state="none">
          <p className="gx-muted">Choose Next settlement or a horizon, then press <b>Predict</b>. The forecast is drawn on the chart, with its reference price, target date and range.</p>
        </section>
      ) : (
        <section className="gx-fp-result" data-testid="result-strip" data-state={completed ? "completed" : "current"} data-forecast-id={f.forecast_id} data-route={today?.route ?? "forward"}>
          <div className="gx-fp-match" data-testid="matches">✓ Matches your choice: {shown!.target === "next" ? "next settlement" : `${shown!.target} ${shown!.target === 1 ? "session" : "sessions"}`} · stored forecast, not recomputed</div>
          <div className="gx-fp-title">
            <b data-testid="strip-title">{cardTitle(shown!.target, f)}</b>
            <span className={`gx-chip ${m.baseline ? "base" : ""}`} data-testid="source-chip">{m.chip}</span>
            {completed && <span className="gx-chip warnchip" data-testid="completed-chip">Completed</span>}
            {newer && <span className="gx-chip warnchip" data-testid="newer-chip">A newer forecast exists — press Predict</span>}
          </div>
          <div className="gx-fp-label">Forecast price</div>
          <div className="gx-fp-price mono" data-testid="strip-price">{usd(f.predicted)}</div>
          <div className="gx-fp-label">Projected change</div>
          <div className={`gx-fp-val mono ${f.little_change ? "flat" : dir}`} data-testid="strip-change">
            {f.little_change ? "Little change" : <><span aria-hidden="true">{arrow(f.change)}</span> {pct(f.change_pct)}</>}
            <small className="mono"> {f.change < 0 ? "−" : "+"}${num(Math.abs(f.change))}{f.little_change && <Info label="Little change" text={`Shown when the predicted move is smaller than ${num(f.little_change_threshold_pct, 2)}% (0.10% × √sessions). A display rule only, not a confidence level and not an edge.`} />}</small>
          </div>
          <div className="gx-fp-label">Target session</div>
          <div className="gx-fp-val" data-testid="strip-target">{etDate(Date.parse(f.target_time), true)}<small> {targetLine(f).split(" · ").slice(1).join(" · ")}</small></div>
          <div className="gx-fp-label">Target range</div>
          <div className="gx-fp-val mono" data-testid="strip-range">{usd(f.low)} – {usd(f.high)}</div>
          <div className="gx-fp-sub" data-testid="calibration">{cov!.text}{cov!.label && <em className="gx-warn" data-testid="calibration-label">{cov!.label}</em>}</div>
          <div className="gx-fp-label">Model</div>
          <div className="gx-fp-model" data-testid="strip-model">{m.text}</div>
          <div className="gx-fp-label">Issued · inputs</div>
          <div className="gx-fp-sub" data-testid="strip-ref">Issued {issuedAt(f)}. Input data: {inputLine(f, baseIso)}. Reference <b className="mono">{usd(f.reference_price)}</b>.</div>

          {today?.route === "baseline_reference" && today.daily_pending && <p className="gx-fp-note" data-testid="strip-sentence">{today.daily_pending}</p>}
          {completed && <p className="gx-completed" data-testid="completed-banner"><b>Completed</b> — {resultLine(p!.result)}</p>}

          <div className="gx-fp-perf" data-testid="strip-perf">
            <b>No demonstrated advantage</b>
            <span>{perfNote(shown!.target, p!)}</span>
            <button className="gx-link2" onClick={() => onDetails("backtest")} data-testid="details-link">Open backtest →</button>
          </div>
          <a className="gx-fp-export" href={api.exportForecastUrl(f.forecast_id)} download data-testid="download">Export forecast (CSV)</a>
        </section>
      )}

    </aside>
  );
}

/** Today's (or the last) finished settlement and its forecast, SEPARATE from the forecast for the next one. Pending until the official close is posted. */
export function SettlementNote({ shown }: { shown: Shown | null }) {
  const rec = shown?.target === "next" ? (shown.payload as TodayPayload).last_result : null;
  if (!rec) return null;
  const sl = settlementLine(rec);
  return (
    <section className="gx-settle" data-testid="settlement-record" data-status={rec.status}>
      <span className="gx-fp-label">{sl.title}</span>
      <p>{sl.text}</p>
    </section>
  );
}
