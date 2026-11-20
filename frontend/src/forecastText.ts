import type { CalibrationInfo, ForecastResult, ForwardPayload, FrozenForecast, Horizon, SettlementRecord, TodayPayload } from "./types";
import { ET, etDate, fmtEt, num, signed, timeBoth } from "./format";

export type ForecastTarget = "next" | Horizon;
export type ShownPayload = TodayPayload | ForwardPayload;
/** A forecast as the user asked for it: one explicit Predict click, with the exact stored forecast it returned. Nothing here is recomputed from settings. */
export interface Shown { target: ForecastTarget; payload: ShownPayload; fetchedAt: number }

export const usd = (v: number) => `$${num(v)}`;
export const TARGETS: { id: ForecastTarget; label: string }[] = [{ id: "next", label: "Next settlement" }, { id: 1, label: "1 session" }, { id: 5, label: "5 sessions" }, { id: 20, label: "20 sessions" }];
export const targetName = (t: ForecastTarget) => (t === "next" ? "Next settlement" : `${t}-session forecast`);

const tgtMs = (f: FrozenForecast) => Date.parse(f.target_time);
/** "Wed 18 Nov · 13:30 New York · 19:30 Berlin" */
export const targetLine = (f: FrozenForecast) => `${etDate(tgtMs(f))} · ${timeBoth(tgtMs(f))}`;
export const issuedLine = (f: FrozenForecast) => `${etDate(f.issued_at * 1000)}, ${timeBoth(f.issued_at * 1000)}`;
export const baseDate = (iso?: string) => (iso ? etDate(Date.parse(iso + "T12:00:00Z"), true) : "the base session");

/** What the reference price is, in words, for each kind of forecast. */
export function referenceText(target: ForecastTarget, f: FrozenForecast, baseDateIso?: string): string {
  return target === "next"
    ? `the latest futures price when issued, ${fmtEt(f.input_cutoff, { hour: "2-digit", minute: "2-digit", hour12: false })} New York`
    : `the official settlement of ${baseDate(baseDateIso)}`;
}

/** The single concise performance note beside the result. Full evidence is in Details. */
export function perfNote(target: ForecastTarget, p: ShownPayload): string {
  if (target === "next") {
    const c = (p as TodayPayload).calibration;
    const daily = (p as TodayPayload).route === "daily_model";
    if (daily) return perfDaily((p as TodayPayload).forecast);
    return c?.historical_pct != null && c.n_dates
      ? `The range held the real settlement ${num(c.historical_pct, 0)}% of ${c.n_dates} past sessions. No model has beaten "price stays put" intraday.`
      : "No measured track record yet. No model has beaten \"price stays put\" intraday.";
  }
  const ev = (p as ForwardPayload).evaluation;
  const dev = ev?.development, fwd = ev?.forward;
  return `${dev ? `Backtest: ${signed(dev.mae_vs_baseline_pct, 1)}% error vs assuming no change.` : ""} ${fwd?.n_matured ? `Live: ${fwd.n_matured} scored.` : "Live: none scored yet."}${dev && dev.mae_vs_baseline_pct > -2 ? " No demonstrated advantage over assuming the price stays put." : ""}`.trim();
}
const perfDaily = (f?: FrozenForecast) => (f ? "Daily model: no demonstrated advantage over assuming the price stays put. Evidence is in Details → Backtest." : "");

/** "80% target range · historical coverage 70%": under-calibrated ranges say "target range", well-calibrated ones just "range". */
export function coverageLine(f: FrozenForecast, c?: CalibrationInfo): { text: string; label: string | null } {
  const lvl = Math.round(f.level * 100), hist = c?.historical_pct;
  const under = !!c?.label;
  return { text: hist != null ? `${lvl}% ${under ? "target range" : "range"} · historical coverage ${num(hist, 0)}%` : `${lvl}% range · historical coverage not yet measured`, label: c?.label ?? null };
}

export const resultLine = (r?: ForecastResult | null) =>
  r?.matured ? `Actual ${usd(r.actual!)}, off by $${num(r.abs_error!)}${r.in_range != null ? (r.in_range ? ", inside the range" : ", outside the range") : ""}.` : "The target time has passed; the official figure is not posted yet.";

export { DEFAULT_RANGE, FIT_RANGES } from "./ranges";
export { rangesFor } from "./ranges";
export const FORECAST_FOOT = "The dashed line only connects the forecast reference to the target; it is illustrative and is not a predicted path. The bar at the target is the forecast range.";

// ------------------------------------------------------------------------------------------------ the forecast card
const nyDay = (ms: number) => new Intl.DateTimeFormat("en-CA", { timeZone: ET }).format(new Date(ms));          // YYYY-MM-DD in New York
const dayDiff = (ms: number, now = Date.now()) => Math.round((Date.parse(nyDay(ms)) - Date.parse(nyDay(now))) / 86400000);
const shortDate = (ms: number) => new Intl.DateTimeFormat("en-GB", { timeZone: ET, weekday: "short", day: "numeric", month: "short" }).format(new Date(ms));
const hhmm = (epochSec: number) => fmtEt(epochSec, { hour: "2-digit", minute: "2-digit", hour12: false });

/** "Tomorrow · Thu 19 Nov" / "Today · Wed 18 Nov" / "5 sessions · Tue 24 Nov". Relative to the New York calendar. */
export function cardTitle(target: ForecastTarget, f: FrozenForecast, now = Date.now()): string {
  const ms = tgtMs(f), d = dayDiff(ms, now);
  if (target === "next") return `${d === 0 ? "Today" : d === 1 ? "Tomorrow" : "Next settlement"} · ${shortDate(ms)}`;
  return `${target === 1 ? "1 session" : `${target} sessions`} · ${shortDate(ms)}`;
}

export const REFERENCE_SENTENCE = "Reference estimate: assumes no change.";
export const SCHEMA_WORDS: Record<string, string> = { gold_v1: "gold inputs", drivers_v1: "gold + market drivers", intraday_v1: "intraday inputs", patterns_v1: "gold + chart patterns", fib_v1: "patterns & Fibonacci (experimental)" };

/** What produced the number, stated once and explicitly. A baseline is never described as a model. */
export function modelLine(f: FrozenForecast): { chip: string; text: string; baseline: boolean } {
  if (f.source.kind === "baseline") return { chip: "Reference estimate", text: REFERENCE_SENTENCE, baseline: true };
  return { chip: "Model forecast", text: `${f.source.label.replace(/ model$/, "")} · ${SCHEMA_WORDS[f.source.feature_schema] ?? f.source.feature_schema} · ${f.horizon ? `${f.horizon}-session daily model` : "intraday model"}`, baseline: false };
}

/** "Wed 18 Nov, 16:24 New York" */
export const issuedAt = (f: FrozenForecast) => `${shortDate(f.issued_at * 1000)}, ${hhmm(f.issued_at)} New York`;
/** What the inputs were: the official settlement (daily forecasts) or the latest futures quote (hourly), with the time of the data. */
export function inputLine(f: FrozenForecast, baseIso?: string | null): string {
  const t = `${shortDate(f.input_cutoff * 1000)}, ${hhmm(f.input_cutoff)} New York`;
  return f.horizon ? `official settlement of ${baseDate(baseIso ?? undefined)} (${t})` : `latest futures quote (${t})`;
}

/** Today's (or the last) finished settlement, kept apart from the forecast above. Pending until the official figure is posted. */
export function settlementLine(r: SettlementRecord, now = Date.now()): { title: string; text: string } {
  const ms = Date.parse(r.target_time), d = dayDiff(ms, now);
  const title = `${d === 0 ? "Today's settlement" : d === -1 ? "Yesterday's settlement" : "Last settlement"} · ${shortDate(ms)}`;
  const was = `Forecast $${num(r.predicted)} (${r.source.kind === "baseline" ? "reference estimate" : r.source.label}), issued ${shortDate(r.issued_at * 1000)} ${hhmm(r.issued_at)} New York.`;
  return { title, text: r.status === "scored" ? `${was} Actual close $${num(r.actual!)}, off by $${num(r.abs_error!)}${r.in_range != null ? (r.in_range ? ", inside the range" : ", outside the range") : ""}.` : `${was} Official close: pending.` };
}
