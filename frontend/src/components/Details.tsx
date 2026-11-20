import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { usePoll } from "../hooks";
import type { DriversPanel, ForwardPayload, FrozenForecast, Horizon, LearningPayload, NewsPayload, Prediction, PricePayload, TaskStatus, TodayPayload } from "../types";
import { etDate, fmtAgo, fmtEt, num, signed, timeBoth } from "../format";
import { baseDate, type Shown } from "../forecastText";
import { Backtest } from "./Backtest";
import { GldComparison } from "./GldComparison";
import { MarketContext } from "./MarketContext";
import { Methodology } from "./Methodology";
import { ErrorBlock, Info, Skeleton } from "./Common";

export type DetailsTab = "model" | "drivers" | "backtest" | "news" | "method" | "gld";
export const DETAIL_TABS: { id: DetailsTab; label: string }[] = [
  { id: "backtest", label: "Backtest" }, { id: "drivers", label: "Market drivers" }, { id: "gld", label: "GLD comparison" },
  { id: "method", label: "Methodology" }, { id: "model", label: "Model & evaluation" }, { id: "news", label: "News" },
];
const TASK_NAME: Record<string, string> = { nowcast: "Today's settlement", fwd_1: "1-session forecast", fwd_5: "5-session forecast", fwd_20: "20-session forecast" };
const when = (t: number | null | undefined) => (t ? `${fmtEt(t, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false })} ET` : "never");

// ------------------------------------------------------------------------------------------------ Model
function TaskRow({ id, t }: { id: string; t: TaskStatus }) {
  const f = t.forward, d = t.development;
  return (
    <div className="gx-task" data-testid={`task-${id}`}>
      <div className="gx-task-head"><b>{TASK_NAME[id]}</b><span className="gx-chip">{t.champion.algorithm === "unchanged" ? "Price stays put" : `${t.champion.algorithm.replace("_", " ")} · ${t.champion.feature_schema}`}</span></div>
      <dl className="gx-kv">
        <div><dt>Last trained</dt><dd>{when(t.champion.last_trained_at)}{t.champion.trained_through ? ` · data through ${t.champion.trained_through}` : ""}</dd></div>
        <div><dt>Forecasts logged / scored</dt><dd className="mono">{t.forecasts_issued} / {f.n_matured}</dd></div>
        <div><dt>Live record vs "no change"</dt><dd className="mono">{f.n_matured ? `${signed(f.mae_vs_baseline_pct ?? 0, 1)}% error · ${f.effective_blocks} of ${f.needed_for_verdict} blocks needed for a verdict` : `none scored yet · need ${f.needed_for_verdict} blocks for a verdict`}</dd></div>
        {d && <div><dt>Walk-forward (development)</dt><dd className="mono">{signed(d.mae_vs_baseline_pct, 1)}% error vs no change · direction {num(d.direction_pct, 0)}% · p={num(d.p_value_vs_baseline, 2)}{d.coverage_pct != null ? ` · range held ${num(d.coverage_pct, 0)}%` : ""}</dd></div>}
        {t.last_decision && <div><dt>Last decision</dt><dd>{t.last_decision.decision} — {t.last_decision.reason}</dd></div>}
      </dl>
    </div>
  );
}

/** The exact forecasts shown on the cards, with the technical detail the cards leave out: model, version, target definition, inputs. */
function ShownForecast({ title, f, referenceNote, cal }: { title: string; f: FrozenForecast | undefined; referenceNote: string; cal?: { historical_pct: number | null; ci90_pct?: [number, number] | null; n_dates?: number | null; historical_basis?: string; by_period_pct?: number[] | null; forward_n?: number; forward_pct?: number | null } }) {
  if (!f) return <div className="gx-task"><div className="gx-task-head"><b>{title}</b></div><p className="gx-muted">No forecast issued yet.</p></div>;
  const t = Date.parse(f.target_time), ms = (sec: number) => `${etDate(sec * 1000, true)}, ${timeBoth(sec * 1000)}`;
  return (
    <div className="gx-task" data-testid={`shown-${title.startsWith("Next") ? "next" : "fwd"}`}>
      <div className="gx-task-head"><b>{title}</b><span className="gx-chip">{f.source.kind === "baseline" ? "Unchanged-price baseline" : "Model forecast"}</span></div>
      <dl className="gx-kv">
        <div><dt>Source</dt><dd>{f.source.kind === "baseline" ? "No model is in control: the prediction equals the futures price when issued (no adjustment, rounded to the cent)." : `${f.source.algorithm.replace("_", " ")} (${f.source.role}) on ${f.source.feature_schema} features`}</dd></div>
        <div><dt>Version</dt><dd className="mono">{f.source.version}</dd></div>
        <div><dt>Forecast id</dt><dd className="mono">{f.forecast_id}</dd></div>
        <div><dt>Target</dt><dd>{f.source.target_def}</dd></div>
        <div><dt>Target time</dt><dd>{ms(t / 1000)}</dd></div>
        <div><dt>Reference</dt><dd><span className="mono">${num(f.reference_price)}</span> · {referenceNote}</dd></div>
        <div><dt>Change from reference</dt><dd className="mono">{signed(f.change)} / {signed(f.change_pct)}%{f.little_change ? ` (below the ${num(f.little_change_threshold_pct, 2)}% "little change" display threshold)` : ""}</dd></div>
        <div><dt>Inputs cut off</dt><dd>{ms(f.input_cutoff)}</dd></div>
        <div><dt>Issued</dt><dd>{ms(f.issued_at)}</dd></div>
        <div><dt>{Math.round(f.level * 100)}% range</dt><dd className="mono">${num(f.low)}–${num(f.high)}</dd></div>
        {cal && <div><dt>Range calibration</dt><dd>{cal.historical_pct != null ? `${num(cal.historical_pct, 0)}% historical coverage` : "not yet measured"}{cal.ci90_pct ? ` (90% interval ${num(cal.ci90_pct[0], 0)}–${num(cal.ci90_pct[1], 0)}%)` : ""}{cal.n_dates ? ` over ${cal.n_dates} ${cal.historical_basis?.includes("session") ? "sessions" : "dates"}` : ""}{cal.historical_basis ? `, ${cal.historical_basis}` : ""}.{cal.by_period_pct ? ` By period (oldest → newest): ${cal.by_period_pct.map((x) => `${num(x, 0)}%`).join(" · ")}.` : ""} {cal.forward_n ? `Live matured: ${cal.forward_n}${cal.forward_pct != null ? `, ${num(cal.forward_pct, 0)}% inside` : ""}.` : "No live outcomes have matured yet."}</dd></div>}
      </dl>
    </div>
  );
}

const SCHEMA_WORDS: Record<string, string> = { gold_v1: "gold inputs", drivers_v1: "+ market drivers", intraday_v1: "intraday inputs", patterns_v1: "+ chart patterns", fib_v1: "+ patterns & Fibonacci (experimental)" };

/** Experimental (shadow) estimates for the same target, kept out of the forecast card. Models that cannot predict the target are listed as unavailable, never as a number. */
function Experimental({ payload }: { payload: TodayPayload | ForwardPayload }) {
  const exp = payload.experimental ?? [];
  const intra = (payload as TodayPayload).intraday;
  const unavailable = (payload as TodayPayload).route && (payload as TodayPayload).route !== "intraday" ? intra?.unavailable_models ?? [] : [];
  return (
    <div data-testid="experimental" className="gx-expblock">
      <h4>Experimental model estimates for this target</h4>
      {exp.length > 0 && <>
        <p className="gx-micro">Shadow forecasts: recorded before the outcome, not in control of the forecast shown. None has shown an advantage over the unchanged price.</p>
        <table className="gx-table"><thead><tr><th>Model</th><th>Inputs</th><th>Estimate</th><th>Range</th></tr></thead>
          <tbody>{exp.map((e) => <tr key={e.forecast_id} data-testid="exp-row"><td>{e.label.replace(" model", "")}</td><td>{SCHEMA_WORDS[e.feature_schema] ?? e.feature_schema}</td><td className="mono">${num(e.predicted)}</td><td className="mono">{e.low != null && e.high != null ? `$${num(e.low)}–$${num(e.high)}` : "—"}</td></tr>)}</tbody></table>
      </>}
      {unavailable.length > 0 && (
        <p className="gx-micro" data-testid="exp-unavailable"><b>Unavailable for this target:</b> {unavailable.map((u) => u.label.replace(" model", "")).join(", ")}. {intra?.reason} Nothing is shown in their place, and no estimate is forced.</p>
      )}
      {exp.length === 0 && unavailable.length === 0 && <p className="gx-micro">No experimental estimates were recorded for this target.</p>}
    </div>
  );
}

function ModelTab({ shown }: { shown: Shown | null }) {
  const { data, error, refresh } = usePoll<LearningPayload>(api.learning, 30000, "learning");
  if (!data) return error ? <ErrorBlock title="Could not load model details" message={error} onRetry={refresh} /> : <Skeleton h={240} />;
  const w = data.worker;
  return (
    <div className="gx-stack" data-testid="model-tab">
      <section>
        <h3>Background worker</h3>
        <dl className="gx-kv">
          <div><dt>Status</dt><dd>{w.daemon_running ? "Running (heartbeat " + fmtAgo(w.heartbeat_age_s) + ")" : "Not running — start it with ./scripts/launchd/install.sh or python -m app.worker run"}</dd></div>
          <div><dt>Last data update</dt><dd>{when(w.jobs.collect?.last_ok)} <span className="gx-muted">({fmtAgo(w.jobs.collect?.age_s)})</span></dd></div>
          <div><dt>Matured forecasts</dt><dd className="mono">{data.matured_total} scored of {data.issued_total} logged</dd></div>
          <div><dt>Spot ticks recorded</dt><dd className="mono">{w.spot_ticks.toLocaleString()}</dd></div>
        </dl>
      </section>
      <section data-testid="shown-forecasts">
        <h3>Forecast on the chart</h3>
        {!shown ? <p className="gx-muted">No forecast is displayed. Press Predict on the Forecast page.</p> : shown.target === "next"
          ? <ShownForecast title="Next settlement" f={(shown.payload as TodayPayload).forecast} cal={(shown.payload as TodayPayload).calibration}
              referenceNote={(shown.payload as TodayPayload).route === "daily_model" ? `official settlement of ${baseDate((shown.payload as TodayPayload).base_date ?? undefined)}` : "latest futures price at issue"} />
          : <ShownForecast title={`${shown.target}-session forecast`} f={(shown.payload as ForwardPayload).forecast} cal={(shown.payload as ForwardPayload).calibration} referenceNote={`official settlement of ${baseDate((shown.payload as ForwardPayload).base_date)}`} />}
        {shown && <Experimental payload={shown.payload} />}
      </section>
      <section>
        <h3>Current model per forecast</h3>
        {Object.entries(data.tasks).map(([id, t]) => <TaskRow key={id} id={id} t={t} />)}
      </section>
      <section data-testid="calibration-audit">
        <h3>Range calibration audit (claimed 80%)</h3>
        <table className="gx-table"><thead><tr><th>Forecast</th><th>Historical coverage</th><th>Oldest → newest period</th><th>Avg width</th><th>Verdict</th></tr></thead>
          <tbody>{Object.entries(data.tasks).filter(([id]) => id !== "nowcast").map(([id, t]) => {
            const c = t.calibration;
            return (
              <tr key={id} data-testid={`cal-${id}`}>
                <td>{TASK_NAME[id]}</td>
                <td className="mono">{c?.historical_pct != null ? `${num(c.historical_pct, 0)}%` : "—"}{c?.ci90_pct ? <span className="gx-muted"> ({num(c.ci90_pct[0], 0)}–{num(c.ci90_pct[1], 0)})</span> : null}</td>
                <td className="mono">{c?.by_period_pct ? c.by_period_pct.map((x) => `${num(x, 0)}%`).join(" · ") : "—"}</td>
                <td className="mono">{c?.mean_width_pct != null ? `${num(c.mean_width_pct, 1)}%` : "—"}</td>
                <td>{c?.label ? <span className="gx-warn">{c.label}</span> : c?.historical_pct != null ? "Within tolerance" : "—"}</td>
              </tr>
            );
          })}</tbody></table>
        <p className="gx-micro">Chronological walk-forward: every range is built only from information available at its forecast date, over the same forecast dates for each horizon. Coverage and width are reported together, with a 90% block-bootstrap interval (overlapping targets are not independent). “Under-calibrated” means more than 8 points below the claimed 80%. Ranges are not tuned or widened after the fact. Live matured outcomes are added as they arrive; none have matured yet in a fresh ledger.</p>
      </section>
      <section>
        <h3>How a model gets promoted</h3>
        <ol className="gx-rule">{data.rule.map((r, i) => <li key={i}>{r}</li>)}</ol>
        <p className="gx-micro">Calibration, model history and rollback: every decision below is stored; a champion can be restored with <code>python -m app.worker rollback &lt;task&gt;</code>.</p>
      </section>
      <section>
        <h3>Decision & model history</h3>
        {data.history.length === 0 ? <p className="gx-muted">No decisions yet.</p> : (
          <ul className="gx-hist">{data.history.slice(0, 14).map((h) => (
            <li key={h.id}><span className="mono">{when(h.at)}</span> <b>{TASK_NAME[h.task] ?? h.task}</b> · {h.decision}{h.champion_after ? ` → ${h.champion_after.split(":").slice(1, 2).join("")}` : ""}<br /><span className="gx-muted">{h.reason}</span></li>
          ))}</ul>
        )}
      </section>
      <section>
        <h3>Collected for the future: news archive</h3>
        <p className="gx-micro">{data.news.stored.toLocaleString()} headlines stored ({data.news.unique_stories.toLocaleString()} unique stories) with their original publication time and the moment this app first saw them{data.news.collecting_since ? `, collecting since ${when(data.news.collecting_since)}` : ""}. {data.news.note}</p>
      </section>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ Drivers
function DriversTab() {
  const panel = usePoll<DriversPanel>(api.drivers, 120000, "drivers");
  const learn = usePoll<LearningPayload>(api.learning, 60000, "learning");
  const r = learn.data?.drivers_research;
  return (
    <div className="gx-stack" data-testid="drivers-tab">
      <section>
        <h3>Inputs, as of now</h3>
        {!panel.data ? <Skeleton h={160} /> : panel.data.status !== "ok" ? <p className="gx-muted">{panel.data.message}</p> : (
          <table className="gx-table"><thead><tr><th>Input</th><th>Latest</th><th>Change</th><th>Observed</th><th>Source · retrieved</th></tr></thead>
            <tbody>{panel.data.items.map((i) => (
              <tr key={i.key} data-testid={`driver-${i.key}`}>
                <td>{i.label}</td>
                <td className="mono">{i.value != null ? `${num(i.value, 2)}` : "—"} <span className="gx-muted">{i.unit}</span></td>
                <td className={`mono ${i.change == null ? "" : i.change > 0 ? "up" : i.change < 0 ? "down" : ""}`}>{i.change == null ? "—" : `${signed(i.change, 2)} ${i.change_unit}`}</td>
                <td>{i.obs_date ?? "—"} <span className={`gx-tag ${i.status}`}>{i.status}</span>{i.note ? <Info label="Note" text={i.note} /> : null}</td>
                <td className="gx-muted">{i.source}{i.retrieved_at ? ` · ${i.retrieved_at.slice(11, 16)}Z` : ""}</td>
              </tr>
            ))}</tbody></table>
        )}
        <p className="gx-micro">Each value is only used for a forecast made after it was published (conservatively 18:00 ET the same day for market closes; the next day for FRED yields). Nothing here is shown as a cause of a price move.</p>
      </section>
      <section>
        <h3>Do these inputs improve the forecast? (measured)</h3>
        {!r ? <p className="gx-muted">The comparison has not been run yet.</p> : (
          <>
            <p className="gx-micro">Walk-forward test on identical forecast dates, refit every 21 sessions, overlapping targets purged, uncertainty from a moving-block bootstrap. Data through {r.data_through}. A gain only counts if its 90% interval excludes zero.</p>
            {Object.entries(r.horizons).map(([h, res]) => {
              const c = r.conclusion[h];
              return (
                <div className="gx-study" key={h} data-testid={`study-${h}`}>
                  <b>{h}-session</b> <span className="gx-muted">· {res.n_origins} forecast dates, {res.oos_start} → {res.oos_end}</span>
                  <p className="gx-micro">{c.verdict}</p>
                  <table className="gx-table"><thead><tr><th>Model</th><th>Gold only vs no-change</th><th>+ drivers vs no-change</th><th>Drivers effect (paired, 90% CI)</th></tr></thead>
                    <tbody>{["ridge", "hgb", "random_forest"].map((a) => {
                      const g = res.results.find((x) => x.algorithm === a && x.features === "gold_only")!, d = res.results.find((x) => x.algorithm === a && x.features === "gold+all_drivers")!, p = res.paired_drivers_vs_gold_only[a];
                      return <tr key={a}><td>{a.replace("_", " ")}</td><td className="mono">{signed(g.mae_vs_baseline_pct, 1)}%</td><td className="mono">{signed(d.mae_vs_baseline_pct, 1)}%</td>
                        <td className="mono">{signed(p.delta_pct_of_baseline_mae, 1)} pts [{signed(p.ci90[0], 1)}, {signed(p.ci90[1], 1)}]{p.ci90[1] < 0 ? " ✓" : ""}</td></tr>;
                    })}</tbody></table>
                </div>
              );
            })}
            <p className="gx-micro">Negative = smaller error than assuming the price stays put. Not tested: economic-release surprises (no timestamped consensus data is available without a paid licence) and futures volume (continuous-contract volume is unreliable).</p>
          </>
        )}
      </section>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ Backtest / Method / News / Data
function useForecast(h: Horizon) {
  const [res, setRes] = useState<Prediction | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let dead = false;
    setRes(null); setErr(null);
    api.predict(h).then((p) => !dead && setRes(p)).catch((e) => !dead && setErr(e instanceof Error ? e.message : "Failed"));
    return () => { dead = true; };
  }, [h]);
  return { res, err };
}

function HorizonSeg({ h, set }: { h: Horizon; set: (h: Horizon) => void }) {
  return <div className="gx-seg sm">{([1, 5, 20] as const).map((x) => <button key={x} className={h === x ? "on" : ""} onClick={() => set(x)}>{x} {x === 1 ? "session" : "sessions"}</button>)}</div>;
}

function BacktestTab() {
  const [h, setH] = useState<Horizon>(5);
  const { res, err } = useForecast(h);
  return <div className="gx-stack"><HorizonSeg h={h} set={setH} />{err ? <ErrorBlock title="Backtest unavailable" message={err} /> : !res ? <Skeleton h={220} /> : <Backtest result={res} horizon={h} />}</div>;
}

function MethodTab() {
  const [prices, setPrices] = useState<PricePayload | null>(null);
  const { res } = useForecast(5);
  useEffect(() => { api.prices().then(setPrices).catch(() => undefined); }, []);
  return prices ? <Methodology prices={prices} result={res} /> : <Skeleton h={200} />;
}

function NewsTab() {
  const [news, setNews] = useState<NewsPayload | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const load = useCallback(async (refresh = false) => { setLoading(true); setErr(null); try { setNews(await api.news(refresh)); } catch (e) { setErr(e instanceof Error ? e.message : "Failed"); } finally { setLoading(false); } }, []);
  useEffect(() => { void load(); }, [load]);
  return <MarketContext news={news} loading={loading} error={err} onRetry={() => load(true)} />;
}

export function DataSettings({ onChanged }: { onChanged: () => void }) {
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const file = useRef<HTMLInputElement>(null);
  const act = async (fn: () => Promise<PricePayload>, label: string) => {
    setBusy(true); setMsg(null);
    try { const p = await fn(); setMsg({ kind: p.provider_error ? "err" : "ok", text: p.provider_error ? `${label}: ${p.provider_error} Showing the last valid data.` : `${label} complete. Latest completed close ${p.latest.date}: ${num(p.latest.close)}.` }); onChanged(); }
    catch (e) { setMsg({ kind: "err", text: `${label} failed: ${e instanceof ApiError || e instanceof Error ? e.message : "error"}` }); }
    finally { setBusy(false); if (file.current) file.current.value = ""; }
  };
  return (
    <div className="gx-stack" data-testid="data-tab">
      <section>
        <h3>Data sources and delays</h3>
        <table className="gx-table"><thead><tr><th>What</th><th>Source</th><th>Delay / cadence</th></tr></thead><tbody>
          <tr><td>Gold futures price, OHLC candles</td><td>Yahoo Finance · COMEX GC=F</td><td>≈10 min delayed · chart refreshes every 15 s</td></tr>
          <tr><td>Spot gold price</td><td>gold-api.com · XAU/USD</td><td>seconds · recorded every 10 s by the worker</td></tr>
          <tr><td>Spot candles</td><td>Ticks recorded by this app</td><td>start when recording started (no keyless spot history exists)</td></tr>
          <tr><td>Daily closes the models learn from</td><td>Yahoo Finance · GC=F settlements</td><td>after each completed session</td></tr>
          <tr><td>Dollar, yields, equities, VIX</td><td>Yahoo Finance daily</td><td>same evening</td></tr>
          <tr><td>Real yield</td><td>FRED · DFII10</td><td>published the next business day</td></tr>
          <tr><td>Headlines</td><td>Google News RSS</td><td>archived with first-seen time</td></tr>
        </tbody></table>
        <p className="gx-micro">Free feeds are never labelled real-time unless they are. Prices here are for information, not trading.</p>
      </section>
      <section>
        <h3>Daily price data</h3>
        <div className="gx-row">
          <button className="btn" disabled={busy} onClick={() => act(api.refresh, "Refresh")}>Refresh now</button>
          <button className="btn" disabled={busy} onClick={() => file.current?.click()}>Upload CSV…</button>
          <button className="btn" disabled={busy} onClick={() => act(api.useProvider, "Switch to live data")}>Use live data</button>
          <input ref={file} type="file" accept=".csv,text/csv" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) void act(() => api.upload(f), `Upload ${f.name}`); }} />
        </div>
        {msg && <p className={`gx-micro ${msg.kind === "err" ? "down" : ""}`} role="status">{msg.text}</p>}
        <p className="gx-micro">An uploaded CSV replaces the daily series the models learn from until you switch back to live data.</p>
      </section>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ Details panel (tabs under the chart)
export function DetailsPanel({ shown, tab, onTab }: { shown: Shown | null; tab: DetailsTab; onTab: (t: DetailsTab) => void }) {
  return (
    <section className="gx-details" id="details" aria-label="Details" data-testid="details-panel">
      <div className="gx-tabs" role="tablist">
        {DETAIL_TABS.map((t) => <button key={t.id} role="tab" aria-selected={tab === t.id} className={tab === t.id ? "on" : ""} onClick={() => onTab(t.id)} data-testid={`dtab-${t.id}`}>{t.label}</button>)}
      </div>
      <div className="gx-details-body">
        {tab === "model" && <ModelTab shown={shown} />}
        {tab === "drivers" && <DriversTab />}
        {tab === "backtest" && <BacktestTab />}
        {tab === "news" && <NewsTab />}
        {tab === "method" && <MethodTab />}
        {tab === "gld" && <GldComparison />}
      </div>
    </section>
  );
}
