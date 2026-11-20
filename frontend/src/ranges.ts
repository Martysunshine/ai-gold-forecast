// Which history ranges can show a forecast's target without hundreds of empty slots. Dependency-free so it can be unit-tested with plain node.
export type Target = "next" | 1 | 5 | 20;
export const FIT_RANGES: Record<string, string[]> = { next: ["1D", "1W"], "1": ["1W", "1M", "3M"], "5": ["3M", "1Y"], "20": ["3M", "1Y"] };
export const DEFAULT_RANGE: Record<string, string> = { next: "1D", "1": "1M", "5": "3M", "20": "3M" };

/** A next-settlement target that is tomorrow (after today's 13:30 New York) is ~22 h away: on 5-minute bars that is ~260 empty slots, so it needs 30-minute bars. */
export function rangesFor(target: Target, f: { target_time: string }, now = Date.now()): { allowed: string[]; dflt: string } {
  if (target === "next") {
    const hours = (Date.parse(f.target_time) - now) / 3600000;
    return hours <= 6 ? { allowed: ["1D", "1W"], dflt: "1D" } : { allowed: ["1W"], dflt: "1W" };
  }
  return { allowed: FIT_RANGES[String(target)] ?? [], dflt: DEFAULT_RANGE[String(target)] ?? "1M" };
}
