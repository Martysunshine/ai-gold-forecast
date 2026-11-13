export const usd = (v: number, digits = 2) =>
  new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v);

export const signed = (v: number, digits = 2) => `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(digits)}`;
export const pct = (v: number, digits = 2) => `${signed(v, digits)}%`;

export const parseDay = (iso: string) => new Date(iso + "T00:00:00Z").getTime();

export const fmtDate = (iso: string, opts: Intl.DateTimeFormatOptions = { day: "numeric", month: "short", year: "numeric" }) =>
  new Date(iso.length === 10 ? iso + "T00:00:00Z" : iso).toLocaleDateString("en-GB", { timeZone: "UTC", ...opts });

export const fmtTs = (ms: number, opts: Intl.DateTimeFormatOptions) => new Date(ms).toLocaleDateString("en-GB", { timeZone: "UTC", ...opts });

export function timeAgo(iso: string): string {
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (mins < 60) return `${Math.max(mins, 1)} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 48) return `${hrs} h ago`;
  return `${Math.round(hrs / 24)} d ago`;
}

export const dirClass = (v: number) => (v > 0 ? "up" : v < 0 ? "down" : "flat");
export const arrow = (v: number) => (v > 0 ? "▲" : v < 0 ? "▼" : "■");
