import { useCallback, useEffect, useMemo, useState } from "react";
import { CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import type { GldComparison as Gld } from "../types";
import { fmtDate, fmtTs, niceAxis, num, parseDay, pct, spacedTicks, timeAgo, usd } from "../format";
import { useElementWidth } from "../hooks";
import { ErrorBlock, Info, Skeleton } from "./Common";

interface Row { t: number; gc: number; gld: number }

function Tip({ active, payload }: { active?: boolean; payload?: { payload: Row }[] }) {
  if (!active || !payload?.length) return null;
  const r = payload[0].payload;
  return (
    <div className="chart-tip mono">
      <div className="tt-date">{fmtTs(r.t, { weekday: "short", day: "2-digit", month: "short", year: "numeric" })}</div>
      <div className="tt-row"><span>GC=F</span><b>{num(r.gc, 1)}</b></div>
      <div className="tt-row"><span className="muted">GLD</span><span>{num(r.gld, 1)}</span></div>
    </div>
  );
}

/** Loaded only when the tab is opened. GLD is context: a different instrument, never a model input. */
export function GldComparison() {
  const [data, setData] = useState<Gld | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [boxRef, width] = useElementWidth<HTMLDivElement>();

  const load = useCallback(async (refresh = false) => {
    setLoading(true); setError(null);
    try { setData(await api.gld(refresh)); } catch (e) { setError(e instanceof Error ? e.message : "Request failed."); } finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const rows = useMemo<Row[]>(() => data?.comparison?.indexed.map((r) => ({ t: parseDay(r.date), gc: r.gc, gld: r.gld })) ?? [], [data]);

  const note = <p className="notice" data-testid="gld-note">GLD is an exchange-traded fund, not the COMEX futures contract. It is shown for comparison only and is <b>not</b> used by the forecast.</p>;
  if (loading && !data) return <div>{note}<Skeleton h={18} w="60%" /><div style={{ height: 12 }} /><Skeleton h={240} /></div>;
  if (error) return <div data-testid="gld">{note}<ErrorBlock title="GLD data unavailable" message={error} onRetry={() => load(true)} /><p className="micro">The forecast and backtest are unaffected.</p></div>;
  if (!data) return null;
  if (!data.configured) {
    return (
      <div data-testid="gld">
        {note}
        <div className="setup" data-testid="gld-setup">
          <h3>Optional: enable the GLD comparison</h3>
          <ol>{data.setup?.map((s) => <li key={s}>{s}</li>)}</ol>
          <p className="micro">Keys stay on the server. They are read from <code>.env</code> (git-ignored), sent only to Alpaca, and never shown in the interface or API responses.</p>
        </div>
      </div>
    );
  }
  const c = data.comparison!, w = c.window, l = data.latest!;
  const vals = rows.flatMap((r) => [r.gc, r.gld]);
  const axis = niceAxis(Math.min(...vals), Math.max(...vals), 5);
  const ticks = spacedTicks(rows[0].t, rows[rows.length - 1].t, width < 460 ? 4 : 6);
  return (
    <div data-testid="gld">
      {note}
      {data.provider_error && <p className="notice warn">Refresh failed: {data.provider_error} Showing GLD data from {timeAgo(data.fetched_at!)}.</p>}
      <div className="bt-metrics">
        <div className="metric"><div className="label">GLD close <Info label="GLD close" text="Latest completed daily close of SPDR Gold Shares (GLD) on NYSE Arca, USD per share. An in-progress session is excluded." /></div>
          <div className="mono mval" data-testid="gld-close">{usd(l.close)}</div><div className="micro mono">{fmtDate(l.date, { day: "2-digit", month: "short", year: "numeric" })} · {pct(l.change_pct)}</div></div>
        <div className="metric"><div className="label">Return correlation <Info label="Return correlation" text={`Correlation of daily log returns of GC=F and GLD over the last ${w.sessions} common sessions. 1 means they moved identically day to day.`} /></div>
          <div className="mono mval" data-testid="gld-corr">{w.return_correlation.toFixed(2)}</div><div className="micro mono">{w.sessions} sessions</div></div>
        <div className="metric"><div className="label">GC=F return <Info label="GC=F return" text="Change in the futures close over the same window." /></div>
          <div className="mono mval">{pct(w.gc_return_pct, 1)}</div><div className="micro mono">{fmtDate(w.start, { month: "short", year: "numeric" })} – {fmtDate(w.end, { month: "short", year: "numeric" })}</div></div>
        <div className="metric"><div className="label">GLD return <Info label="GLD return" text="Change in the GLD close over the same window. Differences from GC=F come from fees, futures roll and contango, and trading hours." /></div>
          <div className="mono mval">{pct(w.gld_return_pct, 1)}</div><div className="micro mono">{data.status?.label} · {data.source}</div></div>
      </div>
      <div className="legend">
        <span><i className="lg solid" />GC=F (futures)</span>
        <span><i className="lg muted-solid" />GLD (ETF)</span>
        <em className="micro">both indexed to 100 on {fmtDate(c.start)}; {c.common_dates.toLocaleString()} common dates</em>
      </div>
      <div className="chart-box short" ref={boxRef} role="img" aria-label={`GC=F and GLD indexed to 100 from ${fmtDate(c.start)} to ${fmtDate(c.end)}. Return correlation ${w.return_correlation.toFixed(2)}.`}>
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={rows} margin={{ top: 8, right: 4, bottom: 2, left: 4 }}>
            <CartesianGrid stroke="var(--grid)" vertical={false} />
            <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} ticks={ticks} interval={0} padding={{ left: 26, right: 0 }} tickLine={false} stroke="var(--border)"
              tick={{ fill: "var(--muted)", fontSize: 11, fontFamily: "var(--mono)" }} tickFormatter={(v: number) => fmtTs(v, { month: "short", year: "2-digit" })} />
            <YAxis domain={axis.domain} ticks={axis.ticks} orientation="right" width={48} axisLine={false} tickLine={false}
              tick={{ fill: "var(--muted)", fontSize: 11, fontFamily: "var(--mono)" }} />
            <Tooltip content={<Tip />} cursor={{ stroke: "var(--muted)", strokeDasharray: "2 3" }} isAnimationActive={false} />
            <Line dataKey="gld" stroke="var(--muted)" strokeWidth={1.4} dot={false} isAnimationActive={false} />
            <Line dataKey="gc" stroke="var(--gold)" strokeWidth={1.6} dot={false} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <div className="row-left"><button className="btn small" onClick={() => load(true)} disabled={loading}>{loading ? "Refreshing…" : "Refresh GLD"}</button>
        <span className="micro mono">{data.count?.toLocaleString()} GLD closes · fetched {data.fetched_at ? timeAgo(data.fetched_at) : "—"}</span></div>
    </div>
  );
}
