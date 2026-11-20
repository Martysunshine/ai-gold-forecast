import { useState } from "react";
import type { Conn, InstrumentKey, MarketSummary, OhlcPayload, RangeKey } from "../types";
import { INTERVAL_WORDS } from "../chartKit";
import { fmtAgo } from "../format";
import { FORECAST_FOOT, type Shown } from "../forecastText";
import { MainChart } from "./MainChart";

const RANGES: RangeKey[] = ["1D", "1W", "1M", "3M", "1Y", "5Y"];

/** The chart: history range, Line/Candles, Fit/Hide forecast, and the chart the stored forecast is drawn on. Choosing and running a forecast is in the side panel. */
export function ChartCard({ instrument, summary, range, onRange, mode, onMode, shown, onHide, fitToken, onFit, onConn, conn }: {
  instrument: InstrumentKey; summary: MarketSummary | null; range: RangeKey; onRange: (r: RangeKey) => void; mode: "line" | "candles"; onMode: (m: "line" | "candles") => void;
  shown: Shown | null; onHide: () => void; fitToken: number; onFit: () => void; onConn: (c: Conn) => void; conn: Conn;
}) {
  const [meta, setMeta] = useState<OhlcPayload | null>(null);
  const [info, setInfo] = useState(false);
  const forecastOn = !!shown && instrument === "futures";
  const fc = shown?.payload.forecast ?? null;
  const res = shown?.payload.result ?? null;

  const word = meta ? INTERVAL_WORDS[meta.interval] ?? meta.interval : "";
  const delay = meta?.delay_seconds;
  const st = summary?.status;
  const feed = conn === "offline" ? "Offline" : conn === "reconnecting" ? "Reconnecting…" : st ? st.label : "…";
  const hint = forecastOn && fc
    ? FORECAST_FOOT
    : meta?.time_kind === "day" ? `Each ${mode === "line" ? "point is a " : "candle is a "}${word} ${mode === "line" ? "close" : "(open, high, low, close)"}.`
      : mode === "line" ? `Each point is a ${word} close.` : `Each candle covers ${word === "5-minute" ? "5 minutes" : `one ${word.replace("-", " ")} period`}: open, high, low, close.`;

  return (
    <section className="gx-chartcard" aria-label="Chart" data-testid="chart-card">
      <div className="gx-controls">
        <div className="gx-seg" role="tablist" aria-label="Range">
          {RANGES.map((r) => <button key={r} role="tab" aria-selected={range === r} className={range === r ? "on" : ""} onClick={() => onRange(r)} data-testid={`range-${r}`}>{r}</button>)}
        </div>
        <div className="gx-controls-right">
          {forecastOn && <>
            <button className="gx-textbtn" onClick={onFit} data-testid="fit-forecast">Fit forecast</button>
            <button className="gx-textbtn" onClick={onHide} data-testid="hide-forecast">Hide forecast</button>
          </>}
          <div className="gx-seg" role="tablist" aria-label="Chart type">
            {(["candles", "line"] as const).map((m) => <button key={m} role="tab" aria-selected={mode === m} className={mode === m ? "on" : ""} onClick={() => onMode(m)} data-testid={`mode-${m}`}>{m === "candles" ? "Candles" : "Line"}</button>)}
          </div>
          <button className={`gx-icon sm ${info ? "on" : ""}`} aria-expanded={info} aria-label="About this data" onClick={() => setInfo(!info)} data-testid="chart-info">i</button>
        </div>
      </div>

      <div className="gx-controls sub">
        <span className="gx-feed" data-testid="feed-status"><i className={`gx-dot ${st?.state === "live" ? "ok" : st?.state === "delayed" ? "warn" : st?.state === "stale" ? "bad" : "off"}`} />{feed}{summary ? ` · refreshes every ${summary.update_every_seconds >= 15 ? 15 : summary.update_every_seconds} s` : ""}</span>
      </div>
      {info && meta && (
        <div className="gx-infopanel" data-testid="chart-info-panel">
          <p><b>Source:</b> {meta.source}. {delay != null && delay > 120 ? `This feed is about ${Math.round(delay / 60)} minutes behind the market and is not real-time.` : delay != null ? `Last tick ${fmtAgo(delay)}.` : ""}</p>
          <p><b>Session:</b> {summary?.session.convention}</p>
          <p><b>Gaps:</b> {meta.gap_policy}</p>
          <p><b>Time zone:</b> all times are New York (ET).</p>
        </div>
      )}
      <MainChart instrument={instrument} range={range} mode={mode} forecast={forecastOn ? fc : null} result={forecastOn ? res : null} fitToken={fitToken} onMeta={setMeta} onConn={onConn} />
      <p className="gx-foot" data-testid="chart-foot">{hint}</p>
    </section>
  );
}
