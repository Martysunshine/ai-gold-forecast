import { useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { usePoll } from "../hooks";
import type { WorkspacePayload, WsCandidate, WsTask } from "../types";
import { etDate, fmtAgo, fmtEt, num, signed } from "../format";
import { C } from "../chartKit";
import { ErrorBlock, Info, Skeleton } from "./Common";

const when = (t: number | null | undefined) => (t ? `${etDate(t * 1000)}, ${fmtEt(t, { hour: "2-digit", minute: "2-digit", hour12: false })} New York` : "never");
const inTime = (t: number) => { const s = t - Date.now() / 1000; return s < 90 ? "in under 2 min" : s < 5400 ? `in ${Math.round(s / 60)} min` : s < 172800 ? `in ${Math.round(s / 3600)} h` : `in ${Math.round(s / 86400)} d`; };
const pctS = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${signed(v, d)}%`);
const cov = (v: number | null | undefined) => (v == null ? "—" : `${num(v, 0)}%`);
const wid = (v: number | null | undefined) => (v == null ? "—" : `${num(v, 1)}%`);
const short = (v: string) => v.split(":").slice(1).join(" · ").replace("_v1", "").replace("_", " ");
const JOB_LABEL: Record<string, string> = { spot: "Spot price recording", nowcast: "Next-settlement forecasts", score: "Score matured forecasts", learn: "Learning check", alerts: "Alerts + paper book", "daily cycle": "Full daily cycle (23:30 UTC)" };
const STEP_LABEL: Record<string, string> = { collect: "Collect", validate: "Validate", score: "Score", learn: "Features → train → evaluate → decide → publish", nowcast: "Next settlement", threshold: "Touch research", alerts: "Alerts", paper: "Paper book" };
const TASK_ORDER = ["fwd_1", "fwd_5", "fwd_20", "nowcast"] as const;
const TREND_WORD = { first_run: "First evaluation", unchanged: "No meaningful change", improved: "Improved", worsened: "Worsened" } as const;

function Runtime({ d }: { d: WorkspacePayload }) {
  const w = d.worker;
  const gap = w.session_gap;
  const ok = (j: string) => (w.jobs[j]?.last_ok ? `${when(w.jobs[j].last_ok)} (${fmtAgo(w.jobs[j].age_s)})` : "never");
  return (
    <section className="gx-card" data-testid="lr-runtime">
      <div className="gx-card-head"><h2>BACKGROUND WORKER</h2>
        <span className={`gx-chip ${w.daemon_running ? "" : "warnchip"}`} data-testid="lr-runtime-state">{w.daemon_running ? `Running · ${w.runtime ?? "unknown runtime"}` : "Not running"}</span>
      </div>
      <p className="gx-micro" data-testid="lr-runtime-note">{w.runtime_note}</p>
      <dl className="gx-kv gx-al-kv">
        <div><dt>Heartbeat</dt><dd>{w.heartbeat_age_s != null ? `${w.heartbeat_age_s}s ago${w.pid ? ` · pid ${w.pid}` : ""}${w.host ? ` · ${w.host}` : ""}` : "none"}</dd></div>
        <div><dt>Data through</dt><dd>{gap ? <>{gap.data_through}; the latest completed session per the exchange calendar is {gap.expected_last_session}{gap.sessions_behind ? ` (${gap.sessions_behind} behind${gap.stale ? " — stale" : ""})` : " (up to date)"}</> : "—"}</dd></div>
        <div><dt>Last collection</dt><dd>{ok("collect")}</dd></div>
        <div><dt>Last validation</dt><dd>{ok("validate")}</dd></div>
        <div><dt>Last scoring</dt><dd>{ok("score")}</dd></div>
        <div><dt>Last training / evaluation</dt><dd>{ok("learn")}</dd></div>
        <div><dt>Forecasts logged / scored</dt><dd className="mono">{d.totals.issued} / {d.totals.matured}</dd></div>
        <div data-testid="lr-schedule"><dt>Scheduled daily cycle (23:30 UTC)</dt><dd><b>{d.schedule.verdict}</b>{Object.keys(d.schedule.counts).length ? ` Recorded cycles by trigger: ${Object.entries(d.schedule.counts).map(([k, v]) => `${k} ${v}`).join(", ")}.` : " No full cycle is recorded at all."}</dd></div>
      </dl>
      <p className="gx-micro" data-testid="lr-proof-note">A moving quote does not show that training ran. These rows come from the worker's persistent job and cycle records: the heartbeat proves the process is alive, the last training row proves a training run finished, and only a recorded <code>scheduled</code> cycle proves the daily schedule fired.</p>
      <h3>Next scheduled jobs</h3>
      <table className="gx-table" data-testid="lr-next-jobs"><thead><tr><th>Job</th><th>Next run</th><th>State</th></tr></thead>
        <tbody>{w.next_runs.map((r) => (
          <tr key={r.job}><td>{JOB_LABEL[r.job] ?? r.job}</td><td>{inTime(r.next_at)}</td>
            <td>{r.at_retry_limit ? <span className="gx-warn">retry limit reached · retrying hourly</span> : r.backing_off ? <span className="gx-warn">backing off after {r.failures} failure{r.failures > 1 ? "s" : ""}</span> : "on schedule"}</td></tr>
        ))}</tbody></table>
      {d.cycles[0] && (
        <>
          <h3>Last full cycle</h3>
          <p className="gx-micro" data-testid="lr-cycle">#{d.cycles[0].id} · {d.cycles[0].trigger} · {when(d.cycles[0].started_at)} · {d.cycles[0].status}{d.cycles[0].runtime ? ` · ${d.cycles[0].runtime}` : ""}</p>
          <ol className="gx-steps">{d.cycles[0].steps.map((s) => <li key={s.step} className={s.status}><b>{STEP_LABEL[s.step] ?? s.step}</b><span>{s.status}{s.status !== "skipped" || s.seconds ? ` · ${s.seconds}s` : ""}</span></li>)}</ol>
        </>
      )}
      <p className="gx-micro">Commands: <code>./scripts/worker.sh status | logs | restart | install | uninstall</code>. A sleeping or powered-off Mac records nothing; gaps stay gaps.</p>
    </section>
  );
}

function Summary({ d, sel, onSel }: { d: WorkspacePayload; sel: string; onSel: (t: string) => void }) {
  return (
    <section className="gx-card" data-testid="lr-horizons">
      <div className="gx-card-head"><h2>ACTIVE MODEL PER HORIZON</h2></div>
      <div className="gx-scroll"><table className="gx-table">
        <thead><tr><th>Horizon</th><th>Active (in charge)</th><th>Matured forward origins</th><th>Forward error vs unchanged</th><th>Development error vs unchanged</th><th>80% range: coverage · width</th></tr></thead>
        <tbody>{TASK_ORDER.map((id) => {
          const t = d.tasks[id];
          const dev = t.development.metrics;
          return (
            <tr key={id} className={sel === id ? "sel" : ""} onClick={() => onSel(id)} data-testid={`lr-row-${id}`} style={{ cursor: "pointer" }}>
              <td><b>{t.label}</b></td>
              <td>{t.active.is_unchanged_baseline ? "Unchanged price" : short(t.active.version)}</td>
              <td className="mono">{t.forward.distinct_origins} <span className="gx-muted">{id === "nowcast" ? "sessions" : `/ ${t.forward.blocks_needed} blocks`}</span></td>
              <td className="mono">{t.forward.n_matured ? pctS(t.forward.mae_vs_baseline_pct) : "none matured"}</td>
              <td className="mono">{pctS(dev?.mae_vs_baseline_pct)}</td>
              <td className="mono">{cov(t.range.development_coverage_pct)} · {wid(t.range.development_width_pct)}</td>
            </tr>
          );
        })}</tbody></table></div>
      <p className="gx-micro">Error vs unchanged: below 0% would mean smaller average error than assuming the price stays put. “Forward” = forecasts recorded before their outcome existed. “Development” = walk-forward on history, re-examined daily, so it is <b>not</b> an untouched test. A horizon needs 30 independent forward blocks (matured sessions ÷ horizon) before a verdict is allowed.</p>
    </section>
  );
}

function Charts({ t }: { t: WsTask }) {
  const s = t.development.series;
  const rows = s ? s.dates.map((d, i) => ({ date: d.slice(5), Actual: s.actual[i], Predicted: s.pred[i], Unchanged: s.base[i] })) : [];
  const r = t.development.rolling;
  const roll = r.dates.map((d, i) => ({ date: d.slice(5), Model: r.model[i], Unchanged: r.unchanged[i] }));
  const axis = { stroke: C.muted, fontSize: 11, tickLine: false } as const;
  return (
    <div className="gx-charts2">
      <div data-testid="lr-pva"><h4>Predicted vs actual <span className="gx-muted">· walk-forward on history (development)</span></h4>
        {rows.length ? <ResponsiveContainer width="100%" height={220}><LineChart data={rows} margin={{ left: 4, right: 8, top: 8 }}><CartesianGrid stroke={C.grid} /><XAxis dataKey="date" {...axis} minTickGap={32} /><YAxis {...axis} domain={["auto", "auto"]} width={52} />
          <Tooltip contentStyle={{ background: "#0F1318", border: "1px solid #1B2128", fontSize: 12 }} formatter={(v: number) => num(v)} /><Legend wrapperStyle={{ fontSize: 11 }} />
          <Line type="linear" dataKey="Actual" stroke={C.line} dot={false} strokeWidth={1.5} isAnimationActive={false} /><Line type="linear" dataKey="Predicted" stroke={C.accent} dot={false} strokeWidth={1.5} isAnimationActive={false} /><Line type="linear" dataKey="Unchanged" stroke={C.muted} dot={false} strokeDasharray="4 3" strokeWidth={1} isAnimationActive={false} /></LineChart></ResponsiveContainer>
          : <p className="gx-muted">Not stored yet: the next evaluation records it.</p>}
      </div>
      <div data-testid="lr-rolling"><h4>Rolling error, {r.window ?? 60} forecasts <span className="gx-muted">· USD, lower is better</span></h4>
        {roll.length ? <ResponsiveContainer width="100%" height={220}><LineChart data={roll} margin={{ left: 4, right: 8, top: 8 }}><CartesianGrid stroke={C.grid} /><XAxis dataKey="date" {...axis} minTickGap={32} /><YAxis {...axis} width={44} />
          <Tooltip contentStyle={{ background: "#0F1318", border: "1px solid #1B2128", fontSize: 12 }} formatter={(v: number) => num(v)} /><Legend wrapperStyle={{ fontSize: 11 }} />
          <Line type="linear" dataKey="Model" stroke={C.accent} dot={false} strokeWidth={1.5} isAnimationActive={false} /><Line type="linear" dataKey="Unchanged" stroke={C.muted} dot={false} strokeDasharray="4 3" strokeWidth={1} isAnimationActive={false} /></LineChart></ResponsiveContainer>
          : <p className="gx-muted">Needs a stored walk-forward series.</p>}
      </div>
    </div>
  );
}

function Candidates({ list }: { list: WsCandidate[] }) {
  return (
    <div className="gx-scroll"><table className="gx-table" data-testid="lr-candidates">
      <thead><tr><th>Candidate</th><th>Status</th><th>Dev. error vs unchanged</th><th>p (corrected later)</th><th>Range cov · width</th><th>Forward origins</th><th>Latest decision / attempt</th></tr></thead>
      <tbody>{list.map((c) => (
        <tr key={c.version} className={c.status === "rejected" ? "dim" : ""}>
          <td>{short(c.version)}{c.feature_schema === "fib_v1" && <span className="gx-chip base" style={{ marginLeft: 6 }}>experimental</span>}</td>
          <td>{c.status}</td>
          <td className="mono">{pctS(c.development?.mae_vs_baseline_pct)}</td><td className="mono">{c.development?.p_value_vs_baseline != null ? num(c.development.p_value_vs_baseline, 2) : "—"}</td>
          <td className="mono">{cov(c.development?.coverage_pct)} · {wid(c.development?.mean_width_pct)}</td>
          <td className="mono">{c.forward.distinct_origins}</td>
          <td className="gx-micro">{c.last_decision ? `${c.last_decision.decision}: ${c.last_decision.reason.slice(0, 90)}` : c.last_attempt ? `${c.last_attempt.status} ${when(c.last_attempt.at)}${c.last_attempt.detail ? ` — ${c.last_attempt.detail}` : ""}` : "—"}</td>
        </tr>
      ))}</tbody></table></div>
  );
}

function Detail({ t }: { t: WsTask }) {
  const f = t.forward, dev = t.development.metrics, tr = t.development.trend;
  return (
    <section className="gx-card" data-testid="lr-detail" data-task={t.task}>
      <div className="gx-card-head"><h2>{t.label.toUpperCase()}</h2><span className="gx-chip">{t.active.is_unchanged_baseline ? "Unchanged price in charge" : `${short(t.active.version)} in charge`}</span></div>
      <p className="gx-micro">{t.production_note} Trained through {t.active.trained_through ?? "—"}{t.active.last_trained_at ? `, last refit ${when(t.active.last_trained_at)}` : ""}.</p>

      <h3>Genuine forward results <Info label="Forward" text="Forecasts written to an append-only ledger before their outcome existed, scored once after. This is the only out-of-sample evidence." /></h3>
      <div data-testid="lr-forward">
        <dl className="gx-kv gx-al-kv">
          <div><dt>Distinct matured origins</dt><dd className="mono">{f.distinct_origins} <span className="gx-muted">({f.n_matured} scored forecasts · {f.distinct_targets} target sessions · {f.effective_blocks} of {f.blocks_needed} independent blocks)</span></dd></div>
          <div><dt>Error vs unchanged</dt><dd className="mono">{f.n_matured ? `${pctS(f.mae_vs_baseline_pct)} (model $${num(f.mae ?? 0)} vs unchanged $${num(f.baseline_mae ?? 0)})` : "none yet: the first outcome is scored after its target settlement"}</dd></div>
          <div><dt>80% range · coverage · width</dt><dd className="mono">{f.n_matured ? `${cov(f.coverage_pct)} inside · ${wid(f.mean_width_pct)} wide` : "—"}</dd></div>
        </dl>
        {f.history.length > 0 && (
          <div className="gx-scroll"><table className="gx-table"><thead><tr><th>Target</th><th>Origin</th><th>Predicted</th><th>Actual</th><th>Error</th><th>Unchanged error</th><th>Inside range</th></tr></thead>
            <tbody>{f.history.slice(0, 12).map((r) => <tr key={r.base_time + r.target_date}><td>{r.target_date}</td><td>{r.base_time}</td><td className="mono">{num(r.predicted)}</td><td className="mono">{num(r.actual)}</td><td className="mono">{num(r.abs_error)}</td><td className="mono">{num(r.baseline_abs_error)}</td><td>{r.in_range == null ? "—" : r.in_range ? "yes" : "no"}</td></tr>)}</tbody></table></div>
        )}
      </div>

      <h3>Walk-forward on history <Info label="Development" text="Re-examined every day, so it is development evidence, never an untouched test. Each forecast date uses only earlier data; targets that overlap the block being forecast are purged from training." /></h3>
      <div data-testid="lr-development">
        <p className="gx-micro">{t.development.label}.</p>
        {dev ? (
          <dl className="gx-kv gx-al-kv">
            <div><dt>Error vs unchanged</dt><dd className="mono">{pctS(dev.mae_vs_baseline_pct)} over {dev.n_origins} forecast dates (p = {dev.p_value_vs_baseline != null ? num(dev.p_value_vs_baseline, 2) : "—"}, one-sided)</dd></div>
            <div><dt>80% range · coverage · width</dt><dd className="mono">{cov(dev.coverage_pct)} inside · {wid(dev.mean_width_pct)} wide {dev.coverage_pct != null && dev.coverage_pct < 72 && <span className="gx-warn">Under-calibrated</span>}</dd></div>
          </dl>
        ) : <p className="gx-muted">Not evaluated yet.</p>}
        <Charts t={t} />
        <h4>Training history <span className={`gx-chip ${tr.verdict === "improved" ? "" : "base"}`} data-testid="lr-trend">{TREND_WORD[tr.verdict]}</span></h4>
        <p className="gx-micro">{tr.text}</p>
        {tr.points.length > 0 && <div className="gx-scroll"><table className="gx-table"><thead><tr><th>Data through</th><th>Error vs unchanged</th><th>Range coverage</th><th>Width</th><th>Forecast dates</th></tr></thead>
          <tbody>{[...tr.points].reverse().map((p) => <tr key={p.data_through}><td>{p.data_through}</td><td className="mono">{pctS(p.mae_vs_baseline_pct, 2)}</td><td className="mono">{cov(p.coverage_pct)}</td><td className="mono">{wid(p.mean_width_pct)}</td><td className="mono">{p.n_origins}</td></tr>)}</tbody></table></div>}
      </div>

      <h3>Candidate models and their latest decisions</h3>
      <Candidates list={t.candidates} />
      {t.decisions[0] && <p className="gx-micro" data-testid="lr-decision">Latest decision, {when(t.decisions[0].at)}: <b>{t.decisions[0].decision}</b> — {t.decisions[0].reason}</p>}
    </section>
  );
}

function FeatureGroups({ d }: { d: WorkspacePayload }) {
  const g = d.feature_groups;
  const v = g.verdict;
  return (
    <section className="gx-card" data-testid="lr-fib">
      <div className="gx-card-head"><h2>FEATURE GROUPS</h2><span className="gx-chip base">Fibonacci: experimental</span>
        {v && <span className={`gx-chip ${v.status === "supported" ? "" : "warnchip"}`} data-testid="lr-fib-verdict">{v.status === "supported" ? "Supported — kept" : v.status === "not_supported" ? "Not supported — rejected" : "Insufficient evidence"}</span>}</div>
      <p className="gx-micro">Same models, identical forecast dates and outcomes, with and without each addition. Negative = smaller error. The rule was fixed before any result: {g.rule}</p>
      {!g.evaluated || !g.comparisons ? <p className="gx-muted">Not evaluated yet.</p> : (
        <>
          <p className="gx-micro" data-testid="lr-fib-reason"><b>{v?.status}</b>: {v?.reason} (data through {g.data_through}).</p>
          {(["fib_vs_patterns", "patterns_vs_gold"] as const).map((name) => (
            <div key={name} className="gx-scroll"><h4>{name === "fib_vs_patterns" ? "With Fibonacci vs the same model without it" : "With chart-pattern features vs gold features alone"}</h4>
              <table className="gx-table"><thead><tr><th>Horizon</th><th>Model</th><th>Forecast dates</th><th>With, vs unchanged</th><th>Without, vs unchanged</th><th>Paired change (pts of unchanged error)</th><th>90% interval</th></tr></thead>
                <tbody>{Object.entries(g.comparisons![name] ?? {}).flatMap(([h, per]) => Object.entries(per).map(([algo, r]) => (
                  <tr key={name + h + algo}><td>{h}</td><td>{algo}</td><td className="mono">{r.n_origins}</td><td className="mono">{pctS(r.a_vs_unchanged_pct)}</td><td className="mono">{pctS(r.b_vs_unchanged_pct)}</td>
                    <td className="mono">{pctS(r.delta_pct_of_baseline_mae, 2)}</td><td className="mono">[{signed(r.ci90[0], 2)}, {signed(r.ci90[1], 2)}]{r.ci90[1] < 0 ? " ✓ better" : r.ci90[0] > 0 ? " ✗ worse" : ""}</td></tr>)))}</tbody></table></div>
          ))}
          {g.rejected && g.rejected.length > 0 && <p className="gx-micro">Rejected candidates stop issuing forecasts; their history is kept: {g.rejected.length} Fibonacci versions across the 3 horizons.</p>}
          <p className="gx-micro">{g.volume_note}</p>
        </>
      )}
    </section>
  );
}

function Quality({ d }: { d: WorkspacePayload }) {
  const q = d.quality;
  return (
    <section className="gx-card" data-testid="lr-quality">
      <div className="gx-card-head"><h2>DATA QUALITY</h2><span className={`gx-chip ${q.blocking.length ? "warnchip" : "base"}`}>{q.blocking.length ? "Learning blocked" : "Learning allowed"}</span></div>
      <p className="gx-micro">Problems are detected and recorded, never deleted: duplicates, out-of-order rows, invalid highs, wrong units, a stale feed, provider schema changes. Large moves are checked and kept, not removed for being large. Only a schema change, wrong units or a stale feed block learning (the working models stay in place).</p>
      <p className="gx-micro">By status: {Object.keys(q.by_status).length ? Object.entries(q.by_status).map(([k, v]) => `${k} ${v}`).join(" · ") : "nothing recorded"}. Ledger audit: {d.ledger_audit.conflicts} re-issue{d.ledger_audit.conflicts === 1 ? "" : "s"} from a different dataset snapshot (ignored; the original forecast and its timestamp stand, and each is recorded). {d.ledger_audit.fallbacks} intraday-model row{d.ledger_audit.fallbacks === 1 ? "" : "s"} issued while the model had no valid input for the target were flagged as not model output (kept, never scored, excluded from every model statistic).</p>
      {q.blocking.map((b) => <p key={b.key + b.kind} className="gx-micro gx-warnline">{b.kind}: {b.detail}</p>)}
      {q.recent.length > 0 && <details className="gx-al-details"><summary>Recent findings ({q.recent.length})</summary>
        <ul className="gx-al-list">{q.recent.map((f) => <li key={f.kind + f.key}><b>{f.kind}</b> · {f.key} · <span className="gx-muted">{f.status}</span><br /><span className="gx-micro">{f.detail}</span></li>)}</ul></details>}
    </section>
  );
}

function Attempts({ d }: { d: WorkspacePayload }) {
  return (
    <section className="gx-card" data-testid="lr-attempts">
      <div className="gx-card-head"><h2>TRAINING ATTEMPTS</h2></div>
      <p className="gx-micro">Every candidate attempt is kept, including failures ({d.attempts.ok} ok · {d.attempts.failed} failed). Nothing is ever erased to improve the record.</p>
      <div className="gx-scroll"><table className="gx-table"><thead><tr><th>When</th><th>Candidate</th><th>Stage</th><th>Result</th><th>Seconds</th></tr></thead>
        <tbody>{d.attempts.recent.slice(0, 12).map((a, i) => <tr key={i}><td>{when(a.at)}</td><td>{short(a.version)}</td><td>{a.stage.replace("_", " ")}</td><td className={a.status === "failed" ? "down" : ""}>{a.status}{a.detail ? ` — ${a.detail}` : ""}</td><td className="mono">{a.duration_s ?? "—"}</td></tr>)}</tbody></table></div>
      <details className="gx-al-details"><summary>How a model gets promoted</summary><ol className="gx-rule">{d.promotion_rule.map((r, i) => <li key={i}>{r}</li>)}</ol></details>
    </section>
  );
}

export function LearningPage() {
  const ws = usePoll<WorkspacePayload>(api.workspace, 30000, "workspace");
  const [sel, setSel] = useState<string>("fwd_5");
  if (!ws.data) return <div className="gx-page" data-testid="page-learning">{ws.error ? <ErrorBlock title="Could not load the learning workspace" message={ws.error} onRetry={ws.refresh} /> : <Skeleton h={320} />}</div>;
  const d = ws.data;
  return (
    <div className="gx-page" data-testid="page-learning">
      <p className="gx-micro">Learning means measuring. A model is only trusted after forecasts recorded <b>before</b> their outcomes beat the unchanged price. A job running is not improvement: the numbers below say whether measured performance improved, worsened or stayed the same.</p>
      <Runtime d={d} />
      <Summary d={d} sel={sel} onSel={setSel} />
      <div className="gx-seg wrap" role="tablist" aria-label="Horizon detail">
        {TASK_ORDER.map((id) => <button key={id} role="tab" aria-selected={sel === id} className={sel === id ? "on" : ""} onClick={() => setSel(id)} data-testid={`sel-${id}`}>{d.tasks[id].label}</button>)}
      </div>
      <Detail t={d.tasks[sel]} />
      <FeatureGroups d={d} />
      <Quality d={d} />
      <Attempts d={d} />
    </div>
  );
}
