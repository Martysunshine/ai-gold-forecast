export type Horizon = 1 | 5 | 20;
export type Scenario = "bearish" | "base" | "bullish";

export interface Instrument { name: string; kind: string; unit: string; note: string }

export interface PricePayload {
  source: string;
  source_kind: "provider" | "upload";
  symbol: string | null;
  instrument: Instrument;
  fetched_at: string;
  warnings: string[];
  provider_error: string | null;
  freshness: { latest_date: string; age_days: number; status: "fresh" | "stale"; message: string };
  latest: { date: string; close: number; previous_date: string; previous_close: number; change: number; change_pct: number };
  count: number;
  first_date: string;
  prices: { date: string; close: number }[];
}

export interface Interval { level: number; low: number; high: number; low_pct: number; high_pct: number }
export interface Forecast {
  horizon: Horizon;
  base_date: string;
  target_date: string;
  base_price: number;
  predicted_return_pct: number;
  forecast_price: number;
  expected_change_pct: number;
  interval: Interval;
  residual_std_pct: number;
}
export interface Performance {
  horizon: Horizon;
  n_predictions: number;
  n_folds: number;
  test_start: string;
  test_end: string;
  mae_model: number;
  mae_baseline: number;
  mae_improvement_pct: number;
  folds_beating_baseline: number;
  directional_accuracy: number;
  always_up_accuracy: number;
  verdict: "beats" | "worse" | "inconclusive";
  verdict_label: string;
}
export interface Driver { feature: string; label: string; value: number; z_score: number; contribution_pct: number; text: string }
export interface ForecastPayload {
  generated_at: string;
  forecasts: Forecast[];
  performance: Performance[];
  signals: Record<string, { drivers: Driver[]; drift_pct: number }>;
  context: { vol_20_percentile: number; price_vs_ma50_pct: number; momentum_20_pct: number };
  model: { type: string; alpha: number; validation: string; interval: string; baseline: string; n_observations: number; features: { name: string; label: string }[] };
  data: { source: string; source_kind: string; latest_date: string };
}

export interface NewsTheme { id: string; label: string; count: number; share_pct: number; examples: { title: string; link: string; source: string }[] }
export interface NewsItem { title: string; source: string; link: string; published: string; themes: string[] }
export interface NewsPayload {
  status: "ok" | "partial" | "stale" | "error";
  message: string | null;
  items: NewsItem[];
  themes: NewsTheme[];
  n_items: number;
  fetched_at: string | null;
  source: string;
  method?: string;
}
