// Retakes the README screenshots from a running app (dark theme, New York time).
//   Windows:  set LIVE=http://127.0.0.1:8742 && set OUT=docs\screenshots && set FRAMES=%TEMP%\gold-frames && node e2e\shots.mjs
//   macOS:    LIVE=http://127.0.0.1:8742 OUT=docs/screenshots FRAMES=/tmp/gold-frames node e2e/shots.mjs
// FRAMES is optional: the frames of the demo (docs/screenshots/forecast-demo.gif is built from them).
// The Learning page's worker panel names the computer, so it is never photographed whole.
import { chromium } from "playwright-core";
import fs from "node:fs";

const URL = process.env.LIVE || "http://127.0.0.1:8742";
const { OUT, FRAMES } = process.env;
if (!OUT) throw new Error("set OUT to the folder for the screenshots");
fs.mkdirSync(OUT, { recursive: true });
const CHROME = process.env.CHROME_PATH || (process.platform === "win32" ? "C:/Program Files/Google/Chrome/Application/chrome.exe" : "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome");
const browser = await chromium.launch({ executablePath: CHROME, headless: true });
const look = { colorScheme: "dark", timezoneId: "America/New_York", locale: "en-US" };

const open = async (ctx, hash, id) => {
  const page = await ctx.newPage();
  await page.goto(URL + hash);
  await page.waitForSelector(`[data-testid="${id}"]`, { timeout: 30000 });
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(2500);
  return page;
};
const predict = async (page, target) => {
  await page.click(`[data-testid="target-${target}"]`);
  await page.click('[data-testid="predict"]');
  await page.waitForSelector('[data-testid="result-strip"]:not([data-state="none"])', { timeout: 90000 });
  await page.waitForTimeout(1200);
};
// The union of some elements, with a margin of page background so rounded corners are not cut.
const clipOf = async (page, selectors, pad = 14) => {
  const boxes = [];
  for (const s of selectors) {
    const el = page.locator(s).first();
    await el.scrollIntoViewIfNeeded();
    const b = await el.boundingBox(), sc = await page.evaluate(() => ({ x: scrollX, y: scrollY }));
    boxes.push({ x: b.x + sc.x, y: b.y + sc.y, r: b.x + sc.x + b.width, b: b.y + sc.y + b.height });
  }
  const width = page.viewportSize().width;
  const x = Math.max(0, Math.min(...boxes.map((b) => b.x)) - pad), y = Math.max(0, Math.min(...boxes.map((b) => b.y)) - pad);
  return { x, y, width: Math.min(width - x, Math.max(...boxes.map((b) => b.r)) + pad - x), height: Math.max(...boxes.map((b) => b.b)) + pad - y };
};
const shot = async (page, selectors, path, pad) => page.screenshot({ path, fullPage: true, clip: await clipOf(page, selectors, pad) });

// Desktop: the forecast, the backtest, the methodology and two parts of the Learning page.
const ctx = await browser.newContext({ viewport: { width: 1360, height: 900 }, deviceScaleFactor: 1.5, ...look });
let page = await open(ctx, "/#/forecast", "page-forecast");
await predict(page, 5);
const cc = await clipOf(page, ['[data-testid="chart-card"]'], 18);
await page.screenshot({ path: `${OUT}/app.png`, fullPage: true, clip: { x: 0, y: 0, width: 1360, height: Math.ceil(cc.y + cc.height) } });
await page.click('[data-testid="dtab-backtest"]');
await page.waitForSelector('[data-testid="backtest"]');
await page.waitForTimeout(1500);
await shot(page, ['[data-testid="details-panel"]'], `${OUT}/backtest.png`);
await page.click('[data-testid="dtab-method"]');
await page.waitForTimeout(1500);
await shot(page, ['[data-testid="details-panel"]'], `${OUT}/methodology.png`);
await page.close();

page = await open(ctx, "/#/learning", "page-learning");
await page.click('[data-testid="sel-fwd_1"]');
await page.waitForTimeout(2000);
await shot(page, ['[data-testid="lr-horizons"]'], `${OUT}/learning-horizons.png`);
await shot(page, ['[data-testid="lr-pva"] >> xpath=..'], `${OUT}/learning-predicted-vs-actual.png`, 4);
await ctx.close();

// Phone: the forecast page, stacked.
const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, ...look });
const m = await open(phone, "/#/forecast", "page-forecast");
await predict(m, 20);
const rs = await clipOf(m, ['[data-testid="result-strip"]'], 16);
await m.screenshot({ path: `${OUT}/mobile.png`, fullPage: true, clip: { x: 0, y: 0, width: 390, height: Math.ceil(rs.y + rs.height) } });
await phone.close();

if (FRAMES) {
  // Each horizon chosen and forecast, then the backtest, then the Learning page's predicted-vs-actual chart.
  fs.mkdirSync(FRAMES, { recursive: true });
  const c = await browser.newContext({ viewport: { width: 1200, height: 800 }, ...look });
  const p = await open(c, "/#/forecast", "page-forecast");
  let n = 0;
  const frame = async () => { await p.waitForTimeout(300); await p.screenshot({ path: `${FRAMES}/${String(n++).padStart(2, "0")}.png` }); };
  await frame();
  for (const t of ["next", 1, 5, 20]) {
    await p.click(`[data-testid="target-${t}"]`);
    await frame();
    await p.click('[data-testid="predict"]');
    await p.waitForSelector('[data-testid="result-strip"]:not([data-state="none"])', { timeout: 90000 });
    await p.waitForTimeout(900);
    await frame();
  }
  await p.click('[data-testid="open-details"]');
  await p.waitForSelector('[data-testid="backtest"]');
  await p.waitForTimeout(1500);
  await frame();
  await p.goto(URL + "/#/learning");
  await p.waitForSelector('[data-testid="lr-pva"]');
  await p.waitForTimeout(1500);
  await p.locator('[data-testid="lr-pva"]').scrollIntoViewIfNeeded();
  await p.evaluate(() => scrollBy(0, -120));
  await frame();
  await c.close();
  console.log(`saved ${n} demo frames in ${FRAMES}`);
}
await browser.close();
console.log(`saved the screenshots in ${OUT}`);
