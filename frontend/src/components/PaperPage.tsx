import { useEffect, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { usePoll } from "../hooks";
import type { PaperBook, PaperParams, PaperPayload } from "../types";
import { etDate, fmtEt, num, signed } from "../format";
import { C } from "../chartKit";
import { ErrorBlock, Skeleton } from "./Common";

const when = (t: number) => `${etDate(t * 1000)}, ${fmtEt(t, { hour: "2-digit", minute: "2-digit", hour12: false })} New York`;
const usd = (v: number | null | undefined) => (v == null ? "—" : `$${num(v)}`);
const sgn$ = (v: number) => `${v < 0 ? "−" : "+"}$${num(Math.abs(v))}`;
const pct = (v: number | null | undefined, d = 2) => (v == null ? "—" : `${signed(v, d)}%`);
const dir = (v: number) => (v > 0 ? "up" : v < 0 ? "down" : "");
const PARAM_LABEL: Record<string, string> = {
  min_net_edge_bps: "Required margin over costs (bps)", spread_bps: "Assumed spread (bps)", slippage_bps: "Assumed slippage (bps per side)", commission_per_oz: "Commission (USD per oz)",
  max_position_pct: "Max position (% of equity)", stop_loss_pct: "Stop-loss (%)", max_daily_loss_pct: "Daily loss limit (%)", max_drawdown_pct: "Drawdown limit (%)",
  max_trades_per_day: "Max fills per day", cooldown_minutes: "Cooldown after a fill (min)", quote_max_age_min: "Max quote age (min)",
};
const NUM_KEYS = Object.keys(PARAM_LABEL) as (keyof PaperParams)[];

function Params({ book, limits, busy, onSave }: { book: PaperBook; limits: PaperPayload["limits"]; busy: boolean; onSave: (p: Partial<PaperParams>) => void }) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  useEffect(() => { setDraft(Object.fromEntries(Object.entries(book.params).map(([k, v]) => [k, String(v)]))); }, [book.id, book.params]);
  const dirty = Object.entries(book.params).some(([k, v]) => draft[k] !== undefined && draft[k] !== String(v));
  const save = () => {
    const patch: Record<string, number | string> = {};
    for (const [k, v] of Object.entries(draft)) if (v !== String((book.params as unknown as Record<string, unknown>)[k])) patch[k] = NUM_KEYS.includes(k as keyof PaperParams) ? Number(v) : v;
    onSave(patch as Partial<PaperParams>);
  };
  return (
    <div data-testid="pp-params">
      <div className="gx-params">
        <label>Forecast that can open a position
          <select className="gx-input" value={draft.task ?? ""} onChange={(e) => setDraft({ ...draft, task: e.target.value })} data-testid="pp-task"><option value="fwd_1">1-session</option><option value="fwd_5">5-session</option><option value="fwd_20">20-session</option></select></label>
        <label>…taken from the model in role
          <select className="gx-input" value={draft.role ?? ""} onChange={(e) => setDraft({ ...draft, role: e.target.value })}><option value="champion">Champion (in charge)</option><option value="shadow">Shadow (experimental)</option><option value="baseline">Unchanged baseline</option></select></label>
        {NUM_KEYS.map((k) => (
          <label key={k}>{PARAM_LABEL[k]}
            <input className="gx-input mono" inputMode="decimal" value={draft[k] ?? ""} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} title={limits[k] ? `${limits[k][0]} to ${limits[k][1]}` : undefined} data-testid={`pp-in-${k}`} />
          </label>
        ))}
      </div>
      <div className="gx-row"><button className="gx-show on" disabled={busy || !dirty || book.status === "archived"} onClick={save} data-testid="pp-save">Save limits</button>
        <span className="gx-micro">Applies to future decisions only. Costs are <b>assumptions</b>; edit them to match a real broker's schedule.</span></div>
    </div>
  );
}

function Curve({ p }: { p: PaperBook["performance"] }) {
  const rows = p.curve.map((c) => ({ t: fmtEt(c.ts, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }), "Paper book": c.book, "Hold gold": c.hold, "Cash": c.cash }));
  if (rows.length < 2) return <p className="gx-muted" data-testid="pp-curve-empty">The curve starts as the worker marks the book (every couple of minutes while a quote is fresh).</p>;
  const axis = { stroke: C.muted, fontSize: 11, tickLine: false } as const;
  return (
    <div data-testid="pp-curve"><ResponsiveContainer width="100%" height={240}><LineChart data={rows} margin={{ left: 4, right: 8, top: 8 }}><CartesianGrid stroke={C.grid} /><XAxis dataKey="t" {...axis} minTickGap={48} /><YAxis {...axis} domain={["auto", "auto"]} width={64} tickFormatter={(v: number) => `$${num(v, 0)}`} />
      <Tooltip contentStyle={{ background: "#0F1318", border: "1px solid #1B2128", fontSize: 12 }} formatter={(v: number) => `$${num(v)}`} /><Legend wrapperStyle={{ fontSize: 11 }} />
      <Line type="linear" dataKey="Paper book" stroke={C.accent} dot={false} strokeWidth={2} isAnimationActive={false} /><Line type="linear" dataKey="Hold gold" stroke={C.line} dot={false} strokeWidth={1.2} isAnimationActive={false} /><Line type="linear" dataKey="Cash" stroke={C.muted} dot={false} strokeDasharray="4 3" strokeWidth={1} isAnimationActive={false} /></LineChart></ResponsiveContainer></div>
  );
}

export function PaperPage() {
  const { data, error, refresh } = usePoll<PaperPayload>(api.paper, 20000, "paper");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [confirmNew, setConfirmNew] = useState(false);
  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true); setNote(null);
    try { await fn(); setNote({ kind: "ok", text: ok }); refresh(); } catch (e) { setNote({ kind: "err", text: e instanceof Error ? e.message : "Failed." }); } finally { setBusy(false); setConfirmNew(false); }
  };
  if (!data) return <div className="gx-page" data-testid="page-paper">{error ? <ErrorBlock title="Could not load the paper book" message={error} onRetry={refresh} /> : <Skeleton h={320} />}</div>;
  const b = data.book;
  const p = b?.performance;
  return (
    <div className="gx-page" data-testid="page-paper">
      <section className="gx-card">
        <div className="gx-card-head"><h2>PAPER TRADING · GOLD ONLY</h2><span className="gx-chip warnchip" data-testid="pp-sim">Simulation · no broker connected</span></div>
        <p className="gx-micro" data-testid="pp-notice">{data.notice} A position opens only when a <b>stored forecast</b> (its id is written on the fill) predicts a return that beats the round-trip cost plus your margin; long or flat only. Exits: the forecast's target time, the stop-loss, or a risk limit (which also pauses the book). The simulated fills use the delayed futures quote plus the assumed spread and slippage.</p>
        {!b ? (
          <div className="gx-row"><p className="gx-muted">No paper book yet.</p><button className="gx-show on" disabled={busy} onClick={() => run(() => api.paperNewBook({}), "Started a fresh book.")} data-testid="pp-start">Start a book (10,000 simulated USD)</button></div>
        ) : (
          <>
            <div className="gx-card-head" style={{ marginTop: 6 }}>
              <b data-testid="pp-name">{b.name}</b>
              <span className={`gx-chip ${b.status === "active" ? "" : "warnchip"}`} data-testid="pp-status">{b.status === "active" ? "Active" : b.status === "paused" ? "Paused" : "Archived"}</span>
              {b.paused_reason && <span className="gx-micro" data-testid="pp-paused-reason">{b.paused_reason}</span>}
            </div>
            <div className="gx-cells" data-testid="pp-tiles">
              <div className="gx-cell"><span>Equity</span><b className="mono">{usd(p!.equity)}</b><small>started with {usd(p!.initial_cash)}</small></div>
              <div className="gx-cell"><span>Return</span><b className={`mono ${dir(p!.return_pct)}`}>{pct(p!.return_pct)}</b><small>realised {sgn$(p!.realised_pnl)} · unrealised {sgn$(p!.unrealised_pnl)}</small></div>
              <div className="gx-cell"><span>Position</span><b className="mono">{p!.qty > 0 ? `${num(p!.qty)} oz` : "Flat"}</b><small>{p!.qty > 0 && p!.avg_entry ? `avg entry ${usd(p!.avg_entry)} · mark ${usd(p!.mark)}` : `cash ${usd(p!.cash)}`}</small></div>
              <div className="gx-cell"><span>Trades · fees</span><b className="mono">{p!.n_trades}</b><small>{p!.win_rate_pct != null ? `${num(p!.win_rate_pct, 0)}% winners · ` : ""}fees {usd(p!.fees)} · max drawdown {p!.max_drawdown_pct == null ? "—" : `${num(p!.max_drawdown_pct)}%`}</small></div>
            </div>
            <div className="gx-row" style={{ marginTop: 8 }}>
              {b.status === "active" && <button className="gx-show" disabled={busy} onClick={() => run(() => api.paperPause(b.id), "Paused.")} data-testid="pp-pause">Pause</button>}
              {b.status === "paused" && <button className="gx-show on" disabled={busy} onClick={() => run(() => api.paperResume(b.id), "Resumed.")} data-testid="pp-resume">Resume</button>}
              {b.status !== "archived" && p!.qty > 0 && <button className="gx-show" disabled={busy} onClick={() => run(() => api.paperFlatten(b.id), "Closed the simulated position.")} data-testid="pp-flatten">Close simulated position</button>}
              {b.status !== "archived" && <button className="gx-show" disabled={busy} onClick={() => run(() => api.paperEvaluate(), "Evaluated now.")} data-testid="pp-eval">Evaluate now</button>}
              {!confirmNew
                ? <button className="gx-show" disabled={busy} onClick={() => setConfirmNew(true)} data-testid="pp-new">Start a fresh book…</button>
                : <><button className="gx-show on" disabled={busy} onClick={() => run(() => api.paperNewBook({}), "Archived the old book (kept) and started a fresh one.")} data-testid="pp-new-confirm">Confirm: archive this book and start fresh</button><button className="gx-textbtn" onClick={() => setConfirmNew(false)}>Cancel</button></>}
            </div>
            {note && <p className={`gx-micro ${note.kind === "err" ? "gx-err" : "gx-okmsg"}`} role="status" data-testid="pp-note">{note.text}</p>}
          </>
        )}
      </section>

      {b && p && (
        <>
          <section className="gx-card" data-testid="pp-compare">
            <div className="gx-card-head"><h2>VS HOLDING GOLD AND STAYING IN CASH</h2></div>
            <div className="gx-scroll"><table className="gx-table"><thead><tr><th></th><th>Value</th><th>Return</th></tr></thead><tbody>
              <tr><td><b>This book</b></td><td className="mono">{usd(p.equity)}</td><td className={`mono ${dir(p.return_pct)}`}>{pct(p.return_pct)}</td></tr>
              <tr><td>Hold gold (bought at the book's first mark{p.hold ? `, ${usd(p.hold.start_price)}` : ""}, same assumed costs)</td><td className="mono">{usd(p.hold?.equity)}</td><td className={`mono ${dir(p.hold?.return_pct ?? 0)}`}>{pct(p.hold?.return_pct)}</td></tr>
              <tr><td>Cash</td><td className="mono">{usd(p.cash_only.equity)}</td><td className="mono">{pct(p.cash_only.return_pct)}</td></tr></tbody></table></div>
            <Curve p={p} />
            <p className="gx-micro">A short or flat history proves nothing: with few trades, luck dominates. Profit and forecast accuracy are different questions and are reported separately.</p>
          </section>

          <section className="gx-card" data-testid="pp-accuracy">
            <div className="gx-card-head"><h2>PREDICTION ACCURACY (SEPARATE FROM PROFIT)</h2></div>
            <p className="gx-micro">For the forecast that drives this book ({b.accuracy.task.replace("fwd_", "")}-session, role {b.accuracy.role}), from the forecast ledger only.</p>
            <dl className="gx-kv gx-al-kv">
              <div><dt>Matured forecasts</dt><dd className="mono">{b.accuracy.n_matured ?? 0}{b.accuracy.n_matured ? ` over ${b.accuracy.n_sessions} target sessions` : " — none yet, so no accuracy claim"}</dd></div>
              <div><dt>Error vs unchanged</dt><dd className="mono">{b.accuracy.n_matured ? pct(b.accuracy.mae_vs_baseline_pct, 1) : "—"}</dd></div>
              <div><dt>Range coverage</dt><dd className="mono">{b.accuracy.coverage_pct != null ? `${num(b.accuracy.coverage_pct, 0)}%` : "—"}</dd></div>
            </dl>
          </section>

          <section className="gx-card">
            <div className="gx-card-head"><h2>RISK LIMITS AND COSTS</h2></div>
            <Params book={b} limits={data.limits} busy={busy} onSave={(patch) => run(() => api.paperParams(b.id, patch), "Saved. Future decisions use the new limits.")} />
          </section>

          <section className="gx-card" data-testid="pp-trades">
            <div className="gx-card-head"><h2>TRADES AND FILLS</h2></div>
            {b.trades.length === 0 && b.fills.length === 0 ? <p className="gx-muted" data-testid="pp-no-trades">No fills yet. Entries need a stored forecast that beats the round-trip cost plus margin; with no model edge, staying flat is the expected result.</p> : (
              <div className="gx-scroll"><table className="gx-table"><thead><tr><th>When</th><th>Side</th><th>Qty (oz)</th><th>Price</th><th>Cost</th><th>Why</th><th>Forecast id</th></tr></thead>
                <tbody>{b.fills.slice(0, 20).map((f) => <tr key={f.id}><td>{when(f.ts)}</td><td>{f.side}</td><td className="mono">{num(f.qty)}</td><td className="mono">{usd(f.price)}</td><td className="mono">{usd(f.cost)}</td><td className="gx-micro">{f.rationale}</td><td className="mono gx-micro">{f.forecast_id ?? "—"}</td></tr>)}</tbody></table></div>
            )}
            {b.trades.length > 0 && <p className="gx-micro">Closed trades: {b.trades.map((t) => `${sgn$(t.pnl)} (${t.exit_reason})`).join(" · ")}</p>}
          </section>

          <section className="gx-card" data-testid="pp-events">
            <div className="gx-card-head"><h2>DECISION LOG</h2></div>
            {b.events.length === 0 ? <p className="gx-muted">Nothing yet.</p> : <ul className="gx-al-list">{b.events.slice(0, 12).map((e, i) => <li key={i}><span className="gx-muted">{when(e.ts)}</span> · <b>{e.kind}</b> — {e.detail}</li>)}</ul>}
          </section>
        </>
      )}

      {data.books.length > 0 && (
        <section className="gx-card" data-testid="pp-books">
          <div className="gx-card-head"><h2>ALL BOOKS (NEVER DELETED)</h2></div>
          <div className="gx-scroll"><table className="gx-table"><thead><tr><th>Book</th><th>Status</th><th>Started</th><th>Start cash</th><th>Return</th><th>Trades</th></tr></thead>
            <tbody>{data.books.map((x) => <tr key={x.id}><td>{x.name}</td><td>{x.status}</td><td>{when(x.created_at)}</td><td className="mono">{usd(x.initial_cash)}</td><td className={`mono ${dir(x.return_pct)}`}>{pct(x.return_pct)}</td><td className="mono">{x.n_trades}</td></tr>)}</tbody></table></div>
        </section>
      )}
    </div>
  );
}
