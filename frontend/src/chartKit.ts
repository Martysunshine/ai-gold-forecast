import { ColorType, CrosshairMode, LineStyle, createChart, type IChartApi, type Time } from "lightweight-charts";
import { fmtEt, num } from "./format";
import type { Bar } from "./types";

export const C = { text: "#9AA3AF", grid: "#12161B", up: "#4FA77B", down: "#D2685F", accent: "#D6B878", line: "#E9EDF2", muted: "#6B7480", panel: "#0F1318" };
export const STEP: Record<string, number> = { "5m": 300, "30m": 1800, "1h": 3600 };
export const INTERVAL_WORDS: Record<string, string> = { "5m": "5-minute", "30m": "30-minute", "1h": "1-hour", "1d": "daily", "1wk": "weekly" };
export const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export const etDate = (s: number) => fmtEt(s, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
/** Day-candle times arrive as 'YYYY-MM-DD' strings (or {year,month,day}); returns the parts either way. */
export const dayParts = (t: unknown): { y: number; m: number; d: number } => {
  if (typeof t === "string") { const [y, m, d] = t.split("-").map(Number); return { y, m, d }; }
  const o = t as { year: number; month: number; day: number };
  return { y: o.year, m: o.month, d: o.day };
};
export const fromBiz = (t: unknown) => { const { y, m, d } = dayParts(t); return `${String(d).padStart(2, "0")} ${MONTHS[m - 1]} ${y}`; };
export const timeLabel = (t: unknown) => (typeof t === "number" ? `${etDate(t)} ET` : fromBiz(t));

export const byTime = <T extends { time: Time }>(a: T[]) => a.sort((x, y) => (x.time < y.time ? -1 : x.time > y.time ? 1 : 0));

/** A chart with the app's look: subtle grid, readable labels, exchange-time axis. */
export function makeChart(el: HTMLElement): IChartApi {
  const chart = createChart(el, {
    autoSize: true,
    layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: C.text, fontFamily: "Inter, system-ui, sans-serif", fontSize: 12, attributionLogo: true },
    grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
    rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.12 } },
    timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false, rightOffset: 4, minBarSpacing: 2 },
    crosshair: { mode: CrosshairMode.Normal, vertLine: { color: C.muted, style: LineStyle.Dashed, labelBackgroundColor: "#1E242B" }, horzLine: { color: C.muted, style: LineStyle.Dashed, labelBackgroundColor: "#1E242B" } },
    localization: { priceFormatter: (p: number) => num(p, 2), timeFormatter: timeLabel },
    handleScale: { axisPressedMouseMove: true }, handleScroll: { pressedMouseMove: true, horzTouchDrag: true },
  });
  chart.applyOptions({
    timeScale: {
      tickMarkFormatter: (t: unknown, type: number) => {
        if (typeof t !== "number") { const { y, m, d } = dayParts(t); return type === 0 ? String(y) : type === 1 ? MONTHS[m - 1] : `${d} ${MONTHS[m - 1]}`; }
        return type === 3 || type === 4 ? fmtEt(t, { hour: "2-digit", minute: "2-digit", hour12: false }) : fmtEt(t, { day: "numeric", month: "short" });
      },
    },
  });
  (window as unknown as { __gxChart?: IChartApi }).__gxChart = chart; // handle for the browser checks
  return chart;
}

/** Empty slots between the last observation and a target so the target sits at its true distance in time. */
export function futureSlots(last: number | string, target: number | string, kind: "epoch" | "day", interval: string): { time: Time }[] {
  const out: { time: Time }[] = [];
  if (kind === "epoch" && typeof last === "number" && typeof target === "number") {
    const step = STEP[interval] ?? 3600;
    for (let t = last + step, n = 0; t <= target + step && n < 700; t += step, n++) out.push({ time: t as Time });
  } else if (kind === "day" && typeof last === "string" && typeof target === "string") {
    const stepDays = interval === "1wk" ? 7 : 1;
    const end = Date.parse(target + "T00:00:00Z");
    for (let d = Date.parse(last + "T00:00:00Z") + stepDays * 86400000, n = 0; d <= end + stepDays * 86400000 && n < 400; d += stepDays * 86400000, n++) {
      const dt = new Date(d);
      if (stepDays === 1 && (dt.getUTCDay() === 0 || dt.getUTCDay() === 6)) continue;
      out.push({ time: dt.toISOString().slice(0, 10) as Time });
    }
  }
  return out;
}

/** Whitespace for periods where a recorded (spot) series has no ticks, so gaps are shown as gaps. */
export function gapSlots(bars: Bar[], step: number): { time: Time }[] {
  const out: { time: Time }[] = [];
  for (let i = 1; i < bars.length && out.length < 600; i++) {
    const a = bars[i - 1].time, b = bars[i].time;
    if (typeof a === "number" && typeof b === "number" && b - a > step * 1.5) for (let t = a + step; t < b && out.length < 600; t += step) out.push({ time: t as Time });
  }
  return out;
}
