import { useMemo, useState } from "react";
import { Area, CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Forecast, Horizon, PricePayload, Scenario } from "../types";
import { fmtDate, fmtTs, parseDay, pct, usd } from "../format";
import { scenarioPrice } from "../scenario";

const RANGES = [
  { id: "3M", n: 63 },
  { id: "6M", n: 126 },
  { id: "1Y", n: 252 },
  { id: "5Y", n: Infinity },
] as const;

interface Row {
  t: number;
  close?: number;
  fc?: number;
  band?: [number, number];
  scen?: number;
  isForecast?: boolean;
  horizon?: Horizon;
}

function Tip({ active, payload }: { active?: boolean; payload?: { payload: Row }[] }) {
  if (!active || !payload?.length) return null;
  const r = payload[0].payload;
  return (
    <div className="tip">
      <div className="tip-date">{fmtTs(r.t, { weekday: "short", day: "numeric", month: "short", year: "numeric" })}</div>
      {r.isForecast ? (
        <>
          <div className="tip-row"><span>{r.horizon}-session forecast</span><b>{usd(r.fc!)}</b></div>
          <div className="tip-row muted"><span>80% range</span><span>{usd(r.band![0], 0)} – {usd(r.band![1], 0)}</span></div>
          {r.scen !== undefined && <div className="tip-row gold"><span>Scenario (assumption)</span><span>{usd(r.scen)}</span></div>}
        </>
      ) : (
        r.close !== undefined && <div className="tip-row"><span>Close</span><b>{usd(r.close)}</b></div>
      )}
    </div>
  );
}

export function PriceChart({ prices, forecasts, horizon, scenario }: { prices: PricePayload; forecasts: Forecast[] | null; horizon: Horizon; scenario: Scenario }) {
  const [range, setRange] = useState<(typeof RANGES)[number]["id"]>("6M");
  const [view, setView] = useState<"chart" | "table">("chart");

  const { rows, domain, forecastRows } = useMemo(() => {
    const n = RANGES.find((r) => r.id === range)!.n;
    const hist = prices.prices.slice(-n);
    const out: Row[] = hist.map((p) => ({ t: parseDay(p.date), close: p.close }));
    const fRows: Row[] = [];
    if (forecasts?.length) {
      const last = prices.latest;
      out[out.length - 1] = { ...out[out.length - 1], fc: last.close, band: [last.close, last.close] };
      for (const f of forecasts) {
        const row: Row = {
          t: parseDay(f.target_date), fc: f.forecast_price, band: [f.interval.low, f.interval.high], isForecast: true, horizon: f.horizon,
          scen: scenario === "base" ? undefined : scenarioPrice(f, scenario),
        };
        out.push(row);
        fRows.push(row);
      }
    }
    const vals = out.flatMap((r) => [r.close, r.band?.[0], r.band?.[1], r.scen]).filter((v): v is number => v !== undefined);
    const lo = Math.min(...vals), hi = Math.max(...vals), pad = (hi - lo) * 0.08;
    return { rows: out, domain: [Math.floor(lo - pad), Math.ceil(hi + pad)] as [number, number], forecastRows: fRows };
  }, [prices, forecasts, range, scenario]);

  const ticks = useMemo(() => {
    const lo = rows[0].t, hi = rows[rows.length - 1].t, n = 5;
    return Array.from({ length: n + 1 }, (_, i) => Math.round(lo + ((hi - lo) * i) / n));
  }, [rows]);
  const selected = forecastRows.find((r) => r.horizon === horizon);
  const lastT = parseDay(prices.latest.date);

  return (
    <section className="card chart-card" aria-label="Historical price chart with forecast">
      <div className="card-head">
        <div>
          <h2>Price history &amp; forecast</h2>
          <p className="sub">Daily closes, USD per troy ounce · {prices.instrument.kind === "futures" ? "futures" : "uploaded series"}</p>
        </div>
        <div className="controls">
          <div className="seg" role="tablist" aria-label="History range">
            {RANGES.map((r) => (
              <button key={r.id} role="tab" aria-selected={range === r.id} className={range === r.id ? "on" : ""} onClick={() => setRange(r.id)}>{r.id}</button>
            ))}
          </div>
          <div className="seg" role="tablist" aria-label="View">
            <button role="tab" aria-selected={view === "chart"} className={view === "chart" ? "on" : ""} onClick={() => setView("chart")}>Chart</button>
            <button role="tab" aria-selected={view === "table"} className={view === "table" ? "on" : ""} onClick={() => setView("table")}>Table</button>
          </div>
        </div>
      </div>

      <div className="legend">
        <span><i className="sw line" /> Historical close</span>
        {forecasts && <span><i className="sw dash" /> Model forecast</span>}
        {forecasts && <span><i className="sw band" /> 80% range (out-of-sample errors)</span>}
        {scenario !== "base" && <span><i className="sw scen" /> {scenario === "bullish" ? "Bullish" : "Bearish"} scenario (assumption)</span>}
      </div>

      {view === "chart" ? (
        <div className="chart-wrap">
          <ResponsiveContainer width="100%" height={380}>
            <ComposedChart data={rows} margin={{ top: 10, right: 12, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="var(--grid)" vertical={false} />
              <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} ticks={ticks} stroke="var(--grid)"
                tick={{ fill: "var(--muted)", fontSize: 12 }} tickLine={false}
                tickFormatter={(t: number) => fmtTs(t, range === "5Y" ? { month: "short", year: "2-digit" } : { day: "numeric", month: "short" })} />
              <YAxis domain={domain} orientation="right" width={56} tick={{ fill: "var(--muted)", fontSize: 12 }} axisLine={false} tickLine={false}
                tickFormatter={(v: number) => v.toLocaleString("en-US")} />
              <Tooltip content={<Tip />} cursor={{ stroke: "var(--muted)", strokeDasharray: "3 3" }} isAnimationActive={false} />
              <ReferenceLine x={lastT} stroke="var(--line-soft)" strokeDasharray="2 4" />
              <Area dataKey="band" stroke="none" fill="var(--gold)" fillOpacity={0.18} isAnimationActive={false} connectNulls />
              <Line dataKey="close" stroke="var(--gold)" strokeWidth={2} dot={false} isAnimationActive={false} activeDot={{ r: 4, stroke: "var(--surface)", strokeWidth: 2 }} />
              <Line dataKey="fc" stroke="var(--ivory)" strokeWidth={2} strokeDasharray="5 4" connectNulls isAnimationActive={false}
                dot={(p: { cx?: number; cy?: number; payload?: Row; index?: number }) => {
                  const r = p.payload;
                  if (!r?.isForecast || p.cx === undefined || p.cy === undefined) return <g key={p.index} />;
                  const on = r.horizon === horizon;
                  return <circle key={p.index} cx={p.cx} cy={p.cy} r={on ? 6 : 4} fill={on ? "var(--ivory)" : "var(--surface)"} stroke="var(--ivory)" strokeWidth={2} />;
                }} />
              {scenario !== "base" && <Line dataKey="scen" stroke="var(--scen)" strokeWidth={2} strokeDasharray="1 4" strokeLinecap="round" dot={{ r: 3, fill: "var(--scen)" }} connectNulls isAnimationActive={false} />}
            </ComposedChart>
          </ResponsiveContainer>
          {selected && (
            <p className="chart-note">
              Selected: <b>{selected.horizon}-session</b> forecast for ≈{fmtTs(selected.t, { day: "numeric", month: "short", year: "numeric" })} — {usd(selected.fc!)} (range {usd(selected.band![0], 0)} – {usd(selected.band![1], 0)}). Forecast dates skip weekends but not exchange holidays.
            </p>
          )}
        </div>
      ) : (
        <div className="table-wrap" tabIndex={0}>
          <table>
            <caption className="sr">Last 30 observations and forecasts</caption>
            <thead><tr><th>Date</th><th className="num">Close / forecast</th><th className="num">80% range</th></tr></thead>
            <tbody>
              {[...rows.filter((r) => r.isForecast).reverse(), ...rows.filter((r) => !r.isForecast).slice(-30).reverse()].map((r) => (
                <tr key={r.t + String(r.isForecast)} className={r.isForecast ? "fc-row" : ""}>
                  <td>{fmtTs(r.t, { day: "numeric", month: "short", year: "numeric" })}{r.isForecast && <span className="tag">forecast · {r.horizon} sessions</span>}</td>
                  <td className="num">{usd(r.isForecast ? r.fc! : r.close!)}</td>
                  <td className="num">{r.isForecast ? `${usd(r.band![0], 0)} – ${usd(r.band![1], 0)}` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="foot-note">
        Range: {fmtDate(prices.first_date)} → {fmtDate(prices.latest.date)} · {prices.count.toLocaleString()} observations. Change over the last session: {pct(prices.latest.change_pct)}.
      </p>
    </section>
  );
}
