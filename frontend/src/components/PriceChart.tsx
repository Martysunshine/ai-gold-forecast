import { useMemo, useState } from "react";
import { CartesianGrid, ComposedChart, Line, ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Prediction, PricePayload } from "../types";
import { fmtDate, fmtTs, niceAxis, parseDay, pct, spacedTicks, usd } from "../format";
import { useElementWidth } from "../hooks";

const RANGES = [{ id: "3M", n: 63 }, { id: "6M", n: 126 }, { id: "1Y", n: 252 }, { id: "5Y", n: Infinity }] as const;
type RangeId = (typeof RANGES)[number]["id"];

interface Row { t: number; close?: number; chg?: number; fc?: number; target?: boolean }

function Tip({ active, payload }: { active?: boolean; payload?: { payload: Row }[] }) {
  if (!active || !payload?.length) return null;
  const r = payload[0].payload;
  return (
    <div className="chart-tip mono">
      <div className="tt-date">{fmtTs(r.t, { weekday: "short", day: "2-digit", month: "short", year: "numeric" })}</div>
      {r.target ? (
        <div className="tt-row"><span>Forecast</span><b>{usd(r.fc!)}</b></div>
      ) : (
        <>
          <div className="tt-row"><span>Close</span><b>{usd(r.close!)}</b></div>
          {r.chg !== undefined && <div className="tt-row"><span>Day</span><span className={r.chg >= 0 ? "up" : "down"}>{pct(r.chg * 100)}</span></div>}
        </>
      )}
    </div>
  );
}

export function PriceChart({ prices, result }: { prices: PricePayload; result: Prediction | null }) {
  const [range, setRange] = useState<RangeId>("6M");
  const [hover, setHover] = useState<Row | null>(null);
  const [boxRef, width] = useElementWidth<HTMLDivElement>();

  const m = useMemo(() => {
    const n = RANGES.find((r) => r.id === range)!.n;
    const all = prices.prices;
    const start = Math.max(0, all.length - n);
    const rows: Row[] = all.slice(start).map((p, i) => {
      const prev = start + i > 0 ? all[start + i - 1].close : undefined;
      return { t: parseDay(p.date), close: p.close, chg: prev ? p.close / prev - 1 : undefined };
    });
    const lastT = rows[rows.length - 1].t;
    const vals = rows.map((r) => r.close!);
    let targetT: number | null = null, pad = 0;
    const lo = result ? Math.min(...vals, result.forecast.interval.low) : Math.min(...vals);
    const hi = result ? Math.max(...vals, result.forecast.interval.high) : Math.max(...vals);
    const span = rows[rows.length - 1].t - rows[0].t;
    if (result) {
      targetT = parseDay(result.target_date);
      pad = Math.max(86400000 * 1.2, span * 0.012);
      rows[rows.length - 1] = { ...rows[rows.length - 1], fc: result.base_price };
      rows.push({ t: targetT, fc: result.forecast.price, target: true });
    }
    const margin = (hi - lo) * 0.04;
    const axis = niceAxis(lo - margin, hi + margin, 5);
    return { rows, lastT, targetT, pad, domain: axis.domain, yTicks: axis.ticks, firstT: rows[0].t, endT: (targetT ?? lastT) + pad };
  }, [prices, result, range]);

  const nTicks = width < 460 ? 4 : width < 760 ? 5 : 7;
  const ticks = spacedTicks(m.firstT, m.endT, nTicks);
  const dateFmt: Intl.DateTimeFormatOptions = range === "5Y" ? { month: "short", year: "2-digit" } : { day: "2-digit", month: "short" };
  const shown = hover && !hover.target ? hover : null;
  const lastRow = m.rows.filter((r) => !r.target).slice(-1)[0];
  const read = shown ?? lastRow;

  return (
    <div className="chart-block" data-testid="price-chart">
      <div className="chart-head">
        <div className="readout mono" aria-live="off" data-testid="readout">
          <span className="muted">{fmtTs(read.t, { weekday: "short", day: "2-digit", month: "short", year: "numeric" })}</span>
          <span className="rd-price">{usd(read.close!)}</span>
          {read.chg !== undefined && <span className={read.chg >= 0 ? "up" : "down"}>{pct(read.chg * 100)}</span>}
          {!shown && <span className="muted">latest close</span>}
        </div>
        <div className="seg" role="tablist" aria-label="History range">
          {RANGES.map((r) => (
            <button key={r.id} role="tab" aria-selected={range === r.id} className={range === r.id ? "on" : ""} onClick={() => setRange(r.id)}>{r.id}</button>
          ))}
        </div>
      </div>

      <div className="legend">
        <span><i className="lg solid" />Historical close</span>
        {result && <span><i className="lg dashed" />Forecast origin → target <em>(straight connector, not a modelled path)</em></span>}
        {result && <span><i className="lg range" />Target range, nominal {Math.round(result.forecast.interval.level * 100)}% <em>(at target session only)</em></span>}
      </div>

      <div className="chart-box" ref={boxRef} role="img"
        aria-label={`Gold futures daily close, ${fmtDate(prices.first_date)} to ${fmtDate(prices.latest.date)}. Latest close ${usd(prices.latest.close)}.${result ? ` Forecast for ${fmtDate(result.target_date)}: ${usd(result.forecast.price)}.` : ""}`}>
        <div className="chart-abs"><ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={m.rows} margin={{ top: 8, right: 4, bottom: 2, left: 4 }}
            onMouseMove={(s: { activePayload?: { payload: Row }[] }) => { const r = s?.activePayload?.[0]?.payload; if (r) setHover((h) => (h?.t === r.t ? h : r)); }}
            onMouseLeave={() => setHover(null)}>
            <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
            <XAxis dataKey="t" type="number" scale="time" domain={[m.firstT, m.endT]} ticks={ticks} padding={{ left: 26, right: 0 }} tickLine={false} stroke="var(--chart-axis)"
              tick={{ fill: "var(--ink-2)", fontSize: 12, fontFamily: "var(--font)" }} tickFormatter={(t: number) => fmtTs(t, dateFmt)} interval={0} />
            <YAxis domain={m.domain} ticks={m.yTicks} orientation="right" width={58} axisLine={false} tickLine={false} allowDecimals={false}
              tick={{ fill: "var(--ink-2)", fontSize: 12, fontFamily: "var(--font)" }} tickFormatter={(v: number) => v.toLocaleString("en-US")} />
            <Tooltip content={<Tip />} cursor={false} isAnimationActive={false} />
            {shown && <ReferenceLine x={shown.t} stroke="var(--ink-3)" strokeDasharray="2 3" />}
            {shown && <ReferenceLine y={shown.close} stroke="var(--ink-3)" strokeDasharray="2 3" />}
            {result && m.targetT !== null && (
              <>
                <ReferenceArea x1={m.lastT} x2={m.endT} fill="var(--forecast)" fillOpacity={0.04} stroke="none" />
                <ReferenceArea x1={m.targetT - m.pad} x2={m.targetT + m.pad} y1={result.forecast.interval.low} y2={result.forecast.interval.high}
                  fill="var(--forecast)" fillOpacity={0.2} stroke="var(--forecast)" strokeOpacity={0.7} strokeWidth={1} />
                <ReferenceLine x={m.lastT} stroke="var(--forecast)" strokeOpacity={0.7} strokeDasharray="4 3"
                  label={{ value: `origin ${fmtTs(m.lastT, { day: "2-digit", month: "short" })}`, position: "insideTopRight", fill: "var(--accent-ink)", fontSize: 12, fontFamily: "var(--font)" }} />
              </>
            )}
            <Line dataKey="close" stroke="var(--gold)" strokeWidth={2} dot={false} isAnimationActive={false} activeDot={{ r: 3.5, fill: "var(--gold)", stroke: "var(--surface)", strokeWidth: 2 }} />
            <Line dataKey="fc" stroke="var(--forecast)" strokeWidth={2} strokeDasharray="5 4" connectNulls isAnimationActive={false} activeDot={false}
              dot={(p: { cx?: number; cy?: number; payload?: Row; index?: number }) => {
                if (p.cx === undefined || p.cy === undefined || !p.payload) return <g key={p.index} />;
                if (p.payload.target) return <rect key={p.index} x={p.cx - 4} y={p.cy - 4} width={8} height={8} fill="var(--forecast)" />;
                return p.payload.fc !== undefined ? <circle key={p.index} cx={p.cx} cy={p.cy} r={3.5} fill="var(--surface)" stroke="var(--forecast)" strokeWidth={1.6} /> : <g key={p.index} />;
              }} />
          </ComposedChart>
        </ResponsiveContainer></div>
      </div>
      <p className="micro mono chart-foot">
        {prices.count.toLocaleString()} daily closes · {fmtDate(prices.first_date)} → {fmtDate(prices.latest.date)}
        {result && ` · forecast target ${fmtDate(result.target_date)} (session ${result.horizon} on the exchange calendar)`}
      </p>
    </div>
  );
}
