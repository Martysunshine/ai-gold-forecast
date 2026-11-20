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
  calendar: { name: string; library: string; rule: string };
  splits: Record<"train" | "validation" | "test", Span>;
  test_series: { date: string; actual: number; predicted: number; baseline: number }[];
  features: { name: string; label: string }[];
}

export interface Quote {
  symbol: string;
  price: number;
  sell: number;
  buy: number;
  spread: number;
  spread_indicative: boolean;
  day_open: number | null;
  previous_close: number | null;
  change: number | null;
  change_pct: number | null;
  day_high: number | null;
  day_low: number | null;
  volume: number | null;
  currency: string;
  instrument: string;
  source: string;
  market_open: boolean;
  quote_time_epoch: number | null;
  delay_seconds: number | null;
  as_of_epoch: number;
  cached: boolean;
}

export type InstrumentKey = "futures" | "spot";
export type RangeKey = "1D" | "1W" | "1M" | "3M" | "1Y" | "5Y";
export type Conn = "ok" | "reconnecting" | "offline";

export interface Bar { time: number | string; open: number; high: number; low: number; close: number; volume?: number | null }
export interface OhlcPayload {
  instrument: InstrumentKey; range: RangeKey; interval: string; time_kind: "epoch" | "day"; bars: Bar[]; source: string; real_ohlc: boolean;
  history_start: number | string | null; delay_seconds: number | null; timezone: string; gap_policy: string;
}
export interface MarketStatus { state: "live" | "delayed" | "stale" | "closed" | "unavailable"; label: string }
export interface MarketSummary {
  instrument: InstrumentKey; name: string; currency: string; unit: string; price: number; as_of_epoch: number | null; delay_seconds: number | null; status: MarketStatus;
  session: { date: string; open: number | null; high: number; low: number; starts: string; convention: string; open_note?: string | null };
  previous_close: number | null; previous_close_status: "recorded" | "unavailable_from_provider"; previous_close_note: string; previous_close_date: string | null; change: number | null; change_pct: number | null; update_every_seconds: number; provider: string;
}

export interface ForwardStats {
  n_matured: number; n_sessions: number; mae?: number; baseline_mae?: number; mae_vs_baseline_pct?: number | null; coverage_pct?: number | null;
  effective_blocks?: number; needed_for_verdict?: number; mae_diff_ci90_pct?: [number, number];
}
export interface DevMetrics { by_period_coverage_pct?: number[]; coverage_ci90_pct?: [number, number]; n_origins: number; mae: number; baseline_mae: number; mae_vs_baseline_pct: number; direction_pct: number; coverage_pct: number | null; mean_width_pct: number | null; p_value_vs_baseline: number; by_period_vs_baseline_pct: number[]; data_through: string }
export interface TaskStatus {
  champion: { version: string; algorithm: string; feature_schema: string; status_changed_at: number; last_trained_at: number | null; trained_through: string | null };
  baseline_version: string; forecasts_issued: number; forward: ForwardStats; development: DevMetrics | null; calibration?: CalibrationInfo;
  last_decision: { at: number; decision: string; reason: string; champion_after: string } | null;
  challengers: { version: string; status: string; last_trained_at: number | null; forward: ForwardStats }[];
}
export interface Decision { id: number; at: number; task: string; kind: string; champion_before: string | null; champion_after: string | null; decision: string; reason: string }
export interface DriversResearch {
  ran_at: string; data_through: string; driver_errors: Record<string, string>; duration_s: number;
  horizons: Record<string, { n_origins: number; oos_start: string; oos_end: string; paired_drivers_vs_gold_only: Record<string, { delta_pct_of_baseline_mae: number; ci90: [number, number] }>;
    results: { algorithm: string; features: string; mae_vs_baseline_pct: number; mae_diff_ci90_pct: [number, number]; direction_pct: number; coverage_pct: number; by_period_vs_baseline_pct: number[] }[] }>;
  conclusion: Record<string, { verdict: string; drivers_help_significantly: string[]; beats_unchanged_price: string[] }>;
}
export interface LearningPayload {
  tasks: Record<string, TaskStatus>; rule: string[]; history: Decision[]; matured_total: number; issued_total: number; worker: WorkerStatus;
  news: { stored: number; unique_stories: number; collecting_since: number | null; last_seen: number | null; note: string };
  drivers_research: DriversResearch | null;
}
export interface DriverItem { key: string; label: string; status: "fresh" | "stale" | "missing"; value?: number; unit: string; change?: number | null; change_unit?: string; obs_date?: string; available_at?: string; retrieved_at?: string; source: string; age_days?: number; note?: string | null; error?: string | null }
export interface DriversPanel { status: string; items: DriverItem[]; note?: string; message?: string }

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

export interface ForecastSource {
  kind: "baseline" | "model"; label: string; version: string; algorithm: string; feature_schema: string; role: string; target_def: string;
}
/** A forecast exactly as it was issued. Nothing in it changes when quotes move. */
export interface FrozenForecast {
  forecast_id: string; predicted: number; low: number; high: number; level: number; reference_price: number; change: number; change_pct: number;
  little_change: boolean; little_change_threshold_pct: number; issued_at: number; input_cutoff: number; target_date: string; target_time: string;
  horizon: number | null; source: ForecastSource;
}
export interface CalibrationInfo {
  target_pct: number; historical_pct: number | null; ci90_pct?: [number, number] | null; n_dates?: number | null; by_period_pct?: number[] | null;
  historical_basis?: string; mean_width_pct?: number | null; mean_width_usd?: number; label: string | null; forward_pct?: number | null; forward_n?: number;
  typical_error_usd?: number | null;
}
export interface ForecastResult { matured: boolean; actual: number | null; abs_error: number | null; in_range: boolean | null }
export interface NextForecastInfo { state: "waiting_for_provider" | "issuing" | "scheduled"; message: string }
export interface PendingSettlement { date: string; reason: string; close_seen?: number; settlement_price?: number }
export interface ExperimentalForecast { forecast_id: string; algorithm: string; label: string; feature_schema: string; version: string; predicted: number; low: number | null; high: number | null }
export interface SettlementRecord {
  forecast_id: string; target_date: string; target_time: string; predicted: number; low: number | null; high: number | null; reference_price: number;
  issued_at: number; input_cutoff: number; source: { kind: "baseline" | "model"; label: string; version: string };
  actual: number | null; abs_error: number | null; in_range: boolean | null; status: "scored" | "pending";
}
/** `intraday`: an hourly forecast for the session that is open. `daily_model`: the next settlement is a LATER session, so the stored 1-session daily forecast is shown.
 *  `baseline_reference`: that daily forecast is not issued yet, so the stored unchanged-price baseline is shown, labelled as such. */
export type ForecastRoute = "intraday" | "daily_model" | "baseline_reference";
export interface TodayPayload {
  status: "current" | "completed" | "none"; message?: string; route?: ForecastRoute; forecast?: FrozenForecast; result?: ForecastResult | null; market_open?: boolean;
  next_update_epoch?: number | null; pending_settlement?: PendingSettlement | null; calibration?: CalibrationInfo;
  live?: { price: number; as_of_epoch: number; delay_seconds: number; label: string; vs_forecast: number; vs_forecast_pct: number };
  next_target?: { target_date: string; target_time: string }; experimental?: ExperimentalForecast[]; daily_pending?: string | null; base_date?: string | null;
  intraday?: { available: boolean; reason: string | null; unavailable_models: { algorithm: string; label: string; reason: string }[] };
  last_result?: SettlementRecord | null;
}
export interface ForwardPayload {
  status: "current" | "completed" | "none"; horizon: Horizon; message?: string; forecast?: FrozenForecast; result?: ForecastResult | null; base_date?: string;
  calibration?: CalibrationInfo; next_forecast?: NextForecastInfo; experimental?: ExperimentalForecast[];
  evaluation?: { development: DevMetrics | null; forward: ForwardStats };
}

// ------------------------------------------------------------------------------------------------ alerts
export interface AlertSettings {
  forecast_alerts: boolean; price_alerts: boolean; levels: number[]; entry_price: number | null; position_size: number | null; quote_basis: "futures" | "spot";
  position_status: "open" | "closed_unconfirmed" | "closed"; exit_price: number | null; closed_at: string | null; closure_reason: string | null;
  immediate_sms: boolean; price_sms: boolean; morning_enabled: boolean; morning_time: string; morning_tz: string; rearm: "next_session" | "after_drop";
}
export interface AlertDelivery { id: number; kind: string; mode: string; state: string; provider_sid: string | null; accepted_at: number | null; delivered_at: number | null; detail: string | null }
export interface AlertEvent {
  id: number; alert_type: "forecast_headsup" | "price_crossing"; threshold: number; target_session: string; created_at: number; expires_at: number; message: string;
  sms_state: string; payload: Record<string, unknown>; deliveries: AlertDelivery[];
}
export interface ForecastCheck { at: number; enabled: boolean; forecasts: { task: string; verdict: string; predicted?: number; target_date?: string; issued_at?: number; age_h?: number }[]; new_events: number[] }
export interface TouchGate { display: boolean; reasons: string[] }
export interface TouchSlice { n?: number; event_rate?: number; brier?: number; brier_climatology?: number; log_loss?: number; ece?: number;
  at_cut?: { cut: number; events: number; alerts: number; true_alerts: number; false_alerts: number; missed_events: number; precision: number | null; recall: number | null } }
export interface TouchPanel {
  status: "experimental" | "displayed"; reason?: string; selected_variant?: string; data_through?: string; event?: string;
  lead_time?: { min_h: number; max_h: number; note: string }; gates?: Record<string, TouchGate>;
  forward: { recorded: number; scored: number; hits: number; mean_probability_scored: number | null; latest: { origin_date: string; target_date: string; level: number; z: number; probability: number; model: string; displayed: number }[] };
  live_levels?: { level: number; z: number | null; final_slice: { events?: number; n_dates?: number; mean_p?: number | null; observed?: number | null; in_range?: boolean } }[];
  variants?: Record<string, { selection: TouchSlice; final: TouchSlice }>; improvement_vs_z_only_final?: Record<string, number>; periods?: { selection: string[]; final: string[] };
  endpoint?: { absolute: Record<string, Record<string, { sessions_below_level: number; events: number; alerts: number; true_alerts: number; false_alerts: number; missed_events: number; precision: number | null; recall: number | null }>>; lead_time: { hours: number; note: string }; oos: string[] } | null;
}
export interface AlertsPayload {
  settings: AlertSettings; levels: { level: number; note: string | null }[]; last_forecast_check: ForecastCheck | null;
  last_price_check: { at: number; verdict?: string; price?: number; as_of?: number; age_min?: number; basis: string } | null;
  last_alert: AlertEvent | null; recent: AlertEvent[];
  delivery: { mode: "simulate" | "live"; configured: boolean; missing: string[]; note: string; review_base: string };
  last_handoff: { at: number; state: string; detail: string } | null; log: { at: number; topic: string; outcome: string; detail: string | null }[];
  labels: { forecast_kind: string; entry_note: string }; touch: TouchPanel;
  position: PositionInfo;
}
export interface PositionInfo { status: "open" | "closed_unconfirmed" | "closed"; entry_price: number | null; exit_price: number | null; closed_at: string | null; closure_reason: string | null; note: string }
export interface BasisCheck {
  levels_apply_to: { instrument: string; currency: string; unit: string; forecast_target: string; price_alert_quote: string; price_alert_quote_note: string };
  currency_checked: string; quotes: Record<string, { price?: number; as_of?: number; age_min?: number; source?: string; error?: string }>;
  spot_minus_futures: number | null; entry_price: { value: number | null; verified: boolean; note: string }; warnings: string[];
}

// ---------------------------------------------------------------------------------------------- learning workspace, worker, paper
export interface WorkerJob { last: { status: string; started_at: number; finished_at: number | null; detail: string | null } | null; last_ok: number | null; age_s: number | null; failures: number; last_error: string | null; next_retry_at: number | null; at_retry_limit: boolean }
export interface NextRun { job: string; every_s: number; next_at: number; backing_off: boolean; failures: number; at_retry_limit: boolean }
export interface CycleStep { step: string; status: "ok" | "failed" | "skipped"; seconds: number; detail: unknown }
export interface CycleRun { id: number; started_at: number; finished_at: number | null; trigger: string; runtime: string | null; data_through: string | null; status: string; steps: CycleStep[] }
export interface WorkerStatus {
  daemon_running: boolean; heartbeat_age_s: number | null; runtime: string | null; host: string | null; pid: number | null; runtime_note: string; launchd_installed: boolean;
  jobs: Record<string, WorkerJob>; next_runs: NextRun[]; last_cycle: CycleRun | null; session_gap: { expected_last_session: string; data_through: string; sessions_behind: number; stale: boolean } | null;
  forecasts: number; matured: number; spot_ticks: number; cycle_overdue: boolean;
}
export interface WsDev { mae_vs_baseline_pct: number; p_value_vs_baseline: number | null; coverage_pct: number | null; mean_width_pct: number | null; n_origins: number; data_through: string }
export interface WsCandidate {
  version: string; algorithm: string; feature_schema: string; status: string; note: string | null; development: WsDev | null;
  forward: { n_matured: number; distinct_origins: number; effective_blocks: number; mae_vs_baseline_pct: number | null };
  last_attempt: { status: string; at: number; detail: string | null } | null; last_decision: { at: number; decision: string; reason: string } | null;
}
export interface WsSeries { dates: string[]; base: number[]; actual: number[]; pred: number[]; low: number[]; high: number[] }
export interface WsTask {
  task: string; label: string; horizon: number;
  active: { version: string; algorithm: string; feature_schema: string; status_changed_at: number; last_trained_at: number | null; trained_through: string | null; notes: string | null; is_unchanged_baseline: boolean };
  production_note: string; baseline_version: string;
  development: { label: string; metrics: (WsDev & Record<string, number | number[] | string | null>) | null; series: WsSeries | null;
    rolling: { window?: number; dates: string[]; model: number[]; unchanged: number[] };
    trend: { points: { data_through: string; mae_vs_baseline_pct: number; coverage_pct: number | null; mean_width_pct: number | null; n_origins: number }[]; verdict: "first_run" | "unchanged" | "improved" | "worsened"; text: string } };
  forward: { label: string; n_matured: number; distinct_origins: number; distinct_targets: number; effective_blocks: number; blocks_needed: number; mae: number | null; baseline_mae: number | null;
    mae_vs_baseline_pct: number | null; coverage_pct: number | null; mean_width_pct: number | null;
    history: { base_time: string; target_date: string; predicted: number; base_price: number; low: number | null; high: number | null; actual: number; in_range: number | null; abs_error: number; baseline_abs_error: number }[] };
  candidates: WsCandidate[]; decisions: { id: number; at: number; kind: string; decision: string; reason: string; champion_after: string | null }[];
  range: { development_coverage_pct: number | null; development_width_pct: number | null; forward_coverage_pct: number | null; forward_width_pct: number | null; target_pct: number };
}
export interface FibCompare { n_origins: number; delta_pct_of_baseline_mae: number; ci90: [number, number]; a_vs_unchanged_pct: number; b_vs_unchanged_pct: number }
export interface FeatureGroups {
  evaluated: boolean; rule: string; data_through?: string; evaluated_at?: number; verdict?: { status: "supported" | "not_supported" | "insufficient"; reason: string; rule: string };
  comparisons?: Record<string, Record<string, Record<string, FibCompare>>>; rejected?: string[]; volume_note?: string;
}
export interface QualityFinding { detected_at: number; source: string; kind: string; key: string; detail: string; status: string }
export interface WorkspacePayload {
  worker: WorkerStatus; tasks: Record<string, WsTask>; feature_groups: FeatureGroups;
  attempts: { recent: { at: number; task: string; version: string; stage: string; status: string; data_through: string | null; duration_s: number | null; detail: string | null }[]; ok: number; failed: number };
  cycles: CycleRun[]; quality: { by_status: Record<string, number>; by_kind: Record<string, number>; blocking: QualityFinding[]; recent: QualityFinding[] };
  schedule: { counts: Record<string, number>; scheduled_ok: number; next_scheduled_utc: string; verdict: string; recent: { id: number; trigger: string; runtime: string | null; started_at: number; finished_at: number | null; status: string; data_through: string | null }[] };
  ledger_audit: { conflicts: number; fallbacks: number; recent: { at: number; forecast_id: string; kind: string; detail: string }[] }; promotion_rule: string[]; totals: { issued: number; matured: number };
}
export interface PaperParams { task: string; role: string; min_net_edge_bps: number; spread_bps: number; slippage_bps: number; commission_per_oz: number; max_position_pct: number; stop_loss_pct: number; max_daily_loss_pct: number; max_drawdown_pct: number; max_trades_per_day: number; cooldown_minutes: number; quote_max_age_min: number }
export interface PaperPerf {
  initial_cash: number; cash: number; qty: number; avg_entry: number | null; mark: number | null; equity: number; return_pct: number; realised_pnl: number; unrealised_pnl: number; fees: number;
  n_trades: number; n_fills: number; win_rate_pct: number | null; max_drawdown_pct: number | null; exposure_pct: number | null;
  hold: { equity: number; return_pct: number; start_price: number } | null; cash_only: { equity: number; return_pct: number }; curve: { ts: number; book: number; hold: number; cash: number }[];
}
export interface PaperBook {
  id: number; name: string; status: "active" | "paused" | "archived"; paused_reason: string | null; created_at: number; initial_cash: number; params: PaperParams; performance: PaperPerf;
  accuracy: { task: string; role: string; n_matured: number | null; n_sessions: number | null; mae: number | null; baseline_mae: number | null; mae_vs_baseline_pct: number | null; coverage_pct: number | null };
  fills: { id: number; ts: number; side: string; qty: number; price: number; cost: number; forecast_id: string | null; rationale: string; reason: string }[];
  trades: { forecast_id: string | null; entry_ts: number; exit_ts: number; qty: number; entry: number; exit: number; pnl: number; exit_reason: string; rationale: string }[];
  events: { ts: number; kind: string; detail: string; forecast_id: string | null }[];
}
export interface PaperPayload {
  simulation_only: boolean; notice: string; defaults: PaperParams; limits: Record<string, [number, number]>; book: PaperBook | null;
  books: { id: number; name: string; status: string; created_at: number; archived_at: number | null; initial_cash: number; return_pct: number; n_trades: number }[];
}
