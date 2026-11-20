import { useEffect, useState } from "react";
import { api } from "../api";
import { usePoll } from "../hooks";
import type { AlertEvent, AlertSettings, AlertsPayload, BasisCheck, TouchPanel } from "../types";
import { etDate, fmtEt, num } from "../format";

const usd = (v: number) => `$${num(v)}`;
const lv = (v: number) => `$${num(v, Number.isInteger(v) ? 0 : 2)}`;
const when = (epoch: number) => `${etDate(epoch * 1000)}, ${fmtEt(epoch, { hour: "2-digit", minute: "2-digit", hour12: false })} New York`;
const tday = (iso?: string) => (iso ? etDate(Date.parse(iso + "T12:00:00Z"), true) : "");
const TZS = ["Europe/Berlin", "America/New_York", "Europe/London", "UTC", "Asia/Tokyo", "Asia/Singapore", "Australia/Sydney"];
const TASK_LABEL: Record<string, string> = { fwd_1: "Next-session forecast", nowcast: "Next settlement (intraday)" };

const SMS_STATE: Record<string, string> = {
  not_requested: "No SMS requested (included in the morning outlook)", pending: "Waiting to be handed to n8n", simulated: "Simulated: no SMS was sent",
  handed_off: "Handed to n8n", accepted: "Accepted by the SMS provider · delivery not confirmed yet", delivered: "Delivered (confirmed by the provider)",
  undelivered: "Provider reports it was not delivered", failed: "Failed", expired: "Expired: not sent, it was too old to count as news",
};
const typeName = (t: AlertEvent["alert_type"]) => (t === "forecast_headsup" ? "Forecast heads-up" : "Actual-price alert");

function forecastCheckLines(d: AlertsPayload) {
  const c = d.last_forecast_check;
  if (!c) return null;
  return c.forecasts.map((f) => {
    const name = TASK_LABEL[f.task] ?? f.task;
    let text: string;
    if (f.verdict === "evaluated" && f.predicted != null) {
      const hit = d.settings.levels.filter((x) => f.predicted! >= x);
      text = `${usd(f.predicted)} for ${tday(f.target_date)} · ${hit.length ? `at or above ${hit.map(lv).join(" and ")}` : `below ${d.settings.levels.length > 1 ? "all" : "your"} review level${d.settings.levels.length > 1 ? "s" : ""}`}`;
    } else if (f.verdict === "none_issued") text = "none issued yet";
    else if (f.verdict === "baseline_copy") text = "unchanged-price baseline (equals the current price: not a prediction, so no heads-up; the price alert covers it)";
    else if (f.verdict.startsWith("stale")) text = `${f.predicted != null ? usd(f.predicted) + " for " + tday(f.target_date) + ", " : ""}stale, not used`;
    else text = f.verdict;
    return <li key={f.task} data-testid={`check-${f.task}`}><b>{name}:</b> {text}</li>;
  });
}

function TouchBlock({ t, levels }: { t: TouchPanel; levels: number[] }) {
  return (
    <section className="gx-al-block" data-testid="touch-block">
      <h3>Touch probability <span className="gx-chip base">Experimental</span></h3>
      <p className="gx-micro">A separate question from the settlement forecast: “will the day’s high reach the level?”. It is trained on actual daily highs and is never derived from the closing-price range.</p>
      <ul className="gx-al-list">
        {levels.map((L) => {
          const g = t.gates?.[String(L)];
          const latest = t.forward.latest.find((x) => Math.abs(x.level - L) < 1e-6 && x.displayed === 1);
          return (
            <li key={L} data-testid={`touch-${L}`}>
              <b>{lv(L)}:</b>{" "}
              {g?.display && latest ? <>about <b className="mono">{(latest.probability * 100).toFixed(1)}%</b> for {latest.target_date} (experimental estimate from distance and volatility only)</>
                : <>not shown. {g?.reasons?.[0] ? g.reasons[0][0].toUpperCase() + g.reasons[0].slice(1) + "." : t.reason ?? "No evidence yet."}</>}
            </li>
          );
        })}
      </ul>
      <details className="gx-al-details" data-testid="touch-evidence">
        <summary>Evidence behind this</summary>
        {t.variants ? (
          <>
            <p className="gx-micro">Event: {t.event}. Chronological walk-forward with purged boundaries; scaling and models fitted on training rows only. Selection slice {t.periods?.selection.join(" → ")} chose the variant ({t.selected_variant}); the final slice {t.periods?.final.join(" → ")} is reported, never used to choose anything. Data through {t.data_through}.</p>
            <div className="gx-scroll"><table className="gx-table">
              <thead><tr><th>Variant (final slice)</th><th>Brier</th><th>vs climatology</th><th>Calibration error</th><th>Events</th><th>Precision</th><th>Recall</th><th>False alerts</th><th>Missed</th></tr></thead>
              <tbody>{Object.entries(t.variants).map(([name, v]) => {
                const f = v.final, c = f.at_cut;
                return (<tr key={name}><td>{name}{name === t.selected_variant ? " ✓" : ""}</td><td className="mono">{f.brier?.toFixed(4)}</td><td className="mono">{f.brier_climatology?.toFixed(4)}</td><td className="mono">{f.ece?.toFixed(3)}</td>
                  <td className="mono">{c?.events}</td><td className="mono">{c?.precision != null ? `${(c.precision * 100).toFixed(0)}%` : "n/a"}</td><td className="mono">{c?.recall != null ? `${(c.recall * 100).toFixed(0)}%` : "n/a"}</td><td className="mono">{c?.false_alerts}</td><td className="mono">{c?.missed_events}</td></tr>);
              })}</tbody>
            </table></div>
            {t.improvement_vs_z_only_final && <p className="gx-micro">Log-loss change vs the distance-only baseline on the final slice: {Object.entries(t.improvement_vs_z_only_final).map(([k, x]) => `${k} ${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(1)}%`).join(" · ")}. Positive would mean the extra inputs helped; they did not make a reliable difference.</p>}
            <p className="gx-micro">Lead time: {t.lead_time?.min_h}–{t.lead_time?.max_h} h from issue to the event window. {t.lead_time?.note}</p>
            {t.live_levels?.map((x) => <p key={x.level} className="gx-micro">{lv(x.level)} sits {x.z} daily-volatility units away; the final slice has {x.final_slice.events ?? 0} distinct historical event dates near that distance (the display rule needs 30).</p>)}
          </>
        ) : <p className="gx-micro">The research has not run yet.</p>}
        <p className="gx-micro">Forward record (written before outcomes exist): {t.forward.recorded} predictions, {t.forward.scored} scored, {t.forward.hits} hits.</p>
        {t.endpoint && (
          <>
            <p className="gx-micro"><b>Settlement-forecast alerts</b> ({t.endpoint.oos.join(" → ")}): rule “point forecast ≥ level” against “settlement ≥ level”, only for sessions that started below the level. Fixed levels were rarely crossed in this history, so counts are small:</p>
            <div className="gx-scroll"><table className="gx-table">
              <thead><tr><th>Level</th><th>Variant</th><th>Sessions below</th><th>Events</th><th>Alerts</th><th>Precision</th><th>Recall</th><th>False</th><th>Missed</th></tr></thead>
              <tbody>{Object.entries(t.endpoint.absolute).flatMap(([L, byV]) => Object.entries(byV).map(([v, r]) => (
                <tr key={L + v}><td className="mono">{lv(Number(L))}</td><td>{v}</td><td className="mono">{r.sessions_below_level}</td><td className="mono">{r.events}</td><td className="mono">{r.alerts}</td>
                  <td className="mono">{r.precision != null ? `${(r.precision * 100).toFixed(0)}%` : "n/a"}</td><td className="mono">{r.recall != null ? `${(r.recall * 100).toFixed(0)}%` : "n/a"}</td><td className="mono">{r.false_alerts}</td><td className="mono">{r.missed_events}</td></tr>)))}</tbody>
            </table></div>
            <p className="gx-micro">Lead time: {t.endpoint.lead_time.hours} h ({t.endpoint.lead_time.note}).</p>
          </>
        )}
      </details>
    </section>
  );
}

export function AlertsPanel() {
  const { data, error, refresh } = usePoll<AlertsPayload>(api.alerts, 30000, "alerts");
  const [draft, setDraft] = useState<{ levels: string; entry: string; time: string; tz: string; exit: string; closedAt: string; reason: string } | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [basis, setBasis] = useState<BasisCheck | null>(null);
  const [basisErr, setBasisErr] = useState<string | null>(null);

  useEffect(() => {
    if (data && !dirty) setDraft({ levels: data.settings.levels.join(", "), entry: data.settings.entry_price != null ? String(data.settings.entry_price) : "", time: data.settings.morning_time, tz: data.settings.morning_tz,
      exit: data.settings.exit_price != null ? String(data.settings.exit_price) : "", closedAt: data.settings.closed_at ?? "", reason: data.settings.closure_reason ?? "" });
  }, [data, dirty]);

  const save = async (patch: Partial<AlertSettings>, okText = "Saved.") => {
    setBusy(true); setNote(null);
    try { await api.saveAlerts(patch); setDirty(false); setNote({ kind: "ok", text: okText }); refresh(); }
    catch (e) { setNote({ kind: "err", text: e instanceof Error ? e.message : "Could not save." }); }
    finally { setBusy(false); }
  };
  const saveDraft = () => {
    if (!draft) return;
    const levels = draft.levels.split(/[\s,;]+/).filter(Boolean).map(Number);
    if (!levels.length || levels.some((x) => !Number.isFinite(x))) { setNote({ kind: "err", text: "Review levels must be numbers, such as 4000, 4100." }); return; }
    const entry = draft.entry.trim() === "" ? null : Number(draft.entry);
    if (entry !== null && !Number.isFinite(entry)) { setNote({ kind: "err", text: "Entry price must be a number." }); return; }
    const exit = draft.exit.trim() === "" ? null : Number(draft.exit);
    if (exit !== null && !Number.isFinite(exit)) { setNote({ kind: "err", text: "Exit price must be a number." }); return; }
    const closed = data?.settings.position_status === "closed";
    void save({ levels, entry_price: entry, morning_time: draft.time, morning_tz: draft.tz, ...(closed ? { exit_price: exit, closed_at: draft.closedAt.trim() || null, closure_reason: draft.reason.trim() || null } : {}) }, "Saved. The new levels and morning time are handed to n8n.");
  };
  const loadBasis = async () => {
    if (basis) return;
    try { setBasis(await api.alertsBasis()); } catch (e) { setBasisErr(e instanceof Error ? e.message : "Could not check."); }
  };

  if (!data) return <section className="gx-card gx-alerts" data-testid="alerts-panel"><h2>ALERTS</h2><p className="gx-muted">{error ?? "Loading alerts…"}</p></section>;
  const s = data.settings, last = data.last_alert, d = data.delivery;
  const toggle = (key: "forecast_alerts" | "price_alerts" | "morning_enabled" | "immediate_sms", label: string, sub: string, id: string) => (
    <label className="gx-switch-row" data-testid={`row-${id}`}>
      <span><b>{label}</b><small>{sub}</small></span>
      <input type="checkbox" role="switch" checked={s[key]} disabled={busy} onChange={(e) => void save({ [key]: e.target.checked }, "Saved.")} data-testid={`sw-${id}`} />
      <i aria-hidden="true" />
    </label>
  );
  return (
    <section className="gx-card gx-alerts" aria-label="Alerts" data-testid="alerts-panel">
      <div className="gx-card-head">
        <h2>ALERTS</h2><span className="gx-chip base">Review levels · not advice</span>
        <span className={`gx-chip ${d.mode === "live" && d.configured ? "" : "base"}`} data-testid="delivery-mode" title={d.note}>{d.mode === "simulate" ? "Simulation · no SMS sent" : d.configured ? "Live via n8n" : "Live: not configured"}</span>
      </div>
      <p className="gx-micro">Two separate alerts. A <b>forecast heads-up</b> says the settlement forecast is at or above a level. An <b>actual-price alert</b> says an observed quote has reached it. Nothing buys or sells; no response means no trade.</p>

      <div className="gx-al-grid">
        <div>
          {toggle("forecast_alerts", "Forecast heads-up", "When a new settlement forecast is at or above a level. Not a touch of the level.", "forecast")}
          {toggle("price_alerts", "Actual-price crossing", `When a fresh observed ${s.quote_basis === "futures" ? "futures quote (~10 min delayed)" : "spot quote"} reaches a level.`, "price")}
          {toggle("immediate_sms", "Immediate SMS for a new forecast heads-up", "Off: heads-ups are listed here and included in the morning SMS.", "immediate")}
        </div>
        <div>
          <div className="gx-field-row">
            <label>Review levels (USD per troy oz)
              <input className="gx-input mono" value={draft?.levels ?? ""} onChange={(e) => { setDraft({ ...draft!, levels: e.target.value }); setDirty(true); }} data-testid="in-levels" inputMode="decimal" />
            </label>
            <label>{s.position_status === "open" ? "Entry price" : "Last entry price (history)"}
              <input className="gx-input mono" value={draft?.entry ?? ""} onChange={(e) => { setDraft({ ...draft!, entry: e.target.value }); setDirty(true); }} data-testid="in-entry" inputMode="decimal" />
            </label>
          </div>
          <ul className="gx-lvls" data-testid="levels">
            {data.levels.map((l) => <li key={l.level}><b className="mono">{lv(l.level)}</b>{l.note && <span className={`gx-chip ${l.note.startsWith("Below") ? "below" : "base"}`}>{l.note}</span>}</li>)}
          </ul>
          <div className="gx-position" data-testid="position">
            <label>Tracked Trading 212 position
              <select className="gx-input" value={s.position_status} disabled={busy} onChange={(e) => void save({ position_status: e.target.value as AlertSettings["position_status"] }, "Saved.")} data-testid="in-position">
                <option value="closed_unconfirmed">Closed (unconfirmed)</option><option value="closed">Closed (I will enter what I know)</option><option value="open">Open</option>
              </select>
            </label>
            <p className="gx-micro" data-testid="entry-note">{data.position.note}{s.entry_price != null ? ` Last entry recorded: ${usd(s.entry_price)} (as entered; its instrument is not verified).` : ""} {s.position_status === "open" ? data.labels.entry_note : "Review levels below are plain price levels, not comparisons with a position."}</p>
            {s.position_status === "closed" && (
              <div className="gx-field-row" data-testid="closure-fields">
                <label>Exit price (optional)<input className="gx-input mono" value={draft?.exit ?? ""} onChange={(e) => { setDraft({ ...draft!, exit: e.target.value }); setDirty(true); }} inputMode="decimal" data-testid="in-exit" /></label>
                <label>Closed on (optional)<input className="gx-input" value={draft?.closedAt ?? ""} placeholder="e.g. 2026-11-17" onChange={(e) => { setDraft({ ...draft!, closedAt: e.target.value }); setDirty(true); }} data-testid="in-closed-at" /></label>
                <label>Reason (optional, yours)<input className="gx-input" value={draft?.reason ?? ""} onChange={(e) => { setDraft({ ...draft!, reason: e.target.value }); setDirty(true); }} data-testid="in-reason" /></label>
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="gx-al-block">
        {toggle("morning_enabled", "Morning SMS outlook", "A concise next-session outlook from the latest valid forecast. If it is stale or missing, the SMS says so.", "morning")}
        <div className="gx-field-row">
          <label>Time
            <input type="time" className="gx-input mono" value={draft?.time ?? "07:30"} onChange={(e) => { setDraft({ ...draft!, time: e.target.value }); setDirty(true); }} data-testid="in-time" />
          </label>
          <label>Time zone
            <input className="gx-input" list="gx-tzs" value={draft?.tz ?? ""} onChange={(e) => { setDraft({ ...draft!, tz: e.target.value }); setDirty(true); }} data-testid="in-tz" />
            <datalist id="gx-tzs">{TZS.map((z) => <option key={z} value={z} />)}</datalist>
          </label>
          <button className="gx-show on" disabled={busy || !dirty} onClick={saveDraft} data-testid="save-alerts">Save changes</button>
        </div>
        {note && <p className={`gx-micro ${note.kind === "err" ? "gx-err" : "gx-okmsg"}`} role="status" data-testid="alerts-note">{note.text}</p>}
      </div>

      <div className="gx-al-block" data-testid="alerts-status">
        <h3>Status</h3>
        <dl className="gx-kv gx-al-kv">
          <div><dt>Last forecast checked</dt><dd data-testid="last-check">{data.last_forecast_check ? <>{when(data.last_forecast_check.at)}{!data.last_forecast_check.enabled && " · heads-up is off"}<ul className="gx-al-list">{forecastCheckLines(data)}</ul></> : "not yet checked (the worker checks every 2 minutes)"}</dd></div>
          <div><dt>Last alert</dt><dd data-testid="last-alert">{last
            ? <><b>{typeName(last.alert_type)}</b> · {lv(last.threshold)} · {when(last.created_at)}<br /><span className="gx-muted">{SMS_STATE[last.sms_state] ?? last.sms_state}</span>{last.deliveries[0]?.provider_sid && <span className="gx-muted mono"> · {last.deliveries[0].provider_sid}</span>}</>
            : "none yet"}</dd></div>
          <div><dt>Last hand-off to n8n</dt><dd data-testid="handoff">{data.last_handoff ? `${data.last_handoff.state} · ${when(data.last_handoff.at)}` : "not yet"}{d.mode === "live" && !d.configured ? ` · needs ${d.missing.join(", ")}` : ""}</dd></div>
        </dl>
        {data.recent.length > 1 && (
          <details className="gx-al-details"><summary>Recent alerts ({data.recent.length})</summary>
            <ul className="gx-al-list">{data.recent.map((e) => <li key={e.id}><b>{typeName(e.alert_type)}</b> {lv(e.threshold)} · {when(e.created_at)} · <span className="gx-muted">{SMS_STATE[e.sms_state] ?? e.sms_state}</span></li>)}</ul>
          </details>
        )}
      </div>

      <TouchBlock t={data.touch} levels={s.levels} />

      <details className="gx-al-details" onToggle={(e) => (e.currentTarget as HTMLDetailsElement).open && void loadBasis()} data-testid="basis">
        <summary>What do these levels apply to?</summary>
        {basisErr && <p className="gx-micro gx-err">{basisErr}</p>}
        {!basis && !basisErr && <p className="gx-micro">Checking quotes…</p>}
        {basis && (
          <>
            <dl className="gx-kv gx-al-kv">
              <div><dt>Instrument</dt><dd>{basis.levels_apply_to.instrument}</dd></div>
              <div><dt>Currency · unit</dt><dd>{basis.levels_apply_to.currency} · {basis.levels_apply_to.unit}</dd></div>
              <div><dt>Forecast target</dt><dd>{basis.levels_apply_to.forecast_target}</dd></div>
              <div><dt>Price alert quote</dt><dd>{basis.levels_apply_to.price_alert_quote} ({basis.levels_apply_to.price_alert_quote_note})</dd></div>
              {Object.entries(basis.quotes).map(([k, q]) => <div key={k}><dt>Latest {k}</dt><dd className="mono">{q.price != null ? `${usd(q.price)} · ${q.age_min} min old` : q.error ?? "unavailable"}</dd></div>)}
              {basis.spot_minus_futures != null && <div><dt>Spot minus futures</dt><dd className="mono">{basis.spot_minus_futures >= 0 ? "+" : "−"}${num(Math.abs(basis.spot_minus_futures))} (the two feeds are not the same price)</dd></div>}
              <div><dt>Entry price</dt><dd>{basis.entry_price.note}</dd></div>
            </dl>
            {basis.warnings.map((w, i) => <p key={i} className="gx-micro gx-warnline">{w}</p>)}
          </>
        )}
      </details>
      <p className="gx-micro">SMS links open a private, expiring review page with the forecast, the latest quote, your tracked position status and a link to Trading 212. Times in this panel are New York; the morning SMS uses {s.morning_tz}.</p>
    </section>
  );
}
