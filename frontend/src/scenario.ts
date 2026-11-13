import type { Forecast, Scenario } from "./types";

/** Scenario shifts are an ASSUMPTION, not a learned quantity: ± half of the out-of-sample residual std. */
export const SCENARIO_SIGMA = 0.5;

export function scenarioPrice(f: Forecast, s: Scenario): number {
  const shiftPct = s === "base" ? 0 : (s === "bullish" ? 1 : -1) * SCENARIO_SIGMA * f.residual_std_pct;
  return f.forecast_price * (1 + shiftPct / 100);
}
