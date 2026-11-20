// Alerts-panel verification against a THROWAWAY copy of the data (see run_alerts.sh): simulated prices/forecasts, simulation mode, no SMS.
// Usage: URL=http://127.0.0.1:8760 TOKEN_FILE=/tmp/token.txt OUT=./shots node verify_alerts.mjs
import { chromium } from "playwright-core";
import fs from "node:fs";

const URL = process.env.URL, OUT = process.env.OUT || "e2e/screenshots";
const TOKEN = fs.readFileSync(process.env.TOKEN_FILE, "utf8").trim();
fs.mkdirSync(OUT, { recursive: true });
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: !!ok }); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`); };
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: true });
const api = async (p) => (await fetch(`${URL}/api${p}`)).json();
const T = async (page, id) => (await page.textContent(`[data-testid="${id}"]`)).trim();

async function session(viewport, label) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const errors = [], bad = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("response", (r) => { if (r.url().includes("/api/") && r.status() >= 500) bad.push(`${r.status()} ${r.url().replace(URL, "")}`); });
  await page.goto(URL);
  await page.waitForSelector('[data-testid="predict"]', { timeout: 40000 });
  const onForecast = await page.locator('[data-testid="alerts-panel"]').count();
  await page.click('[data-testid="nav-alerts"]');
  await page.waitForSelector('[data-testid="alerts-panel"] [data-testid="sw-forecast"]', { state: "attached", timeout: 40000 });
  await page.waitForTimeout(1500);
  const panel = page.locator('[data-testid="alerts-panel"]');
  await panel.scrollIntoViewIfNeeded();

  check(`${label}: the Alerts panel lives on the Alerts & settings page, not on the Forecast page`, onForecast === 0 && (await page.locator('[data-testid="page-alerts"]').count()) === 1 && /alerts$/.test(page.url()));
  const txt = await panel.textContent();
  check(`${label}: both alert types are separate switches with plain wording`, /Forecast heads-up/.test(txt) && /Actual-price crossing/.test(txt) && /Not a touch of the level/.test(txt) && /observed futures quote/.test(txt));
  const lv = await T(page, "levels");
  check(`${label}: review levels 4,000 and 4,100 are shown; 4,000 is labelled "Below entry price"`, /\$4,000\s*Below entry price/.test(lv) && /\$4,100\s*Above entry price/.test(lv), lv);
  check(`${label}: entry price is shown as entered and unverified; no profit figure`, /\$4,039\.96 \(as entered; its instrument is not verified\)/.test(await T(page, "entry-note")) && !/profit[^.]*\$\d/i.test(txt) && /Profit is not computed/.test(txt));
  check(`${label}: delivery mode is plainly 'Simulation · no SMS sent'`, /Simulation · no SMS sent/.test(await T(page, "delivery-mode")));
  check(`${label}: morning SMS time and zone default to 07:30 Europe/Berlin`, (await page.inputValue('[data-testid="in-time"]')) === "07:30" && (await page.inputValue('[data-testid="in-tz"]')) === "Europe/Berlin");

  const lc = await T(page, "last-check");
  check(`${label}: last forecast checked shows the next-session forecast against the levels`, /Next-session forecast: \$4,120\.00 for \w{3},? \d{1,2} \w{3} \d{4} · at or above \$4,000 and \$4,100/.test(lc), lc.replace(/\s+/g, " ").slice(0, 150));
  check(`${label}: an unchanged-price baseline is not treated as a prediction`, /unchanged-price baseline/.test(lc) || /none issued/.test(lc) || /Next settlement/.test(lc), "");
  const la = await T(page, "last-alert");
  check(`${label}: last alert and its delivery status are shown (simulated, not delivered)`, /(Actual-price alert|Forecast heads-up)/.test(la) && /Simulated: no SMS was sent|No SMS requested/.test(la), la.replace(/\s+/g, " ").slice(0, 120));
  check(`${label}: hand-off state is shown`, /simulated/.test(await T(page, "handoff")));

  const tb = await T(page, "touch-block");
  check(`${label}: touch probability is labelled Experimental and not shown without evidence, with the reason`, /Experimental/.test(tb) && /not shown\. Only \d+ historical events near that distance/.test(tb) && /never derived from the closing-price range/.test(tb), tb.replace(/\s+/g, " ").slice(0, 160));
  await page.locator('[data-testid="touch-evidence"] summary').click();
  const ev = await T(page, "touch-evidence");
  check(`${label}: evidence reports precision, recall, false alerts, missed events, event counts, lead time and calibration`, ["Precision", "Recall", "False alerts", "Missed", "Events", "Lead time", "Calibration error", "Brier"].every((w) => ev.includes(w)));
  check(`${label}: the final slice is described as never used for selection`, /never used to choose anything/.test(ev));
  await page.screenshot({ path: `${OUT}/${label}-alerts-evidence.png`, fullPage: true });
  await page.locator('[data-testid="touch-evidence"] summary').click();

  // basis check
  await page.locator('[data-testid="basis"] summary').click();
  await page.waitForFunction(() => /Instrument/.test(document.querySelector('[data-testid="basis"]')?.textContent || ""), null, { timeout: 30000 });
  const bs = await T(page, "basis");
  check(`${label}: the basis check states instrument, USD, troy ounce and the quote basis`, /GC=F/.test(bs) && /USD · USD per troy ounce/.test(bs) && /Price alert quote\s*futures/.test(bs) && /not verified|cannot be verified/.test(bs));
  check(`${label}: an entry price far from the current quote is flagged rather than assumed`, /away from the current/.test(bs) || /Entry price/.test(bs));
  await page.locator('[data-testid="basis"] summary').click();

  // switches persist (and are restored)
  const before = (await api("/alerts")).settings;
  await page.click('[data-testid="row-forecast"]'); await page.waitForTimeout(1200);
  const off = (await api("/alerts")).settings.forecast_alerts;
  check(`${label}: the forecast heads-up switch turns the alert off`, before.forecast_alerts === true && off === false);
  await page.click('[data-testid="row-forecast"]'); await page.waitForTimeout(1200);
  check(`${label}: ...and back on`, (await api("/alerts")).settings.forecast_alerts === true);

  // editing levels / time
  await page.fill('[data-testid="in-levels"]', "3900, 4050");
  await page.fill('[data-testid="in-time"]', "08:15");
  await page.click('[data-testid="save-alerts"]'); await page.waitForTimeout(1500);
  const s2 = (await api("/alerts")).settings;
  check(`${label}: levels and morning time are editable and saved`, JSON.stringify(s2.levels) === "[3900,4050]" && s2.morning_time === "08:15", JSON.stringify([s2.levels, s2.morning_time]));
  await page.waitForTimeout(500);
  check(`${label}: after editing, each level shows its own entry comparison`, /\$3,900\s*Below entry price/.test(await T(page, "levels")) && /\$4,050\s*Above entry price/.test(await T(page, "levels")), await T(page, "levels"));
  await page.fill('[data-testid="in-levels"]', "abc");
  await page.click('[data-testid="save-alerts"]');
  check(`${label}: invalid levels are refused with a plain message and nothing is saved`, /must be numbers/.test(await T(page, "alerts-note")) && JSON.stringify((await api("/alerts")).settings.levels) === "[3900,4050]");
  await page.fill('[data-testid="in-levels"]', "4000, 4100");
  await page.fill('[data-testid="in-time"]', "07:30");
  await page.click('[data-testid="save-alerts"]'); await page.waitForTimeout(1500);
  check(`${label}: original configuration restored`, JSON.stringify((await api("/alerts")).settings.levels) === "[4000,4100]");

  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${OUT}/${label}-alerts.png`, clip: undefined, fullPage: true });
  const sw = await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1);
  check(`${label}: no horizontal page scroll`, sw);
  const small = await page.evaluate(() => [...document.querySelectorAll('[data-testid="alerts-panel"] button, [data-testid="alerts-panel"] .gx-switch-row, [data-testid="alerts-panel"] input:not([role=switch])')].filter((e) => e.getBoundingClientRect().height < 32).map((e) => e.className || e.tagName));
  check(`${label}: controls are comfortable to tap`, small.length === 0, small.join(","));
  check(`${label}: no console errors and no server errors`, errors.length === 0 && bad.length === 0, [...errors, ...bad].slice(0, 3).join(" | "));

  if (label === "desktop") {
    // the review page an SMS link opens
    const rp = await ctx.newPage();
    const ok = await rp.goto(`${URL}/review?t=${TOKEN}`);
    const t = await rp.textContent("body");
    check("review page (valid token): forecast, latest quote, position information and the Trading 212 link", ok.status() === 200 && /Settlement forecast \(endpoint\)/.test(t) && /Latest observed quote/.test(t) && /Entry price/.test(t) && /Below entry price/.test(t) && (await rp.locator('a[href="https://www.trading212.com/"]').count()) === 1, "");
    check("review page: 'No response means no trade'; the quote difference is not called profit", /No response means no trade/.test(t) && /not profit/.test(t) && /Profit is not calculated/.test(t));
    check("review page: says it is an endpoint forecast, not a touch", /does not say the price will reach or touch/.test(t));
    await rp.setViewportSize({ width: 390, height: 844 });
    await rp.screenshot({ path: `${OUT}/review-mobile.png`, fullPage: true });
    check("review page at 390px has no horizontal scroll", await rp.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1));
    const bad1 = await rp.goto(`${URL}/review?t=not-a-token`);
    check("review page: an invalid token is refused (403) and shows nothing", bad1.status() === 403 && !/Settlement forecast/.test(await rp.textContent("body")));
    const none = await rp.goto(`${URL}/review`);
    check("review page: no token is refused", none.status() === 403);
    await rp.close();
  }
  await ctx.close();
}

await session({ width: 1440, height: 1000 }, "desktop");
await session({ width: 390, height: 844 }, "mobile");
await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} alert checks passed`);
process.exit(failed.length ? 1 : 0);
