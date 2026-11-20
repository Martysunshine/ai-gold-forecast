// Browser verification of the Predict-on-chart workflow. Usage: URL=http://127.0.0.1:8750 OUT=/path node e2e/verify.mjs
import { chromium } from "playwright-core";
import fs from "node:fs";

const URL = process.env.URL || "http://127.0.0.1:8750";
const OUT = process.env.OUT || "e2e/screenshots";
fs.mkdirSync(OUT, { recursive: true });
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: !!ok }); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`); };
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: true });
const num = (s) => parseFloat(String(s).replace(/[^0-9.\-−]/g, "").replace("−", "-"));
const api = async (p) => (await fetch(`${URL}/api${p}`)).json();
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function open(viewport, { routes, hash = "" } = {}) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const errors = [], bad = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("response", (r) => { if (r.url().includes("/api/") && r.status() >= 400) bad.push(`${r.status()} ${r.url().replace(URL, "")}`); });
  if (routes) await routes(page);
  await page.goto(URL + hash);
  await page.waitForSelector('[data-testid="price"]', { timeout: 40000 });
  await page.waitForSelector('[data-testid="predict-bar"]', { timeout: 40000 });
  await page.waitForTimeout(2500);
  return { ctx, page, errors, bad };
}
const txt = async (page, id) => (await page.textContent(`[data-testid="${id}"]`)).trim();
const strip = (page) => page.locator('[data-testid="result-strip"][data-forecast-id]');
const predict = async (page, t, settle = 3500) => {
  await page.click(`[data-testid="target-${t}"]`);
  await page.click('[data-testid="predict"]');
  await page.waitForSelector('[data-testid="result-strip"][data-forecast-id]', { timeout: 60000 });
  await page.waitForTimeout(settle);
};
const zoom = (page) => page.evaluate(() => window.__gxChart.timeScale().getVisibleLogicalRange());
const csv = (t) => { const rows = []; let r = [], f = "", q = false; for (let i = 0; i < t.length; i++) { const c = t[i]; if (q) { if (c === '"' && t[i + 1] === '"') { f += '"'; i++; } else if (c === '"') q = false; else f += c; } else if (c === '"') q = true; else if (c === ",") { r.push(f); f = ""; } else if (c === "\n") { r.push(f); rows.push(r); r = []; f = ""; } else f += c; } if (f || r.length) { r.push(f); rows.push(r); } return Object.fromEntries(rows[0].map((h, i) => [h, rows[1][i]])); };

/** All forecast labels: none may overlap another, none may be clipped by the chart or sit over the price axis. */
async function labelAudit(page) {
  await page.locator('[data-testid="chart"]').scrollIntoViewIfNeeded(); await page.waitForTimeout(400);
  return page.evaluate(() => {
    const chart = document.querySelector('[data-testid="chart"]').getBoundingClientRect();
    const labels = [...document.querySelectorAll('[data-testid^="fc-label-"]')].map((e) => ({ id: e.dataset.testid, r: e.getBoundingClientRect() }));
    const overlaps = [];
    for (let i = 0; i < labels.length; i++) for (let j = i + 1; j < labels.length; j++) {
      const a = labels[i].r, b = labels[j].r;
      if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) overlaps.push(`${labels[i].id}×${labels[j].id}`);
    }
    const axisW = window.__gxChart.priceScale("right").width();
    const overAxis = labels.filter((l) => l.r.right > chart.right - axisW + 0.5).map((l) => l.id);
    const layer = document.querySelector(".gx-fc-layer"); const prev = layer ? layer.style.pointerEvents : "";
    if (layer) { layer.style.pointerEvents = "auto"; layer.querySelectorAll(".gx-lbl").forEach((n) => (n.style.pointerEvents = "auto")); }
    const covered = labels.filter((l) => { const el = document.elementFromPoint(l.r.left + l.r.width / 2, l.r.top + l.r.height / 2); return !(el && el.closest('[data-testid^="fc-label-"]')); }).map((l) => l.id);
    if (layer) { layer.style.pointerEvents = prev; layer.querySelectorAll(".gx-lbl").forEach((n) => (n.style.pointerEvents = "")); }
    const clipped = labels.filter((l) => l.r.left < chart.left - 0.5 || l.r.right > chart.right + 0.5 || l.r.top < chart.top - 0.5 || l.r.bottom > chart.bottom + 0.5).map((l) => l.id);
    const t = (id) => document.querySelector(`[data-testid="fc-label-${id}"]`)?.textContent || "";
    return { n: labels.length, overlaps, clipped, overAxis, covered, ids: labels.map((l) => l.id.replace("fc-label-", "")), forecast: t("forecast"), high: t("high"), low: t("low"), ref: t("ref"), actual: t("actual") };
  });
}
const patchJson = (page, glob, patch) => page.route(glob, async (route) => { const r = await route.fetch(); const j = await r.json(); await route.fulfill({ response: r, json: patch(j) }); });

// ============================== desktop: the real, unmodified app ==============================
{
  const { ctx, page, errors, bad } = await open({ width: 1440, height: 1000 });
  check("price summary: latest price, signed change, USD per troy oz, quote timestamp", num(await txt(page, "price")) > 500 && /[▲▼■]/.test(await txt(page, "change")) && /per troy oz/.test(await page.textContent(".gx-cur")) && /quote \d\d:\d\d:\d\d ET/.test(await txt(page, "conn-status")), await txt(page, "conn-status"));
  check("the two forecast cards and the Price history / Forecast view switch are gone", (await page.locator('[data-testid="today-card"], [data-testid="forward-card"], [data-testid="forecast-section"], [data-testid="view-history"], [data-testid="view-forecast"]').count()) === 0);
  const order = await page.evaluate(() => ["price", "chart-card", "result-strip"].map((id) => document.querySelector(`[data-testid="${id}"]`).getBoundingClientRect().top));
  check("layout: price → one chart → one result strip below it", order[0] < order[1] && order[1] < order[2], order.map(Math.round).join(" < "));
  check("the chart offers Line / Candles, six history ranges, four forecast choices and one Predict button", (await page.locator('[data-testid^="range-"]').count()) === 6 && (await page.locator('[data-testid^="mode-"]').count()) === 2 && (await page.locator('[data-testid^="target-"]').count()) === 4 && (await page.locator('[data-testid="predict"]').count()) === 1);
  check("before Predict: history only (no forecast labels, no connector text) and the strip explains what to do", (await page.locator('[data-testid^="fc-label-"]').count()) === 0 && (await page.getAttribute('[data-testid="chart"]', "data-view")) === "history" && /press Predict/.test(await txt(page, "result-strip")));
  check("the alerts panel, training logs and model lab are not on the Forecast page (backtest, drivers, methodology are tabs under the chart)", !/Forecast heads-up|Training attempts|Model lab|Promotion/i.test((await page.textContent('[data-testid="page-forecast"]')).replace(/Details: model, methodology, drivers & backtests →/, "")) && (await page.locator(".gx-al-grid, [data-testid=\"alerts-panel\"]").count()) === 0);
  await page.screenshot({ path: `${OUT}/desktop-before-predict.png`, fullPage: true });

  const ohlc = await api("/market/ohlc?instrument=futures&range=1D");
  check("futures 1D is real OHLC (high >= open/close >= low on every bar)", ohlc.real_ohlc && ohlc.bars.length > 20 && ohlc.bars.every((b) => b.high >= Math.max(b.open, b.close) && b.low <= Math.min(b.open, b.close)), `${ohlc.bars.length} bars, ${ohlc.interval}`);
  await page.locator('[data-testid="chart"]').scrollIntoViewIfNeeded(); await page.waitForTimeout(300);
  const box = await page.locator('[data-testid="chart"] .gx-chart-canvas').boundingBox();
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5); await page.waitForTimeout(500);
  const tipC = await page.locator(".gx-tip").allTextContents();
  check("candles: hover shows time, open, high, low, close and the interval", tipC.length && /Open/.test(tipC[0]) && /High/.test(tipC[0]) && /Low/.test(tipC[0]) && /Close/.test(tipC[0]) && /5-minute candle/.test(tipC[0]) && /ET/.test(tipC[0]), (tipC[0] || "").replace(/\n/g, " | "));
  await page.click('[data-testid="mode-line"]'); await page.waitForTimeout(600);
  await page.mouse.move(box.x + box.width * 0.45, box.y + box.height * 0.5); await page.waitForTimeout(500);
  const tipL = await page.locator(".gx-tip").allTextContents();
  check("line: hover shows observation time and price; 'Each point is a 5-minute close' is stated", tipL.length && /ET/.test(tipL[0]) && /Price [\d,]+\.\d\d/.test(tipL[0]) && /Each point is a 5-minute close/.test(await txt(page, "chart-foot")), (tipL[0] || "").replace(/\n/g, " | "));
  await page.click('[data-testid="mode-candles"]');
  await page.click('[data-testid="chart-info"]');
  check("provider and session explanations are behind the information button", /Source:/.test(await txt(page, "chart-info-panel")) && /Session:/.test(await txt(page, "chart-info-panel")));
  await page.click('[data-testid="chart-info"]');
  for (const r of ["1W", "1M", "3M", "1Y", "5Y", "1D"]) {
    await page.click(`[data-testid="range-${r}"]`); await page.waitForTimeout(1300);
    const d = await api(`/market/ohlc?instrument=futures&range=${r}`);
    check(`range ${r}: real ${d.interval} candles`, d.bars.length > 20 && (await page.getAttribute(`[data-testid="range-${r}"]`, "aria-selected")) === "true", `${d.bars.length} bars`);
  }

  // ---- every horizon: choose → Predict → the forecast appears on the same chart ----
  const api4 = { next: await api("/forecast/today"), 1: await api("/forecast/forward?horizon=1"), 5: await api("/forecast/forward?horizon=5"), 20: await api("/forecast/forward?horizon=20") };
  const ids = {};
  for (const t of ["next", "1", "5", "20"]) {
    const src = api4[t], f = src.forecast;
    if (!f) { // an unavailable horizon must say why and draw nothing
      await page.click(`[data-testid="target-${t}"]`); await page.click('[data-testid="predict"]'); await page.waitForTimeout(2500);
      check(`horizon ${t}: no forecast yet → explained, nothing drawn`, /\w{20}/.test(await txt(page, "predict-error")) && (await page.locator('[data-testid^="fc-label-"]').count()) === 0, await txt(page, "predict-error"));
      continue;
    }
    await predict(page, t);
    const a = await labelAudit(page);
    const sp = await txt(page, "strip-price"), sr = await txt(page, "strip-range"), ref = await txt(page, "strip-ref");
    ids[t] = await strip(page).getAttribute("data-forecast-id");
    check(`horizon ${t}: Predict draws the stored forecast on the SAME chart (price, range, reference equal the strip and the API)`,
      ids[t] === f.forecast_id && num(sp) === f.predicted && sr.includes(f.low.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",")) && ref.includes(f.reference_price.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",")) &&
      a.forecast.includes(f.predicted.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",")) && a.high.includes(f.high.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",")) && a.low.includes(f.low.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",")), `${sp} ${sr} | ${a.forecast.replace(/\s+/g, " ").slice(0, 60)}`);
    check(`horizon ${t}: origin, forecast (price + target date), range ends and 'Illustrative connection' are all labelled`, ["latest", "ref", "forecast", "high", "low", "conn"].every((k) => a.ids.includes(k)) && /\d{1,2} \w{3} · \d\d:\d\d New York/.test(a.forecast) && /Forecast origin/.test(a.ref), a.ids.join(","));
    check(`horizon ${t}: labels never overlap, are never clipped, never sit under the chart or over the price axis`, a.overlaps.length === 0 && a.clipped.length === 0 && a.covered.length === 0 && a.overAxis.length === 0, `overlaps ${a.overlaps.join(",") || "none"} clipped ${a.clipped.join(",") || "none"} axis ${a.overAxis.join(",") || "none"}`);
    const tgt = await txt(page, "strip-target");
    check(`horizon ${t}: strip shows target date with New York and Berlin time, expected change, the reference and ONE performance note`, /\d{4}/.test(tgt) && /New York · \d\d:\d\d Berlin/.test(await page.textContent('[data-testid="strip-target"]')) && /[▲▼]|Little change/.test(await txt(page, "strip-change")) && /Reference \$[\d,]+\.\d\d/.test(ref) && /(official settlement of|latest futures quote)/.test(ref) && (await page.locator('[data-testid="strip-perf"]').count()) === 1, tgt);
    check(`horizon ${t}: no model ids or version names on the main screen`, !/nowcast:|fwd_\d|price_now_v1|gold_v1|drivers_v1/.test(await page.textContent('[data-testid="page-forecast"]')));
    if (t === "5" || t === "20") check(`horizon ${t}: Predict moved a too-short history range to a daily one so origin and target are both readable`, ["range-3M", "range-1Y"].includes(await page.evaluate(() => document.querySelector('[data-testid^="range-"][aria-selected="true"]')?.dataset.testid)));
    await page.screenshot({ path: `${OUT}/desktop-predict-${t}.png`, fullPage: true });
  }
  check("horizons 5 and 20 have distinct future targets", api4[5].forecast.target_date !== api4[20].forecast.target_date && new Date(api4[20].forecast.target_date) > new Date(api4[5].forecast.target_date));
  {
    // ---- the next-settlement card: honest about what produced it, no fake estimates, today's settlement kept separate ----
    await predict(page, "next");
    const n = await api("/forecast/today"), st = (await page.textContent('[data-testid="result-strip"]')) || "";
    check("next-settlement card: titled by its target day, says what produced it once, shows issue time and input-data time", /^(Today|Tomorrow|Next settlement) · \w{3} \d{1,2} \w{3}$/.test(await txt(page, "strip-title")) && (/Reference estimate: assumes no change\./.test(await txt(page, "strip-model")) || /model/i.test(await txt(page, "strip-model"))) && /Issued .+New York\. Input data: .+New York/.test(await txt(page, "strip-ref")), await txt(page, "strip-title"));
    check("experimental-model lists and evaluation paragraphs are not on the card", !/Experimental model estimates|shadow|Backtest: [+−-]/.test(st.replace(/No demonstrated advantage[\s\S]*$/, "")) && (await page.locator('[data-testid="strip-experimental"]').count()) === 0);
    const sameAsBaseline = (n.experimental || []).filter((e) => e.predicted === n.forecast.predicted);
    check("no experimental estimate silently equals the baseline: an intraday model that cannot predict this target is unavailable, not shown as a number", n.route === "intraday" ? true : (n.experimental.length === 0 && n.intraday.available === false && n.intraday.unavailable_models.length > 0 && sameAsBaseline.length === 0), `route ${n.route}, unavailable ${n.intraday?.unavailable_models?.length}`);
    check("a next-session target is routed to the stored daily forecast or an explicitly labelled baseline, with the matching reference", n.route === "intraday" || (n.route === "daily_model" ? n.forecast.horizon === 1 && n.forecast.source.kind === "model" : n.forecast.source.kind === "baseline" && /assumes no change/.test(n.daily_pending)), n.route);
    if (n.last_result) check("today's settlement is a separate record, pending until the official close is posted", (await page.locator('[data-testid="settlement-record"]').count()) === 1 && n.last_result.forecast_id !== n.forecast.forecast_id && (n.last_result.actual == null ? /Official close: pending/.test(await txt(page, "settlement-record")) : /Actual close/.test(await txt(page, "settlement-record"))), (await txt(page, "settlement-record")).slice(0, 140));
    await page.click('[data-testid="dtab-model"]'); await page.waitForSelector('[data-testid="experimental"]', { timeout: 20000 });
    check("Details → Model holds the experimental estimates (or says they are unavailable for this target)", (await page.locator('[data-testid="exp-row"]').count()) > 0 || (await page.locator('[data-testid="exp-unavailable"]').count()) === 1);
    await page.click('[data-testid="dtab-backtest"]');
  }
  const learn = await api("/learning");
  await page.click('[data-testid="target-20"]'); await page.click('[data-testid="predict"]'); await page.waitForTimeout(3000);
  const cal = await txt(page, "calibration");
  check("20-session: '80% target range · historical coverage X%' and 'Under-calibrated' when materially below target", /80% (target )?range · historical coverage \d+%/.test(cal) && (learn.tasks.fwd_20.calibration.historical_pct < 72 ? /target range/.test(cal) && /Under-calibrated/.test(cal) : !/Under-calibrated/.test(cal)), cal.replace(/\s+/g, " "));

  // ---- the forecast stays when switching Line/Candles; zoom is preserved ----
  await page.click('[data-testid="target-5"]'); await page.click('[data-testid="predict"]'); await page.waitForTimeout(3500);
  const sid = await strip(page).getAttribute("data-forecast-id");
  const l0 = await page.locator('[data-testid="fc-label-forecast"]').textContent();
  await page.click('[data-testid="mode-line"]'); await page.waitForTimeout(900);
  const l1 = await page.locator('[data-testid="fc-label-forecast"]').textContent();
  check("switching to Line keeps the same forecast on the chart, unchanged", l0 === l1 && (await strip(page).getAttribute("data-forecast-id")) === sid, l1.replace(/\s+/g, " ").slice(0, 60));
  await page.screenshot({ path: `${OUT}/desktop-predict-line.png`, fullPage: true });
  await page.click('[data-testid="mode-candles"]'); await page.waitForTimeout(600);
  const fit0 = await zoom(page);
  await page.evaluate(() => { const c = window.__gxChart; const r = c.timeScale().getVisibleLogicalRange(); c.timeScale().setVisibleLogicalRange({ from: r.from + 6, to: r.to - 2 }); });
  await page.waitForTimeout(500);
  const z0 = await zoom(page);
  check("test setup: user zoom applied", Math.abs(z0.from - (fit0.from + 6)) < 1.5);
  await page.click('[data-testid="mode-line"]'); await page.waitForTimeout(600); await page.click('[data-testid="mode-candles"]'); await page.waitForTimeout(600);
  const z1 = await zoom(page);
  check("zoom is preserved when switching Line/Candles", Math.abs(z1.from - z0.from) < 0.5 && Math.abs(z1.to - z0.to) < 0.5, `${z0.from.toFixed(1)}..${z0.to.toFixed(1)} → ${z1.from.toFixed(1)}..${z1.to.toFixed(1)}`);
  await wait(38000);
  const z2 = await zoom(page);
  check("zoom is preserved across live quote updates, and the forecast is unchanged", Math.abs(z2.from - z0.from) < 1 && Math.abs(z2.to - z0.to) < 1 && (await strip(page).getAttribute("data-forecast-id")) === sid, `${z0.from.toFixed(1)}..${z0.to.toFixed(1)} → ${z2.from.toFixed(1)}..${z2.to.toFixed(1)}`);
  await page.click('[data-testid="fit-forecast"]'); await page.waitForTimeout(700);
  const z3 = await zoom(page);
  check("'Fit forecast' refits the view to the forecast", Math.abs(z3.from - fit0.from) < 1.5 && Math.abs(z3.to - fit0.to) < 1.5, `${z3.from.toFixed(1)}..${z3.to.toFixed(1)} vs fit ${fit0.from.toFixed(1)}..${fit0.to.toFixed(1)}`);
  await page.evaluate(() => { const c = window.__gxChart; const r = c.timeScale().getVisibleLogicalRange(); c.timeScale().setVisibleLogicalRange({ from: r.from + 8, to: r.to - 3 }); });
  await page.click('[data-testid="predict"]'); await page.waitForTimeout(1500);
  const z4 = await zoom(page);
  check("pressing Predict again refits (a new forecast request resets the view)", Math.abs(z4.from - fit0.from) < 1.5, `${z4.from.toFixed(1)} vs ${fit0.from.toFixed(1)}`);

  // ---- an old result is never relabelled as a new prediction ----
  await page.click('[data-testid="range-3M"]'); await page.waitForTimeout(1800);
  check("changing the history range keeps the same forecast (same id, same strip)", (await strip(page).getAttribute("data-forecast-id")) === sid && (await page.locator('[data-testid="fc-label-forecast"]').count()) === 1);
  await page.click('[data-testid="range-1M"]'); await page.waitForTimeout(1500);
  await page.click('[data-testid="target-20"]'); await page.waitForTimeout(800);
  check("choosing a different horizon clears the displayed forecast until Predict is pressed", (await page.locator('[data-testid="result-strip"][data-forecast-id]').count()) === 0 && (await page.locator('[data-testid^="fc-label-"]').count()) === 0 && (await page.getAttribute('[data-testid="chart"]', "data-view")) === "history");
  await page.click('[data-testid="predict"]'); await page.waitForSelector('[data-testid="result-strip"][data-forecast-id]'); await page.waitForTimeout(2500);

  // ---- export equals the displayed forecast ----
  const href = await page.getAttribute('[data-testid="download"]', "href");
  const body = await (await page.request.get(URL + href)).text();
  const row = csv(body);
  const shownId = await strip(page).getAttribute("data-forecast-id");
  const fnow = api4[20].forecast;
  check("Download CSV is the displayed forecast (id, price, range, reference, change, target date)", row.forecast_id === shownId && num(row.predicted_price_usd) === num(await txt(page, "strip-price")) && (await txt(page, "strip-range")).includes(Number(row.range_low_usd).toLocaleString("en-US", { minimumFractionDigits: 2 })) &&
    (await txt(page, "strip-range")).includes(Number(row.range_high_usd).toLocaleString("en-US", { minimumFractionDigits: 2 })) && (await txt(page, "strip-ref")).includes(Number(row.reference_price_usd).toLocaleString("en-US", { minimumFractionDigits: 2 })) &&
    row.target_date === fnow.target_date && Math.abs(Number(row.change_pct) - fnow.change_pct) < 0.006, `${row.forecast_id} ${row.predicted_price_usd}`);

  // ---- Hide forecast restores the historical chart ----
  await page.click('[data-testid="hide-forecast"]'); await page.waitForTimeout(1200);
  check("Hide forecast restores the historical chart (no labels, no strip result, no leftover forecast axis)", (await page.locator('[data-testid^="fc-label-"]').count()) === 0 && (await page.getAttribute('[data-testid="chart"]', "data-view")) === "history" && (await page.locator('[data-testid="result-strip"][data-forecast-id]').count()) === 0 && (await page.locator('[data-testid="hide-forecast"]').count()) === 0);

  // ---- spot ----
  await page.click('[data-testid="inst-spot"]'); await page.waitForTimeout(3500);
  const sp = await api("/market/summary?instrument=spot");
  check("spot selected: summary and chart are spot; Predict is unavailable with one clear explanation", /Spot/.test(sp.name) && (await page.isDisabled('[data-testid="predict"]')) && (await page.locator('[data-testid="futures-note"]').count()) === 1 && (await page.locator('[data-testid="result-strip"]').count()) === 0);
  const prevTxt = await page.textContent(".gx-statrow");
  check("spot previous close: a recorded value or 'Unavailable from provider', never a futures substitute", sp.previous_close == null ? /Unavailable from provider/.test(prevTxt) : /[\d,]+\.\d\d/.test(prevTxt));
  await page.click('[data-testid="inst-futures"]'); await page.waitForTimeout(1500);

  // ---- secondary surfaces ----
  await page.click('[data-testid="open-details"]'); await page.waitForSelector('[data-testid="details-panel"]'); await page.click('[data-testid="dtab-model"]'); await page.waitForSelector('[data-testid="model-tab"]', { timeout: 20000 });
  check("Details tabs under the chart hold backtest, drivers, GLD, methodology, model and news; calibration audit shows coverage and width", (await page.locator('[data-testid^="dtab-"]').count()) === 6 && (await page.locator('[data-testid="calibration-audit"] tbody tr').count()) === 3);
  await page.screenshot({ path: `${OUT}/desktop-details.png` });
  await page.click('[data-testid="nav-alerts"]'); await page.waitForSelector('[data-testid="page-alerts"]'); await page.waitForTimeout(1500);
  check("Alerts & settings is its own page: alerts panel and data sources", /Alerts/i.test(await page.textContent('[data-testid="page-alerts"]')) && (await page.locator('[data-testid="settings-data"]').count()) === 1);
  await page.screenshot({ path: `${OUT}/desktop-alerts.png`, fullPage: true });
  await page.click('[data-testid="nav-forecast"]'); await page.waitForSelector('[data-testid="page-forecast"]');
  check("no console errors or failed API calls on the real app", errors.length === 0 && bad.length === 0, [...errors, ...bad].slice(0, 4).join(" | "));
  await ctx.close();
}

// ============================== states: loading, error, newer forecast, completed ==============================
{
  const { ctx, page } = await open({ width: 1440, height: 1000 }, { routes: async (p) => { await p.route("**/api/forecast/forward*", async (route) => { await new Promise((r) => setTimeout(r, 1800)); await route.continue(); }); } });
  await page.click('[data-testid="target-5"]'); await page.click('[data-testid="predict"]');
  await page.waitForTimeout(500);
  check("loading: Predict shows 'Predicting…' and cannot be pressed twice", /Predicting/.test(await txt(page, "predict")) && (await page.isDisabled('[data-testid="predict"]')));
  await page.waitForSelector('[data-testid="result-strip"][data-forecast-id]', { timeout: 20000 });
  check("loading ends with the forecast shown", !/Predicting/.test(await txt(page, "predict")));
  await ctx.close();
}
{
  const { ctx, page } = await open({ width: 1440, height: 1000 }, { routes: async (p) => { await p.route("**/api/forecast/forward*", (route) => route.abort()); } });
  await page.click('[data-testid="target-5"]'); await page.click('[data-testid="predict"]'); await page.waitForTimeout(1500);
  check("error: a failed request says so, draws nothing, and does not leave a stale forecast behind", /Could not get the forecast/.test(await txt(page, "predict-error")) && (await page.locator('[data-testid^="fc-label-"]').count()) === 0 && (await page.locator('[data-testid="result-strip"][data-forecast-id]').count()) === 0);
  await ctx.close();
}
{
  let n = 0;
  const { ctx, page } = await open({ width: 1440, height: 1000 }, { routes: async (p) => { await patchJson(p, "**/api/forecast/forward*", (j) => (++n > 1 && j.forecast ? { ...j, forecast: { ...j.forecast, forecast_id: j.forecast.forecast_id + "|newer" } } : j)); } });
  await page.click('[data-testid="target-5"]'); await page.click('[data-testid="predict"]'); await page.waitForSelector('[data-testid="result-strip"][data-forecast-id]'); await page.waitForTimeout(3000);
  check("a newer forecast is offered (chip) but never swapped in behind the user's back", /newer forecast/i.test(await txt(page, "result-strip")) && !(await strip(page).getAttribute("data-forecast-id")).endsWith("|newer"));
  await ctx.close();
}
{
  const { ctx, page, errors } = await open({ width: 1440, height: 1000 }, {
    routes: async (p) => {
      await patchJson(p, "**/api/forecast/forward*", (j) => !j.forecast ? j : { ...j, status: "completed", forecast: { ...j.forecast, target_time: "2026-11-10T13:30:00-05:00", target_date: "2026-11-10", input_cutoff: Date.parse("2026-11-05T18:30:00Z") / 1000 },
        result: { matured: true, actual: j.forecast.predicted + 40, abs_error: 40, in_range: true } });
      await patchJson(p, "**/api/forecast/today", (j) => !j.forecast ? j : ({ ...j, status: "current", forecast: { ...j.forecast, predicted: j.forecast.reference_price * 1.031, low: j.forecast.reference_price * 1.0, high: j.forecast.reference_price * 1.06, change: j.forecast.reference_price * 0.031, change_pct: 3.1, little_change: false } }));
    },
  });
  await predict(page, "5");
  check("a forecast whose target has passed is shown as COMPLETED, with the actual and the error", /Completed/.test(await txt(page, "completed-chip")) && /Actual/.test(await txt(page, "completed-banner")));
  const a5 = await labelAudit(page);
  check("completed target on the chart: the target and the actual are both labelled with no overlap or clipping", a5.ids.includes("actual") && a5.overlaps.length === 0 && a5.clipped.length === 0, a5.ids.join(","));
  await page.screenshot({ path: `${OUT}/desktop-predict-completed.png`, fullPage: true });
  await predict(page, "next");
  const a = await labelAudit(page);
  check("a large predicted move shows the signed change and separates the labels", !/Little change/.test(await txt(page, "strip-change")) && /[▲▼]/.test(await txt(page, "strip-change")) && a.n >= 6 && a.overlaps.length === 0 && a.clipped.length === 0, `${a.ids.join(",")} overlaps ${a.overlaps.join(",") || "none"}`);
  await page.screenshot({ path: `${OUT}/desktop-predict-large-gap.png`, fullPage: true });
  check("no console errors with edited payloads", errors.length === 0, errors.slice(0, 3).join(" | "));
  await ctx.close();
}
{
  const { ctx, page } = await open({ width: 1440, height: 1000 }, {
    routes: async (p) => {
      await patchJson(p, "**/api/market/summary*", (j) => ({ ...j, status: { state: "stale", label: "Stale: no recent trades" }, delay_seconds: 5400 }));
      await p.route("**/api/forecast/today", (route) => route.fulfill({ json: { status: "none", message: "No settlement forecast has been issued yet. One is issued each hour while the market is open." } }));
    },
  });
  check("a stale quote is labelled Stale, not Live", /Stale/.test(await txt(page, "conn-status")) && /Stale/.test(await txt(page, "feed-status")) && !/Live/.test(await txt(page, "feed-status")), await txt(page, "feed-status"));
  await page.click('[data-testid="target-next"]'); await page.click('[data-testid="predict"]'); await page.waitForTimeout(1500);
  check("a missing next-settlement forecast says so instead of showing a number", /No settlement forecast has been issued yet/.test(await txt(page, "predict-error")) && (await page.locator('[data-testid="result-strip"][data-forecast-id]').count()) === 0);
  await ctx.close();
}
{
  const { ctx, page } = await open({ width: 1440, height: 900 });
  const keep = num(await txt(page, "price"));
  await page.route("**/api/market/**", (r) => r.abort());
  await page.waitForFunction(() => /Reconnecting|Offline/.test(document.querySelector('[data-testid="conn-status"]')?.textContent || ""), null, { timeout: 40000 });
  check("outage shows 'Reconnecting…' and keeps the last good price", num(await txt(page, "price")) === keep, await txt(page, "conn-status"));
  await page.unroute("**/api/market/**");
  await page.waitForFunction(() => /Live|Delayed|closed/i.test(document.querySelector('[data-testid="conn-status"]')?.textContent || ""), null, { timeout: 40000 });
  check("status recovers automatically", true, await txt(page, "conn-status"));
  await ctx.close();
}

// ============================== mobile 390 ==============================
{
  const { ctx, page, errors, bad } = await open({ width: 390, height: 844 });
  const sw = await page.evaluate(() => ({ s: document.documentElement.scrollWidth, c: document.documentElement.clientWidth }));
  check("390px: no page-level horizontal scroll", sw.s <= sw.c + 1, `${sw.s} vs ${sw.c}`);
  const small = await page.evaluate(() => [...document.querySelectorAll(".gx button, .gx-nav a")].filter((b) => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height < 28; }).map((b) => b.textContent.trim().slice(0, 12)));
  check("390px: touch targets are comfortable", small.length === 0, small.join(","));
  await page.screenshot({ path: `${OUT}/mobile-before-predict.png`, fullPage: true });
  for (const t of ["next", "5", "20"]) {
    await predict(page, t);
    const a = await labelAudit(page);
    check(`390px horizon ${t}: labels do not overlap, are not clipped, not under the chart and not over the axis`, a.n >= 5 && a.overlaps.length === 0 && a.clipped.length === 0 && a.covered.length === 0 && a.overAxis.length === 0, `${a.ids.join(",")} overlaps ${a.overlaps.join(",") || "none"} clipped ${a.clipped.join(",") || "none"}`);
    await page.screenshot({ path: `${OUT}/mobile-predict-${t}.png`, fullPage: true });
  }
  const sw2 = await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1);
  check("390px: no horizontal page scroll with a forecast shown", sw2);
  {
    const g = await page.evaluate(() => { const r = (id) => document.querySelector(`[data-testid="${id}"]`).getBoundingClientRect(); const c = r("chart-card"), f = r("forecast-panel"); return { below: f.top >= c.bottom - 1, fits: f.right <= window.innerWidth + 1 && f.left >= -1, ids: ["strip-price", "strip-change", "strip-target", "strip-range", "strip-model", "strip-ref"].every((id) => r(id).width > 0 && r(id).right <= window.innerWidth + 1) }; });
    check("390px: the forecast panel stacks under the chart and its price, change, target, range, model and inputs all fit the screen", g.below && g.fits && g.ids, JSON.stringify(g));
  }
  await page.click('[data-testid="nav-alerts"]'); await page.waitForTimeout(1500);
  check("390px: Alerts & settings page has no horizontal scroll", await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1));
  await page.screenshot({ path: `${OUT}/mobile-alerts.png`, fullPage: true });
  check("no console errors on mobile", errors.length === 0 && bad.length === 0, [...errors, ...bad].slice(0, 3).join(" | "));
  await ctx.close();
}

await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
