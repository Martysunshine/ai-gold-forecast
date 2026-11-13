// Browser end-to-end checks. Run through e2e/run.sh (it starts the three servers this script expects).
import { chromium } from "playwright-core";
import fs from "node:fs";

const { LIVE, CACHED, EMPTY, OUT } = process.env;
fs.mkdirSync(OUT, { recursive: true });
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: !!ok }); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`); };
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: true });

async function open(viewport, url, setup) {
  const ctx = await browser.newContext({ viewport, acceptDownloads: true });
  const page = await ctx.newPage();
  const errors = [], bad = [], predictCalls = [], predictBodies = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("response", async (r) => {
    const u = r.url();
    if (u.includes("/api/") && r.status() >= 400) bad.push(`${r.status()} ${u.replace(url, "")}`);
    if (u.includes("/api/predict?")) { try { predictBodies.push(await r.json()); } catch {} }
  });
  page.on("request", (r) => { if (r.url().includes("/api/predict?")) predictCalls.push(r.url()); });
  if (setup) await setup(page);
  await page.goto(url);
  return { ctx, page, errors, bad, predictCalls, predictBodies };
}
const money = (s) => parseFloat(s.replace(/[^0-9.\-−]/g, "").replace("−", "-"));
const txt = async (page, id) => (await page.textContent(`[data-testid="${id}"]`)).trim();
const runForecast = async (page, h) => {
  await page.click(`[data-testid="h-${h}"]`);
  await page.click('[data-testid="run"]');
  await page.waitForSelector('[data-testid="result"]', { timeout: 60000 });
};
const lum = (hex) => { const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)); return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]; };
const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };

// ============================== 1440 desktop: full workflow on live data ==============================
let originalPrice;
{
  const { ctx, page, errors, bad, predictCalls, predictBodies } = await open({ width: 1440, height: 900 }, LIVE);
  await page.waitForSelector('[data-testid="latest-price"]', { timeout: 30000 });
  originalPrice = money(await txt(page, "latest-price"));
  check("1440: top bar has wordmark, GC=F / COMEX, status and refresh time", /Gold Outlook/.test(await page.textContent(".wordmark")) && /GC=F \/ COMEX/.test(await page.textContent(".sym")) && /Live/.test(await txt(page, "data-status")) && /\d{2}:\d{2}/.test(await txt(page, "refresh-time")), await txt(page, "refresh-time"));
  check("1440: market strip shows close, change, observation date, realised vol",
    originalPrice > 500 && /[▲▼]/.test(await txt(page, "session-change")) && /20\d\d/.test(await txt(page, "obs-date")) && /%/.test(await txt(page, "realised-vol")),
    `${originalPrice} | ${await txt(page, "session-change")} | ${await txt(page, "obs-date")} | vol ${await txt(page, "realised-vol")}`);
  // metric definitions via tooltip (hover and keyboard focus)
  const infos = page.locator(".strip .info");
  check("1440: every strip metric has a definition tooltip", (await infos.count()) === 4);
  await infos.nth(3).hover();
  const tipText = await infos.nth(3).locator(".info-tip").innerText();
  check("1440: tooltip appears on hover and defines the metric", /annualised/i.test(tipText) && (await infos.nth(3).locator(".info-tip").isVisible()), tipText.slice(0, 70));
  await page.mouse.move(5, 5);
  await infos.nth(0).focus();
  check("1440: tooltip appears on keyboard focus", await infos.nth(0).locator(".info-tip").isVisible());

  // chart crosshair readout
  const box = await page.locator('[data-testid="price-chart"] .recharts-wrapper').boundingBox();
  const before = await txt(page, "readout");
  await page.mouse.move(box.x + box.width * 0.4, box.y + box.height * 0.5);
  await page.waitForTimeout(250);
  const during = await txt(page, "readout");
  check("1440: chart crosshair updates the readout on hover", before !== during && !/latest close/.test(during), `${before.slice(0, 40)} → ${during.slice(0, 40)}`);
  check("1440: crosshair lines drawn", (await page.locator('[data-testid="price-chart"] .recharts-reference-line').count()) >= 2);
  await page.screenshot({ path: `${OUT}/01-before-forecast-1440.png`, fullPage: true });
  check("1440: export disabled before any forecast", (await page.locator('[data-testid="download-disabled"]').count()) === 1 && (await page.locator('[data-testid="download"]').count()) === 0);
  check("1440: no forecast result before Run forecast", (await page.locator('[data-testid="result"]').count()) === 0 && /No result/.test(await txt(page, "match")));

  // all three horizons
  const seen = {};
  for (const h of [1, 5, 20]) {
    await runForecast(page, h);
    const price = money(await txt(page, "pred-price")), change = money(await txt(page, "pred-change")), target = await txt(page, "pred-target");
    const range = (await txt(page, "pred-range")).split("–").map(money);
    seen[h] = { price, change, target, range };
    check(`1440: horizon ${h} — price, change, range, target, model displayed and consistent`,
      price > 500 && range[0] < price && price < range[1] && Math.abs((price / originalPrice - 1) * 100 - change) < 0.02 && /20\d\d/.test(target) && /Ridge|Forest/.test(await txt(page, "pred-model")),
      `$${price} ${change}% [${range}] ${target}`);
    check(`1440: horizon ${h} — match indicator says result matches configuration`, new RegExp(`Matches configuration: ${h} session`).test(await txt(page, "match")));
  }
  const apiBodies = Object.fromEntries(predictBodies.map((b) => [b.horizon, b]));
  check("1440: displayed forecasts equal the API values (3 horizons)", [1, 5, 20].every((h) => Math.abs(apiBodies[h].forecast.price - seen[h].price) < 0.006 && apiBodies[h].target_date.slice(8) === String(parseInt(seen[h].target)).padStart(2, "0")));
  check("1440: target dates are increasing weekdays", new Date(apiBodies[1].target_date) < new Date(apiBodies[5].target_date) && new Date(apiBodies[5].target_date) < new Date(apiBodies[20].target_date) && [1, 5, 20].every((h) => ![0, 6].includes(new Date(apiBodies[h].target_date + "T00:00:00Z").getUTCDay())));
  if (apiBodies[5].base_date === "2026-11-12") check("1440: calendar — 20th session after 12 Nov 2026 is 11 Dec (Thanksgiving skipped)", apiBodies[20].target_date === "2026-12-11" && apiBodies[5].target_date === "2026-11-19");

  // stale protection: switch horizon after running 20 -> no result shown for 5? (5 was run) so use a fresh page state check via 1 -> still shown for own horizon
  await page.click('[data-testid="h-5"]');
  check("1440: switching horizon shows that horizon's own result, never another's", money(await txt(page, "pred-price")) === seen[5].price);
  await page.screenshot({ path: `${OUT}/02-forecast-1440.png`, fullPage: true });

  // cosmetic interactions do not call the model
  const callsBefore = predictCalls.length;
  for (const r of ["3M", "1Y", "5Y", "6M"]) await page.getByRole("tab", { name: r, exact: true }).click();
  for (const t of ["context", "method", "backtest"]) await page.click(`[data-testid="tab-${t}"]`);
  await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.4);
  check("1440: range buttons, tabs and hover trigger no model requests", predictCalls.length === callsBefore, `${predictCalls.length - callsBefore} extra`);
  const nBefore = predictBodies.length;
  await page.click('[data-testid="run"]'); await page.waitForSelector('[data-testid="result"]');
  await page.waitForFunction((n) => true, nBefore);
  const again = predictBodies[predictBodies.length - 1];
  check("1440: re-running an unchanged configuration is served from the server cache (no retraining)", again.served_from_cache === true && predictBodies.length === nBefore + 1);

  // backtest content
  await page.click('[data-testid="tab-backtest"]');
  const bt = await txt(page, "backtest");
  check("1440: backtest shows evaluation dates, MAE, coverage, model/baseline table", /Train/.test(bt) && /Validation/.test(bt) && /Test/.test(bt) && /Test MAE/i.test(bt) && /Range coverage/i.test(bt) && (await page.locator('[data-testid="comparison"] tbody tr').count()) === 3 && (await page.locator('[data-testid="backtest"] svg.recharts-surface').count()) >= 1);
  await page.click('[data-testid="open-backtest"]');
  check("1440: 'Open backtest' link works", (await page.getAttribute('[data-testid="tab-backtest"]', "aria-selected")) === "true");

  // export equals displayed values
  await runForecast(page, 20);
  const shown = { price: await txt(page, "pred-price"), change: await txt(page, "pred-change"), range: await txt(page, "pred-range"), target: await txt(page, "pred-target"), model: await txt(page, "pred-model") };
  const [dl] = await Promise.all([page.waitForEvent("download"), page.click('[data-testid="download"]')]);
  const csvPath = `${OUT}/export.csv`; await dl.saveAs(csvPath);
  const [head, line] = fs.readFileSync(csvPath, "utf8").trim().split("\n");
  const cols = head.split(","), vals = line.match(/("([^"]|"")*"|[^,]*)(,|$)/g).map((v) => v.replace(/,$/, "").replace(/^"|"$/g, ""));
  const row = Object.fromEntries(cols.map((c, i) => [c, vals[i]]));
  const fmtUsd = (n) => "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  check("export: CSV values equal the displayed forecast exactly (price, change, range, model, horizon)",
    fmtUsd(row.forecast_price_usd) === shown.price && fmtUsd(row.target_range_low_usd) + " – " + fmtUsd(row.target_range_high_usd) === shown.range && Math.abs(Number(row.projected_change_pct) - money(shown.change)) < 1e-9 && row.model === shown.model && row.horizon_sessions === "20",
    `${row.forecast_price_usd} ${row.projected_change_pct}% ${row.target_range_low_usd}-${row.target_range_high_usd} ${row.target_session_date}`);

  // market context
  await page.click('[data-testid="tab-context"]');
  await page.waitForSelector('[data-testid="news"]', { timeout: 30000 });
  const links = await page.$$eval('[data-testid="news"] a.news-row', (a) => a.map((x) => [x.href, x.textContent]));
  check("context: headline rows have publisher, timestamp and source link; states news is not a model input", links.length >= 5 && /\d{2}:\d{2}/.test(links[0][1]) && /not.*input to the numerical forecast/i.test(await txt(page, "news-note")), `${links.length} rows`);
  await page.click('[data-testid="tab-method"]');
  const heads = await page.locator('[data-testid="methodology"] h3').allTextContents();
  check("methodology: data, features, evaluation, calendar, uncertainty sections", JSON.stringify(heads) === JSON.stringify(["Data", "Features and target", "Evaluation", "Calendar", "Uncertainty"]), heads.join(", "));
  await page.screenshot({ path: `${OUT}/03-methodology-1440.png` });

  // refresh keeps valid results when the data did not change
  await page.click('[data-testid="refresh"]');
  await page.waitForFunction(() => !document.querySelector('[data-testid="refresh"]')?.disabled, null, { timeout: 60000 });
  check("refresh: completes and results for unchanged data stay valid (not retrained, not cleared)", /Live/.test(await txt(page, "data-status")) && (await page.locator('[data-testid="result"]').count()) === 1);

  // CSV upload -> results must not carry over; return to live data
  const api = await (await fetch(`${LIVE}/api/prices`)).json();
  const rows = api.prices.slice(-700).map((r, i) => `${r.date},${(r.close * (1 + 0.002 * Math.sin(i))).toFixed(2)}`); // genuine, distinct series
  fs.writeFileSync(`${OUT}/upload-good.csv`, "Date,Close\n" + rows.join("\n"));
  await page.setInputFiles('[data-testid="file-input"]', `${OUT}/upload-good.csv`);
  await page.waitForFunction(() => /Uploaded/.test(document.querySelector('[data-testid="data-status"]')?.textContent ?? ""), null, { timeout: 30000 });
  const upPrice = money(await txt(page, "latest-price"));
  check("upload: valid CSV becomes the active dataset; previous results are not shown for it", (await page.locator('[data-testid="result"]').count()) === 0 && /No result/.test(await txt(page, "match")) && (await page.locator('[data-testid="use-live"]').count()) === 1, `price ${upPrice}`);
  await runForecast(page, 5);
  check("upload: forecast runs on uploaded data and is tied to it", /Matches configuration/.test(await txt(page, "match")));
  fs.writeFileSync(`${OUT}/upload-bad.csv`, "Date,Close\n2024-01-01,-5\n2024-01-02,abc\n");
  await page.setInputFiles('[data-testid="file-input"]', `${OUT}/upload-bad.csv`);
  await page.waitForFunction(() => /rejected/i.test(document.querySelector('[data-testid="toast"]')?.textContent ?? ""), null, { timeout: 15000 });
  check("upload: invalid CSV is rejected and the active dataset, price and result are unchanged", money(await txt(page, "latest-price")) === upPrice && /Uploaded/.test(await txt(page, "data-status")) && (await page.locator('[data-testid="result"]').count()) === 1, (await txt(page, "toast")).slice(0, 80));
  await page.click('[data-testid="use-live"]');
  await page.waitForFunction(() => /Live/.test(document.querySelector('[data-testid="data-status"]')?.textContent ?? ""), null, { timeout: 60000 });
  check("upload: 'Use live data' returns to provider data (original price restored)", money(await txt(page, "latest-price")) === originalPrice && (await page.locator('[data-testid="refresh"]').count()) === 1);
  const unexpected = errors.filter((e) => !/422/.test(e));
  check("1440: no unexpected console errors", unexpected.length === 0, unexpected.join(" | ").slice(0, 200));
  check("1440: no unexpected API failures", bad.filter((b) => !b.startsWith("422 /api/prices/upload")).length === 0, bad.join(", "));
  await ctx.close();
}

// ============================== keyboard + news failure ==============================
{
  const { ctx, page, errors } = await open({ width: 1440, height: 900 }, LIVE, async (p) => { await p.route("**/api/news*", (r) => r.abort()); });
  await page.waitForSelector('[data-testid="latest-price"]', { timeout: 30000 });
  // keyboard only: tab to the Run button and activate it
  let reached = false;
  for (let i = 0; i < 40; i++) { await page.keyboard.press("Tab"); if (await page.evaluate(() => document.activeElement?.getAttribute("data-testid") === "run")) { reached = true; break; } }
  const outline = await page.evaluate(() => getComputedStyle(document.activeElement).outlineStyle + " " + getComputedStyle(document.activeElement).outlineWidth);
  check("keyboard: Run forecast reachable with Tab and shows a visible focus ring", reached && /solid 2px/.test(outline), outline);
  await page.keyboard.press("Enter");
  await page.waitForSelector('[data-testid="result"]', { timeout: 60000 });
  check("keyboard: Enter on Run forecast produces a result", true);
  await page.focus('[data-testid="tab-backtest"]'); await page.keyboard.press("ArrowRight");
  check("keyboard: arrow keys move between research tabs", (await page.getAttribute('[data-testid="tab-context"]', "aria-selected")) === "true");
  await page.waitForSelector(".error-block", { timeout: 10000 });
  check("news outage: Market Context shows an honest error and the forecast workflow is unaffected", /Headlines unavailable/.test(await page.textContent(".error-block")) && (await page.locator('[data-testid="result"]').count()) === 1);
  await ctx.close();
}

// ============================== provider failure: cached data retained, age shown ==============================
{
  const { ctx, page, errors } = await open({ width: 1440, height: 900 }, CACHED);
  await page.waitForSelector('[data-testid="latest-price"]', { timeout: 30000 });
  const price = money(await txt(page, "latest-price"));
  check("provider failure: last valid data retained and flagged cached, with its age", /Cached/.test(await txt(page, "data-status")) && /last successful refresh/.test(await txt(page, "refresh-time")) && /Live refresh failed/.test(await page.textContent("main")), await txt(page, "refresh-time"));
  await page.click('[data-testid="refresh"]');
  await page.waitForFunction(() => !document.querySelector('[data-testid="refresh"]')?.disabled);
  check("provider failure: manual refresh keeps the same data and tells the user", money(await txt(page, "latest-price")) === price && /failed/i.test(await txt(page, "toast")));
  await runForecast(page, 5);
  check("provider failure: forecasting still works on cached data", true);
  await page.screenshot({ path: `${OUT}/04-cached-1440.png` });
  await ctx.close();
}
{
  const { ctx, page } = await open({ width: 1440, height: 800 }, EMPTY);
  await page.waitForSelector("#empty-title", { timeout: 30000 });
  check("no data: empty state, nothing fabricated", /No price data loaded/.test(await page.textContent("main")) && (await page.locator('[data-testid="latest-price"]').count()) === 0);
  fs.writeFileSync(`${OUT}/upload-good2.csv`, fs.readFileSync(`${OUT}/upload-good.csv`));
  await page.setInputFiles('[data-testid="file-input"]', `${OUT}/upload-good2.csv`);
  await page.waitForSelector('[data-testid="latest-price"]', { timeout: 30000 });
  check("no data: CSV upload recovers from the empty state", /Uploaded/.test(await txt(page, "data-status")));
  await ctx.close();
}


// ============================== GLD comparison (optional Alpaca view) ==============================
{
  // Real server, no keys configured: guidance, not an error; opening the tab never touches the model.
  const { ctx, page, errors, bad, predictCalls } = await open({ width: 1440, height: 900 }, LIVE);
  await page.waitForSelector('[data-testid="latest-price"]', { timeout: 30000 });
  const gldCalls = [];
  page.on("request", (r) => { if (r.url().includes("/api/compare/gld")) gldCalls.push(r.url()); });
  check("gld: no Alpaca request on page load (tab is lazy)", gldCalls.length === 0);
  await page.click('[data-testid="tab-gld"]');
  await page.waitForSelector('[data-testid="gld-setup"]', { timeout: 15000 });
  const setup = await page.textContent('[data-testid="gld-setup"]');
  check("gld (no keys): setup guidance shown, labelled not a model input", /ALPACA_API_KEY_ID/.test(setup) && /not.*used by the forecast/i.test(await page.textContent('[data-testid="gld-note"]')) && gldCalls.length === 1);
  check("gld (no keys): no model requests, console errors or API failures", predictCalls.length === 0 && errors.length === 0 && bad.length === 0, [...errors, ...bad].join(" | "));
  await ctx.close();
}
for (const [w, h] of [[1440, 900], [390, 844]]) {
  // Configured state rendered from a MOCKED /api/compare/gld response (no real keys in this test); GLD = 0.092 x GC=F with noise.
  const prices = await (await fetch(`${LIVE}/api/prices`)).json();
  const pts = prices.prices.slice(-1000);
  let seed = 7; const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647 - 0.5) * 0.004;
  const gld = pts.map((p) => p.close * 0.092 * (1 + rnd()));
  const indexed = pts.map((p, i) => ({ date: p.date, gc: +(p.close / pts[0].close * 100).toFixed(3), gld: +(gld[i] / gld[0] * 100).toFixed(3) }));
  const mock = { configured: true, note: "GLD is an exchange-traded fund, not the futures contract. Shown for comparison only; not used by the forecast.",
    instrument: { name: "SPDR Gold Shares (GLD), NYSE Arca", kind: "etf", unit: "USD per share", note: "" }, source: "Alpaca market data (iex feed)",
    fetched_at: new Date().toISOString(), age_minutes: 0, provider_error: null, status: { mode: "live", label: "Live" },
    latest: { date: pts.at(-1).date, close: gld.at(-1), change_pct: (gld.at(-1) / gld.at(-2) - 1) * 100 }, count: pts.length, gold_source: prices.source,
    comparison: { common_dates: pts.length, start: pts[0].date, end: pts.at(-1).date, indexed,
      window: { sessions: 252, start: pts.at(-253).date, end: pts.at(-1).date, return_correlation: 0.97, gc_return_pct: 4.2, gld_return_pct: 3.9 } } };
  const { ctx, page, errors } = await open({ width: w, height: h }, LIVE, async (p) => { await p.route("**/api/compare/gld*", (r) => r.fulfill({ json: mock })); });
  await page.waitForSelector('[data-testid="latest-price"]', { timeout: 30000 });
  await page.click('[data-testid="tab-gld"]');
  await page.waitForSelector('[data-testid="gld"] svg.recharts-surface', { timeout: 15000 });
  await page.waitForTimeout(400);
  const ov = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
  check(`gld ${w} (mocked API): close, correlation and indexed chart render; no overflow`,
    /\$/.test(await txt(page, "gld-close")) && (await txt(page, "gld-corr")) === "0.97" && ov[0] <= ov[1] && errors.length === 0, `overflow ${ov}`);
  if (w === 1440) await page.screenshot({ path: `${OUT}/gld-1440-mocked.png` });
  await ctx.close();
}
// ============================== 768 and 390: loaded + forecast, layout, touch targets ==============================
for (const [w, h] of [[768, 1024], [390, 844]]) {
  const { ctx, page, errors, bad } = await open({ width: w, height: h }, LIVE);
  await page.waitForSelector('[data-testid="latest-price"]', { timeout: 30000 });
  await page.waitForSelector('[data-testid="price-chart"] svg.recharts-surface');
  await runForecast(page, 20);
  await page.click('[data-testid="tab-backtest"]'); await page.waitForSelector('[data-testid="backtest"] svg.recharts-surface'); await page.waitForTimeout(500);
  const ov = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: window.innerWidth }));
  check(`${w}: no page-level horizontal overflow`, ov.sw <= ov.iw, `${ov.sw} vs ${ov.iw}`);
  const stacked = await page.evaluate(() => { const a = document.querySelector(".ws-main").getBoundingClientRect(), b = document.querySelector(".side").getBoundingClientRect(); return b.top >= a.bottom - 1; });
  check(`${w}: chart and forecast panel ${w < 1100 ? "stack" : "sit side by side"}`, stacked === (w < 1100));
  const small = await page.evaluate(() => [...document.querySelectorAll('button:not([hidden]), a.btn, [role="tab"], [role="radio"]')].filter((e) => e.offsetParent && !e.classList.contains("x")).map((e) => [e.textContent.trim().slice(0, 18), Math.round(e.getBoundingClientRect().height)]).filter(([, hh]) => hh < 44));
  check(`${w}: interactive controls are touch-sized (>=44px)`, w > 900 || small.length === 0, small.slice(0, 4).join(" | "));
  const chartH = await page.evaluate(() => Math.round(document.querySelector('[data-testid="price-chart"] .chart-box').getBoundingClientRect().height));
  check(`${w}: chart has a real height (no collapsed or oversized chart)`, chartH >= 300 && chartH <= 700, `${chartH}px`);
  const fit = await page.evaluate(() => { const e = document.querySelector('[data-testid="comparison"]'); return [e.scrollWidth, e.parentElement.clientWidth]; });
  check(`${w}: model comparison table fits without horizontal scrolling`, fit[0] <= fit[1] + 1, `${fit[0]} vs ${fit[1]}`);
  const clipped = await page.evaluate(() => [...document.querySelectorAll(".recharts-xAxis .recharts-cartesian-axis-tick-value")].some((t) => { const r = t.getBoundingClientRect(), c = document.querySelector(".chart-box").getBoundingClientRect(); return r.left < c.left - 1 || r.right > c.right + 1; }));
  check(`${w}: no clipped axis labels`, !clipped);
  await page.screenshot({ path: `${OUT}/forecast-${w}.png`, fullPage: true });
  check(`${w}: no console errors / API failures`, errors.length === 0 && bad.length === 0, [...errors, ...bad].join(" | ").slice(0, 160));
  await ctx.close();
}

// ============================== contrast (design tokens) ==============================
{
  const bg = "#080A0C", surf = "#11151A";
  const pairs = [["muted on surface", "#929CAA", surf], ["text on surface", "#E9EDF2", surf], ["blue on surface", "#66ACFF", surf], ["gold on surface", "#D6B878", surf], ["green on surface", "#4FA77B", surf], ["red on surface", "#D2685F", surf], ["primary button text", "#04101C", "#66ACFF"], ["credits text on bg", "#7C8795", bg]];
  const low = pairs.filter(([, f, b]) => ratio(f, b) < 4.5).map(([n, f, b]) => `${n} ${ratio(f, b).toFixed(2)}`);
  check("contrast: all text/background pairs >= 4.5:1", low.length === 0, low.length ? low.join("; ") : pairs.map(([n, f, b]) => `${ratio(f, b).toFixed(1)}`).join(" "));
}

await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
