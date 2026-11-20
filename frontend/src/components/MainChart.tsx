import { useEffect, useRef, useState } from "react";
import { AreaSeries, CandlestickSeries, LineSeries, LineStyle, LineType, createSeriesMarkers, type IChartApi, type ISeriesApi, type ISeriesMarkersPluginApi, type Time } from "lightweight-charts";
import { api } from "../api";
import type { Bar, Conn, ForecastResult, FrozenForecast, InstrumentKey, OhlcPayload, RangeKey } from "../types";
import { C, INTERVAL_WORDS, STEP, byTime, futureSlots, gapSlots, makeChart, timeLabel } from "../chartKit";
import { resolveLabels, type LabelIn, type LabelOut } from "../labels";
import { fmtIsoEt, num } from "../format";

const POLL_MS = 15000;
const etDay = (epochSec: number) => new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date(epochSec * 1000));
const textW = (s: string, px = 7.5) => Math.ceil(s.length * px + 18);
const asc = (a: { time: number | string }, b: { time: number | string }) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0);

interface Pt { x: number; y: number }
type Lbl = LabelOut & { text: string; sub?: string; sub2?: string; kind: string };
interface Geometry { w: number; h: number; labels: Lbl[]; bar: { x: number; y1: number; y2: number } | null }
interface Win { lastTime: number | string; originKey: number | string; targetKey: number | string; K: number; from: number | string; to: number | string }

/** A key `pad` bars after ``key``: gives the target's labels room on its right without stretching the axis. */
function extendKey(key: number | string, kind: "epoch" | "day", interval: string, pad: number): number | string {
  if (kind === "epoch") return (key as number) + pad * (STEP[interval] ?? 3600);
  const stepDays = interval === "1wk" ? 7 : 1.45;
  return new Date(Date.parse(key + "T00:00:00Z") + Math.ceil(pad * stepDays) * 86400000).toISOString().slice(0, 10);
}

/**
 * The one main chart. Observed prices are always real bars (a 2px line or actual candles). When a forecast has been generated it is
 * drawn on THIS chart: a marker at the forecast origin, a dashed "illustrative" connector (not a predicted path), a gold dot at the
 * target with its price and date, and the prediction range. No intermediate candles or values are ever invented: the connector's
 * in-between points are empty slots that only keep the time axis honest. Labels are placed by a collision resolver (they move; the
 * plotted prices never do). Zoom and scroll are preserved through quote updates, Line/Candles switches and settings changes; the view
 * is refitted only when a new forecast is generated (``fitToken``), on "Fit forecast", or when the forecast is hidden.
 */
export function MainChart({ instrument, range, mode, forecast, result, fitToken, onMeta, onConn }: {
  instrument: InstrumentKey; range: RangeKey; mode: "line" | "candles"; forecast: FrozenForecast | null; result: ForecastResult | null; fitToken: number;
  onMeta?: (m: OhlcPayload) => void; onConn?: (c: Conn) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candles = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const area = useRef<ISeriesApi<"Area"> | null>(null);
  const connector = useRef<ISeriesApi<"Line"> | null>(null);
  const pts = useRef<Record<string, ISeriesApi<"Line"> | null>>({});
  const mk = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const barMap = useRef<Map<number | string, Bar>>(new Map());
  const meta = useRef<OhlcPayload | null>(null);
  const win = useRef<Win | null>(null);
  const modeRef = useRef(mode);
  const [tip, setTip] = useState<{ x: number; y: number; rows: string[] } | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "empty" | "error">("loading");
  const [errText, setErrText] = useState<string | null>(null);
  const [geo, setGeo] = useState<Geometry | null>(null);
  const fRef = useRef(forecast); fRef.current = forecast;
  const rRef = useRef(result); rRef.current = result;
  modeRef.current = mode;
  const showF = instrument === "futures" && !!forecast;

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const chart = makeChart(el);
    candles.current = chart.addSeries(CandlestickSeries, { upColor: C.up, downColor: C.down, borderVisible: false, wickUpColor: C.up, wickDownColor: C.down, priceLineColor: C.accent, lastValueVisible: true, priceLineVisible: true });
    // 2px line, straight segments between real observations (no smoothing), with a very light area fill.
    area.current = chart.addSeries(AreaSeries, { lineColor: C.accent, lineWidth: 2, lineType: LineType.Simple, topColor: "rgba(214,184,120,0.16)", bottomColor: "rgba(214,184,120,0)", priceLineColor: C.accent, lastValueVisible: true, priceLineVisible: true, crosshairMarkerRadius: 4 });
    connector.current = chart.addSeries(LineSeries, { color: C.accent, lineStyle: LineStyle.Dashed, lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
    const dot = (color: string, r: number) => chart.addSeries(LineSeries, { color, lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: r, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
    pts.current = { pred: dot(C.accent, 5), low: dot(C.muted, 3), high: dot(C.muted, 3), actual: dot(C.line, 4) };
    mk.current = createSeriesMarkers(connector.current, []);
    chart.subscribeCrosshairMove((p) => {
      if (!p.point || !p.time || p.point.x < 0 || p.point.y < 0) return setTip(null);
      const m = meta.current;
      const word = m ? INTERVAL_WORDS[m.interval] ?? m.interval : "";
      const when = timeLabel(p.time);
      const c = p.seriesData.get(candles.current!) as { open?: number; high?: number; low?: number; close?: number } | undefined;
      const l = p.seriesData.get(area.current!) as { value?: number } | undefined;
      if (modeRef.current === "candles" && c && c.open != null) setTip({ x: p.point.x, y: p.point.y, rows: [when, `${word} candle`, `Open ${num(c.open)}  High ${num(c.high!)}`, `Low ${num(c.low!)}  Close ${num(c.close!)}`] });
      else if (modeRef.current === "line" && l && l.value != null) setTip({ x: p.point.x, y: p.point.y, rows: [when, `Price ${num(l.value)}`, `${word} close`] });
      else setTip(null);
    });
    chartRef.current = chart;
    return () => { chart.remove(); chartRef.current = null; };
  }, []);

  /** Set the data. Returns the forecast window (for fitting and labels) or null when no forecast is drawn. */
  const draw = () => {
    const m = meta.current, f = fRef.current;
    if (!m || !candles.current || !area.current) return;
    const bars = [...barMap.current.values()].sort(asc);
    if (!bars.length) return;
    const kind = m.time_kind;
    const lastTime = bars[bars.length - 1].time;
    const gaps = instrument === "spot" && kind === "epoch" ? gapSlots(bars, STEP[m.interval] ?? 300) : [];
    candles.current.setData(byTime([...bars.map((b) => ({ time: b.time as Time, open: b.open, high: b.high, low: b.low, close: b.close })), ...gaps]) as never);
    area.current.setData(byTime([...bars.map((b) => ({ time: b.time as Time, value: b.close })), ...gaps]) as never);
    for (const s of [connector.current, ...Object.values(pts.current)]) s?.setData([]);
    mk.current?.setMarkers([]);
    win.current = null;
    if (!f || instrument !== "futures") { win.current = null; return; }
    const targetKey: number | string = kind === "epoch" ? Math.round(Date.parse(f.target_time) / 1000) : f.target_date;
    let originKey: number | string = kind === "epoch" ? Math.floor(f.input_cutoff / (STEP[m.interval] ?? 3600)) * (STEP[m.interval] ?? 3600) : etDay(f.input_cutoff);
    if (originKey > lastTime) originKey = lastTime; // the forecast is newer than the (delayed) chart data
    const between = targetKey > lastTime ? futureSlots(lastTime, targetKey, kind, m.interval).filter((x) => x.time > lastTime && x.time < targetKey) : [];
    const K = between.length + 1;
    const pad = Math.max(3, Math.ceil(0.18 * K));
    const after = futureSlots(targetKey, extendKey(targetKey, kind, m.interval, pad), kind, m.interval).filter((x) => x.time > targetKey);
    // The connector needs strictly ascending times. A corrupt forecast with its target at or before its origin draws only the target.
    const line = originKey < targetKey
      ? [{ time: originKey as Time, value: f.reference_price }, ...between.filter((x) => x.time > originKey), { time: targetKey as Time, value: f.predicted }, ...after]
      : [{ time: targetKey as Time, value: f.predicted }, ...after];
    connector.current!.setData(line as never);
    pts.current.pred!.setData([{ time: targetKey as Time, value: f.predicted }]);
    pts.current.low!.setData([{ time: targetKey as Time, value: f.low }]);
    pts.current.high!.setData([{ time: targetKey as Time, value: f.high }]);
    const r = rRef.current;
    if (r?.matured && r.actual != null) pts.current.actual!.setData([{ time: targetKey as Time, value: r.actual }]);
    if (originKey < targetKey) mk.current?.setMarkers([{ time: originKey as Time, position: "inBar", shape: "circle", color: C.accent, size: 1 }]);
    const originIdx = Math.max(0, bars.findIndex((b) => b.time >= originKey));
    // History shown = enough to give the forecast context, but never so many bars that the chart would need less than ~3.5 px per bar.
    const cap = Math.max(24, Math.floor(((box.current?.clientWidth ?? 600) - 90) / 3.5) - K - pad);
    const hist = Math.min(bars.length, cap, Math.max(m.time_kind === "epoch" && m.interval === "5m" ? 60 : 30, Math.ceil(2.2 * K), bars.length - originIdx + 6));
    let from: number | string = bars[Math.max(0, bars.length - hist)].time;
    if (originKey < from) from = originKey;
    const to = after.length ? after[after.length - 1].time : targetKey;
    win.current = { lastTime, originKey, targetKey, K, from, to: to as number | string };
  };

  const fitView = () => {
    const ts = chartRef.current?.timeScale();
    if (!ts) return;
    const w = win.current;
    if (!w) return ts.fitContent();
    try { ts.setVisibleRange({ from: w.from as Time, to: w.to as Time }); } catch { ts.fitContent(); }
  };

  // Load history for the instrument and range. Re-fits (the old zoom no longer means anything for a different range).
  useEffect(() => {
    let dead = false;
    setState("loading"); setErrText(null); setGeo(null); setTip(null); barMap.current = new Map(); meta.current = null;
    api.ohlc(instrument, range).then((d) => {
      if (dead) return;
      meta.current = d;
      barMap.current = new Map(d.bars.map((b) => [b.time, b]));
      onMeta?.(d); onConn?.("ok");
      if (!d.bars.length) { setState("empty"); candles.current?.setData([]); area.current?.setData([]); return; }
      setState("ready");
      draw(); fitView();
    }).catch((e) => { if (!dead) { setState("error"); setErrText(e instanceof Error ? e.message : "Could not load the chart."); onConn?.("reconnecting"); } });
    return () => { dead = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instrument, range]);

  // A new forecast (or hiding it) redraws the overlay and refits once. Quote updates and Line/Candles switches never do.
  useEffect(() => { if (state === "ready") { draw(); fitView(); } /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [forecast?.forecast_id, state, instrument]);
  // The outcome arriving adds the actual-close dot without moving the view.
  useEffect(() => { if (state === "ready") draw(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [result?.matured]);
  // "Fit forecast" / a fresh Predict click: refit even if the forecast id is unchanged.
  useEffect(() => { if (state === "ready" && fitToken > 0) fitView(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [fitToken]);

  // Live bars: update() the newest candle only. The forecast and its window are unaffected, and zoom is preserved.
  useEffect(() => {
    const id = window.setInterval(async () => {
      if (document.hidden || !meta.current || !meta.current.bars.length) return;
      const tail = [...barMap.current.values()].slice(-3)[0];
      const since = typeof tail?.time === "number" ? tail.time - 1 : Date.now() / 1000 - 8 * 86400;
      try {
        const d = await api.ohlc(instrument, range, since);
        if (d.instrument !== instrument || d.range !== range || !candles.current || !area.current) return;
        const step = STEP[d.interval] ?? 300;
        const lastBefore = [...barMap.current.values()].sort(asc).slice(-1)[0]?.time;
        let last: number | string | undefined = win.current?.lastTime ?? lastBefore;
        for (const b of [...d.bars].sort(asc)) {
          if (last != null && b.time < last) continue; // a finalised bar is never rewritten
          const old = barMap.current.get(b.time);
          if (old && old.open === b.open && old.high === b.high && old.low === b.low && old.close === b.close) continue; // de-duplicate repeats
          if (instrument === "spot" && typeof last === "number" && typeof b.time === "number" && b.time - last > step * 1.5) {
            for (let t = last + step; t < b.time; t += step) { candles.current.update({ time: t as Time } as never); area.current.update({ time: t as Time } as never); }
          }
          barMap.current.set(b.time, b);
          candles.current.update({ time: b.time as Time, open: b.open, high: b.high, low: b.low, close: b.close });
          area.current.update({ time: b.time as Time, value: b.close });
          last = b.time;
          if (win.current) win.current.lastTime = b.time;
        }
        meta.current = { ...meta.current!, delay_seconds: d.delay_seconds };
        onMeta?.(meta.current); onConn?.("ok");
      } catch { onConn?.(navigator.onLine === false ? "offline" : "reconnecting"); }
    }, POLL_MS);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instrument, range]);

  useEffect(() => { candles.current?.applyOptions({ visible: mode === "candles" }); area.current?.applyOptions({ visible: mode === "line" }); setTip(null); }, [mode]);

  // Label geometry: recomputed ~8x/s from the chart's own coordinates, so labels follow zoom, scroll and resize.
  useEffect(() => {
    const id = window.setInterval(() => {
      const chart = chartRef.current, el = box.current, f = fRef.current, w = win.current, ser = candles.current;
      if (!chart || !el || !f || !w || !ser || instrument !== "futures") return setGeo((g) => (g ? null : g));
      const ts = chart.timeScale();
      const X = (t: number | string) => ts.timeToCoordinate(t as Time);
      const Y = (p: number) => ser.priceToCoordinate(p);
      const lastBar = [...barMap.current.values()].sort(asc).slice(-1)[0];
      const lx = X(w.lastTime), ly = lastBar ? Y(lastBar.close) : null;
      const ox = X(w.originKey), oy = Y(f.reference_price);
      const tx = X(w.targetKey), yp = Y(f.predicted), yl = Y(f.low), yh = Y(f.high);
      const axis = (() => { try { return chart.priceScale("right").width(); } catch { return 0; } })();
      const W = el.clientWidth - axis, H = el.clientHeight;  // labels live in the plot area only, never over the price axis
      if ([lx, ly, ox, oy, tx, yp, yl, yh].some((v) => v == null)) return setGeo((g) => (g ? null : g));
      const narrow = W < 460;
      const items: (LabelIn & { text: string; sub?: string; sub2?: string; kind: string })[] = [];
      const add = (id: string, p: Pt, side: LabelIn["side"], text: string, sub: string | undefined, dy: number, kind: string, sub2?: string) =>
        items.push({ id, x: p.x, y: p.y, w: Math.max(textW(text), sub ? textW(sub, 6) : 0, sub2 ? textW(sub2, 6) : 0), h: sub2 ? 50 : sub ? 36 : 22, side, dy, text, sub, sub2, kind });
      const up = f.predicted >= f.reference_price;
      add("latest", { x: lx!, y: ly! }, "right", `Latest ${num(lastBar.close)}`, undefined, 0, "hist");
      add("ref", { x: ox!, y: oy! }, "right", `Forecast origin ${num(f.reference_price)}`, narrow ? undefined : "reference when issued", up ? 30 : -30, "ref");
      // The target's date and time sit ON its price label, so the target date is never disconnected from its price.
      const when = `${fmtIsoEt(f.target_time, { weekday: "short", day: "numeric", month: "short" })} · ${fmtIsoEt(f.target_time, { hour: "2-digit", minute: "2-digit", hour12: false })} New York`;
      add("forecast", { x: tx!, y: yp! }, "right", num(f.predicted), when, up ? -34 : 34, "forecast", f.source.kind === "baseline" ? "Unchanged-price baseline" : "Model forecast");
      add("high", { x: tx!, y: yh! }, "right", `${Math.round(f.level * 100)}% high ${num(f.high)}`, undefined, -4, "range");
      add("low", { x: tx!, y: yl! }, "right", `${Math.round(f.level * 100)}% low ${num(f.low)}`, undefined, 4, "range");
      add("conn", { x: (ox! + tx!) / 2, y: (oy! + yp!) / 2 }, "center", "Illustrative connection", undefined, up ? 16 : -16, "conn");
      const r = rRef.current;
      if (r?.matured && r.actual != null && Y(r.actual) != null) add("actual", { x: tx!, y: Y(r.actual)! }, "right", `Actual ${num(r.actual)}`, undefined, 0, "actual");
      const labels = resolveLabels(items, { width: W, height: H }) as Lbl[];
      for (const l of labels) { const src = items.find((i) => i.id === l.id)!; l.text = src.text; l.sub = src.sub; l.sub2 = src.sub2; l.kind = src.kind; }
      setGeo((prev) => {
        const next: Geometry = { w: W, h: H, labels, bar: { x: tx!, y1: yh!, y2: yl! } };
        return prev && prev.w === W && prev.h === H && prev.bar?.x === next.bar!.x && prev.bar?.y1 === next.bar!.y1 && prev.bar?.y2 === next.bar!.y2 &&
          prev.labels.length === labels.length && prev.labels.every((p, i) => p.left === labels[i]?.left && p.top === labels[i]?.top && p.text === labels[i]?.text) ? prev : next;
      });
    }, 120);
    return () => window.clearInterval(id);
  }, [instrument, forecast?.forecast_id, result?.matured]);

  return (
    <div className="gx-chart" data-testid="chart" data-view={showF ? "forecast" : "history"}>
      <div ref={box} className="gx-chart-canvas" />
      {showF && geo && (
        <div className="gx-fc-layer" aria-hidden="true">
          <svg width={geo.w} height={geo.h}>
            {geo.bar && (<g stroke={C.muted} strokeWidth="1.5"><line x1={geo.bar.x} x2={geo.bar.x} y1={geo.bar.y1} y2={geo.bar.y2} /><line x1={geo.bar.x - 5} x2={geo.bar.x + 5} y1={geo.bar.y1} y2={geo.bar.y1} /><line x1={geo.bar.x - 5} x2={geo.bar.x + 5} y1={geo.bar.y2} y2={geo.bar.y2} /></g>)}
            {geo.labels.filter((l) => l.leader && l.kind !== "conn").map((l) => (
              <line key={l.id} x1={l.left + (l.x >= l.left + l.w / 2 ? l.w : 0)} y1={l.top + l.h / 2} x2={l.x} y2={l.y} stroke={C.muted} strokeWidth="1" strokeDasharray="2 2" />
            ))}
          </svg>
          {geo.labels.map((l) => (
            <div key={l.id} className={`gx-lbl ${l.kind}`} data-testid={`fc-label-${l.id}`} style={{ left: l.left, top: l.top, width: l.w, height: l.h }}>
              <b className="mono">{l.text}</b>{l.sub && <span>{l.sub}</span>}{l.sub2 && <span>{l.sub2}</span>}
            </div>
          ))}
        </div>
      )}
      {tip && <div className="gx-tip mono" style={{ left: Math.min(tip.x + 14, (box.current?.clientWidth ?? 300) - 190), top: Math.max(tip.y - 20, 4) }}>{tip.rows.map((r, i) => <div key={i}>{r}</div>)}</div>}
      {showF && state === "ready" && !geo && <div className="gx-chart-note" data-testid="offscreen-note">The forecast is outside the visible part of this chart. Press Fit forecast, or choose a longer history range (3M or more for 5 and 20 sessions).</div>}
      {state === "loading" && <div className="gx-chart-msg">Loading chart…</div>}
      {state === "empty" && <div className="gx-chart-msg">{instrument === "spot" ? "No spot ticks recorded for this range yet. Candles appear as the background worker records real prices." : "No data for this range."}</div>}
      {state === "error" && <div className="gx-chart-msg">{errText}</div>}
    </div>
  );
}
