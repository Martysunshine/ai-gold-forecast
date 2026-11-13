import { useMemo, useState } from "react";
import { Area, CartesianGrid, ComposedChart, Line, ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Prediction, PricePayload } from "../types";
import { fmtTs, parseDay, usd } from "../format";

const RANGES = [
  { id: "3M", n: 63 },
  { id: "6M", n: 126 },
  { id: "1Y", n: 252 },
  { id: "5Y", n: Infinity },
] as const;

interface Row { t: number; close?: number; fc?: number; band?: [number, number]; isForecast?: boolean }

function Tip({ active, payload }: { active?: boolean; payload?: { payload: Row }[] }) {
  if (!active || !payload?.length) return null;
  const r = payload[0].payload;
  return (
    <div className="tip">
      <div className="tip-date">{fmtTs(r.t, { weekday: "short", day: "numeric", month: "short", year: "numeric" })}</div>
      {r.isForecast ? (
        <>
          <div className="tip-row"><span>Predicted close</span><b>{usd(r.fc!)}</b></div>
          <div className="tip-row muted"><span>Nominal 80% range</span><span>{usd(r.band![0], 0)} – {usd(r.band![1], 0)}</span></div>
        </>
      ) : r.close !== undefined && <div className="tip-row"><span>Close</span><b>{usd(r.close)}</b></div>}
    </div>
  );
}

export function PriceChart({ prices, prediction }: { prices: PricePayload; prediction: Prediction | null }) {
  const [range, setRange] = useState<(typeof RANGES)[number]["id"]>("6M");
  const [view, setView] = useState<"chart" | "table">("chart");

  const { rows, domain, ticks, lastT, fcT } = useMemo(() => {
    const n = RANGES.find((r) => r.id === range)!.n;
    const out: Row[] = prices.prices.slice(-n).map((p) => ({ t: parseDay(p.date), close: p.close }));
    const lastT = out[out.length - 1].t;
    let fcT: number | null = null;
    if (prediction) {
      const f = prediction.forecast;
      out[out.length - 1] = { ...out[out.length - 1], fc: prices.latest.close, band: [prices.latest.close, prices.latest.close] };
      fcT = parseDay(prediction.target_date);
      out.push({ t: fcT, fc: f.price, band: [f.interval.low, f.interval.high], isForecast: true });
    }
    const vals = out.flatMap((r) => [r.close, r.band?.[0], r.band?.[1]]).filter((v): v is number => v !== undefined);
    const lo = Math.min(...vals), hi = Math.max(...vals), pad = (hi - lo) * 0.08;
    const first = out[0].t, last = out[out.length - 1].t;
    return {
      rows: out, lastT, fcT,
      domain: [Math.floor(lo - pad), Math.ceil(hi + pad)] as [number, number],
      ticks: Array.from({ length: 6 }, (_, i) => Math.round(first + ((last - first) * i) / 5)),
    };
  }, [prices, prediction, range]);

  return (
    <section className="card chart-card" aria-label="Historical price chart">
      <div className="card-head">
        <div>
          <h2>Gold price history{prediction ? " and forecast" : ""}</h2>
          <p className="sub">Daily closes · COMEX gold futures · USD per troy ounce</p>
        </div>
        <div className="controls">
          <div className="seg" role="tablist" aria-label="History range">
            {RANGES.map((r) => (<button key={r.id} role="tab" aria-selected={range === r.id} className={range === r.id ? "on" : ""} onClick={() => setRange(r.id)}>{r.id}</button>))}
          </div>
          <div className="seg" role="tablist" aria-label="View">
            <button role="tab" aria-selected={view === "chart"} className={view === "chart" ? "on" : ""} onClick={() => setView("chart")}>Chart</button>
            <button role="tab" aria-selected={view === "table"} className={view === "table" ? "on" : ""} onClick={() => setView("table")}>Table</button>
          </div>
        </div>
      </div>
      <div className="legend">
        <span><i className="sw line" /> Historical close</span>
        {prediction && <span><i className="sw dash" /> Predicted close</span>}
        {prediction && <span><i className="sw band" /> Nominal 80% range</span>}
      </div>

      {view === "chart" ? (
        <ResponsiveContainer width="100%" height={360}>
          <ComposedChart data={rows} margin={{ top: 10, right: 12, bottom: 0, left: 0 }} accessibilityLayer>
            <CartesianGrid stroke="var(--grid)" vertical={false} />
            <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} ticks={ticks} stroke="var(--grid)" tick={{ fill: "var(--muted)", fontSize: 12 }} tickLine={false}
              tickFormatter={(t: number) => fmtTs(t, range === "5Y" ? { month: "short", year: "2-digit" } : { day: "numeric", month: "short" })} />
            <YAxis domain={domain} orientation="right" width={56} tick={{ fill: "var(--muted)", fontSize: 12 }} axisLine={false} tickLine={false} tickFormatter={(v: number) => v.toLocaleString("en-US")} />
            <Tooltip content={<Tip />} cursor={{ stroke: "var(--muted)", strokeDasharray: "3 3" }} isAnimationActive={false} />
            {fcT !== null && <ReferenceArea x1={lastT} x2={fcT} fill="var(--gold)" fillOpacity={0.05} stroke="none" />}
            {fcT !== null && <ReferenceLine x={lastT} stroke="var(--line-soft)" strokeDasharray="3 3" label={{ value: "forecast →", position: "insideTopRight", fill: "var(--muted)", fontSize: 12 }} />}
            <Area dataKey="band" stroke="none" fill="var(--gold)" fillOpacity={0.2} isAnimationActive={false} connectNulls />
            <Line dataKey="close" stroke="var(--gold)" strokeWidth={2} dot={false} isAnimationActive={false} activeDot={{ r: 4, stroke: "var(--surface)", strokeWidth: 2 }} />
            <Line dataKey="fc" stroke="var(--ivory)" strokeWidth={2} strokeDasharray="5 4" connectNulls isAnimationActive={false}
              dot={(p: { cx?: number; cy?: number; payload?: Row; index?: number }) =>
                p.payload?.isForecast && p.cx !== undefined && p.cy !== undefined
                  ? <circle key={p.index} cx={p.cx} cy={p.cy} r={6} fill="var(--ivory)" stroke="var(--ivory)" strokeWidth={2} />
                  : <g key={p.index} />} />
          </ComposedChart>
        </ResponsiveContainer>
      ) : (
        <div className="table-wrap" tabIndex={0}>
          <table>
            <caption className="sr">Recent closes{prediction ? " and the forecast" : ""}</caption>
            <thead><tr><th>Date</th><th className="num">Close / predicted</th><th className="num">Nominal 80% range</th></tr></thead>
            <tbody>
              {[...rows.filter((r) => r.isForecast), ...rows.filter((r) => !r.isForecast).slice(-30).reverse()].map((r) => (
                <tr key={r.t + String(r.isForecast)} className={r.isForecast ? "fc-row" : ""}>
                  <td>{fmtTs(r.t, { day: "numeric", month: "short", year: "numeric" })}{r.isForecast && <span className="tag">predicted</span>}</td>
                  <td className="num">{usd(r.isForecast ? r.fc! : r.close!)}</td>
                  <td className="num">{r.isForecast ? `${usd(r.band![0], 0)} – ${usd(r.band![1], 0)}` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="foot-note">{prices.count.toLocaleString()} daily closes, {fmtTs(parseDay(prices.first_date), { day: "numeric", month: "short", year: "numeric" })} → {fmtTs(parseDay(prices.latest.date), { day: "numeric", month: "short", year: "numeric" })}.
        {prediction && " The forecast date skips weekends but not exchange holidays, so it may be a day early."}</p>
    </section>
  );
}
