export type Horizon = 1 | 5 | 20;
export type MethodKey = "ridge" | "random_forest" | "unchanged";

export interface Instrument { name: string; kind: string; unit: string; note: string }

export interface PricePayload {
  data_key: string;
  source: string;
  source_kind: "provider" | "upload";
  symbol: string | null;
  instrument: Instrument;
  fetched_at: string;
  age_minutes: number;
  warnings: string[];
  provider_error: string | null;
  status: { mode: "live" | "cached" | "upload"; label: string };
  session: { latest_is_completed_session: boolean; in_progress: { price: number; time: string; session_date: string } | null; note: string };
  freshness: { latest_date: string; age_days: number; status: "fresh" | "stale"; message: string };
  latest: { date: string; close: number; previous_date: string; previous_close: number; change: number; change_pct: number };
  stats: { realised_vol_20d_annualised_pct: number };
  count: number;
  first_date: string;
  prices: { date: string; close: number }[];
}

export interface Span { start: string; end: string; n: number }
export interface MethodResult { label: string; mae: number; directional_accuracy: number | null; vs_baseline_pct?: number }

export interface Prediction {
  data_key: string;
  served_from_cache: boolean;
  generated_at: string;
  horizon: Horizon;
  base_date: string;
  target_date: string;
  base_price: number;
  forecast: {
    model: MethodKey;
    model_label: string;
    price: number;
    change_pct: number;
    interval: {
      level: number; low: number; high: number; low_pct: number; high_pct: number;
      observed_coverage_pct: number; mean_width_usd_on_test: number; z_low: number; z_high: number;
      scale: string; calibrated_on: Span; evaluated_on: Span; built_from: string;
    };
  };
  selection: { default: MethodKey; rule: string; validation_mae: Record<MethodKey, number> };
  test: {
    period: { start: string; end: string };
    n: number;
    effective_n: number;
    methods: Record<MethodKey, MethodResult>;
    always_up_pct: number;
    within_tolerance: Record<string, Record<MethodKey | "default", number>>;
    advantage: boolean;
    verdict: string;
  };
  training: { closes: number; first_close: string; last_close: string; usable_samples: number; n_features: number };
  calendar: { name: string; library: string; rule: string; check: { observed_bars: number; rule_sessions: number; rule_sessions_without_bar: number; bars_not_in_rule: number } };
  splits: Record<"train" | "validation" | "test", Span>;
  test_series: { date: string; actual: number; predicted: number; baseline: number }[];
  features: { name: string; label: string }[];
}

export interface NewsTheme { id: string; label: string; background: string; count: number; share_pct: number; examples: { title: string; link: string; source: string; published: string }[] }
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

export interface GldComparison {
  configured: boolean;
  note: string;
  setup?: string[];
  instrument?: Instrument;
  source?: string;
  fetched_at?: string;
  age_minutes?: number;
  provider_error?: string | null;
  status?: { mode: "live" | "cached"; label: string };
  latest?: { date: string; close: number; change_pct: number };
  count?: number;
  gold_source?: string;
  comparison?: {
    common_dates: number;
    start: string;
    end: string;
    indexed: { date: string; gc: number; gld: number }[];
    window: { sessions: number; start: string; end: string; return_correlation: number; gc_return_pct: number; gld_return_pct: number };
  };
}
