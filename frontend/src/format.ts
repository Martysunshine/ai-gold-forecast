export const usd = (v: number, digits = 2) =>
  new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v);
export const num = (v: number, digits = 2) => new Intl.NumberFormat("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v);
export const signed = (v: number, digits = 2) => `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(digits)}`;
export const pct = (v: number, digits = 2) => `${signed(v, digits)}%`;

export const parseDay = (iso: string) => new Date(iso + "T00:00:00Z").getTime();

const DAY: Intl.DateTimeFormatOptions = { day: "numeric", month: "short", year: "numeric" };
export const fmtDate = (iso: string, opts: Intl.DateTimeFormatOptions = DAY) =>
  new Date(iso.length === 10 ? iso + "T00:00:00Z" : iso).toLocaleDateString("en-GB", { timeZone: "UTC", ...opts });
export const fmtTs = (ms: number, opts: Intl.DateTimeFormatOptions = DAY) => new Date(ms).toLocaleDateString("en-GB", { timeZone: "UTC", ...opts });
export const fmtTime = (iso: string) => new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
export const fmtDateTime = (iso: string) => `${new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" })} ${fmtTime(iso)}`;

export function timeAgo(iso: string): string {
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 48) return `${hrs} h ago`;
  return `${Math.round(hrs / 24)} d ago`;
}

export const dirClass = (v: number) => (v > 0 ? "up" : v < 0 ? "down" : "flat");
export const arrow = (v: number) => (v > 0 ? "▲" : v < 0 ? "▼" : "■");

/** Evenly spaced tick positions across [lo, hi]. */
export const spacedTicks = (lo: number, hi: number, n: number) => Array.from({ length: n }, (_, i) => Math.round(lo + ((hi - lo) * i) / (n - 1)));

/** Axis domain and evenly spaced round-number ticks covering [lo, hi]. */
export function niceAxis(lo: number, hi: number, target = 5) {
  const raw = Math.max((hi - lo) / target, 1e-9);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const f = raw / mag;
  const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * mag;
  const min = Math.floor(lo / step) * step, max = Math.ceil(hi / step) * step;
  const ticks: number[] = [];
  for (let v = min; v <= max + step / 1000; v += step) ticks.push(Math.round(v * 100) / 100);
  return { domain: [min, max] as [number, number], ticks };
}

export const ET = "America/New_York";
export const fmtEt = (epochSec: number, opts: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) =>
  new Intl.DateTimeFormat("en-GB", { timeZone: ET, ...opts }).format(new Date(epochSec * 1000));
export const fmtEtDay = (epochSec: number) => fmtEt(epochSec, { weekday: "short", day: "numeric", month: "short" });
export const fmtEtShort = (epochSec: number) => fmtEt(epochSec, { hour: "2-digit", minute: "2-digit", hour12: false });
export const fmtIsoEt = (iso: string, opts: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat("en-GB", { timeZone: ET, ...opts }).format(new Date(iso));
export const fmtAgo = (sec: number | null | undefined) => (sec == null ? "—" : sec < 90 ? `${Math.round(sec)}s ago` : sec < 5400 ? `${Math.round(sec / 60)} min ago` : sec < 172800 ? `${Math.round(sec / 3600)} h ago` : `${Math.round(sec / 86400)} d ago`);

/** New York and Berlin wall-clock for one instant. Both zones are DST-correct (they differ by 6 h except for ~3 weeks a year). */
const BERLIN = "Europe/Berlin";
const hm = (ms: number, tz: string) => new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(ms));
const ymd = (ms: number, tz: string) => new Intl.DateTimeFormat("en-CA", { timeZone: tz }).format(new Date(ms));
export function zoneTimes(ms: number) {
  const dayShift = Math.round((Date.parse(ymd(ms, BERLIN)) - Date.parse(ymd(ms, ET))) / 86400000);
  return { ny: hm(ms, ET), berlin: hm(ms, BERLIN), dayShift };
}
/** "13:30 New York · 19:30 Berlin" (a day offset is added when Berlin is already on the next date). */
export const timeBoth = (ms: number) => {
  const z = zoneTimes(ms);
  return `${z.ny} New York · ${z.berlin} Berlin${z.dayShift ? ` (${z.dayShift > 0 ? "+" : "−"}${Math.abs(z.dayShift)} day)` : ""}`;
};
/** New York calendar date. The year is included when it is not the current New York year, or when `full` is set. */
export function etDate(ms: number, full = false) {
  const opts: Intl.DateTimeFormatOptions = { weekday: "short", day: "numeric", month: "short" };
  const sameYear = new Intl.DateTimeFormat("en-GB", { timeZone: ET, year: "numeric" }).format(new Date(ms)) === new Intl.DateTimeFormat("en-GB", { timeZone: ET, year: "numeric" }).format(new Date());
  return new Intl.DateTimeFormat("en-GB", { timeZone: ET, ...opts, ...(full || !sameYear ? { year: "numeric" } : {}) }).format(new Date(ms));
}
