// Pure functions used inside the n8n Code nodes. Kept in one file so they can be unit-tested with plain Node
// (n8n/test/logic.test.mjs) and inlined into the workflow by build_workflow.py. No network, no n8n globals in here.

const MORNING_WINDOW_MIN = 90;       // a morning SMS is only sent within this long after the configured time (never at noon because n8n was down)
const MAX_SYNC_AGE_H = 72;           // if the app has not synced for this long, say so in the SMS
const MAX_ATTEMPTS = 3;              // morning send attempts per local date

function localParts(epochSec, tz) {
  const f = new Intl.DateTimeFormat('en-GB', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  const p = Object.fromEntries(f.formatToParts(new Date(epochSec * 1000)).map((x) => [x.type, x.value]));
  const hour = p.hour === '24' ? 0 : Number(p.hour);
  return { dateKey: `${p.year}-${p.month}-${p.day}`, minutes: hour * 60 + Number(p.minute) };
}

// Which text goes out at `now`, from the stored snapshot. Mirrors backend n8n_link.decide_morning exactly.
function chooseMorningText(snap, now) {
  if (!snap) return { fresh: false, text: 'GOLD morning outlook: no forecast has been received from the app yet. Nothing is being repeated as new.' };
  const m = snap.morning || {};
  const fresh = m.text_fresh != null && now < m.valid_until;
  let text = fresh ? m.text_fresh : m.text_stale;
  const syncH = (now - snap.generated_at) / 3600;
  if (syncH > MAX_SYNC_AGE_H) text += ` (App last synced ${Math.round(syncH)} h ago.)`;
  return { fresh, text };
}

// Decide whether to send the morning SMS at `now`. `state` is the workflow static data (mutated only by recordMorning).
function decideMorning(state, now) {
  const snap = state.snapshot || null;
  const sch = (snap && snap.schedule) || { enabled: true, time: '07:30', tz: 'Europe/Berlin' };
  if (!sch.enabled) return { send: false, reason: 'morning SMS is switched off' };
  let lp;
  try { lp = localParts(now, sch.tz); } catch (e) { return { send: false, reason: `unknown time zone ${sch.tz}` }; }
  const [hh, mm] = String(sch.time).split(':').map(Number);
  const target = hh * 60 + mm;
  if (!(lp.minutes >= target && lp.minutes < target + MORNING_WINDOW_MIN)) return { send: false, reason: 'outside the morning window' };
  if (state.lastMorningSent === lp.dateKey) return { send: false, reason: 'already sent today' };
  const tries = (state.morningAttempts && state.morningAttempts[lp.dateKey]) || 0;
  if (tries >= MAX_ATTEMPTS) return { send: false, reason: 'too many failed attempts today' };
  const c = chooseMorningText(snap, now);
  return { send: true, dateKey: lp.dateKey, fresh: c.fresh, text: c.text };
}

function recordMorning(state, dateKey, accepted) {
  state.morningAttempts = state.morningAttempts || {};
  state.morningAttempts[dateKey] = (state.morningAttempts[dateKey] || 0) + 1;
  for (const k of Object.keys(state.morningAttempts)) if (k < dateKey) delete state.morningAttempts[k];
  if (accepted) state.lastMorningSent = dateKey;
}

function storeReview(state, review, now) {
  if (!review || !review.token || !review.html) return false;
  state.reviews = state.reviews || {};
  for (const [t, r] of Object.entries(state.reviews)) if (r.expires_at <= now) delete state.reviews[t];
  state.reviews[review.token] = { html: review.html, expires_at: review.expires_at };
  const keys = Object.keys(state.reviews);
  if (keys.length > 60) for (const t of keys.sort((a, b) => state.reviews[a].expires_at - state.reviews[b].expires_at).slice(0, keys.length - 60)) delete state.reviews[t];
  return true;
}

function lookupReview(state, token, now) {
  const r = token && state.reviews && state.reviews[token];
  if (!r) return { status: 403, html: page('Link invalid', 'This review link is invalid.') };
  if (r.expires_at <= now) return { status: 410, html: page('Link expired', 'This review link has expired.') };
  return { status: 200, html: r.html };
}

function page(title, msg) {
  return `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title}</title><p style="font:16px sans-serif;padding:24px">${msg}</p>`;
}

// Provider "accepted" (queued) is NOT "delivered". Receipts update the same record later.
function pushDelivery(state, rec) {
  state.deliveries = state.deliveries || [];
  state.deliveries.push(rec);
  if (state.deliveries.length > 200) state.deliveries.splice(0, state.deliveries.length - 200);
}

function applyReceipt(state, sid, status, now) {
  const rec = (state.deliveries || []).find((d) => d.sid === sid);
  if (!rec) return false;
  rec.status = String(status).toLowerCase();
  rec.at = now;
  return true;
}

function validAlert(body, now) {
  if (!body || body.type !== 'alert' || typeof body.text !== 'string' || !body.text) return { ok: false, error: 'bad payload' };
  if (body.expires_at != null && now > body.expires_at) return { ok: false, error: 'expired: not sent' };
  return { ok: true };
}

module.exports = { localParts, chooseMorningText, decideMorning, recordMorning, storeReview, lookupReview, pushDelivery, applyReceipt, validAlert, MORNING_WINDOW_MIN, MAX_SYNC_AGE_H, MAX_ATTEMPTS };
