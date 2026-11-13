// Retakes the README and docs screenshots from the running app, in the light and the dark theme.
//   macOS / Linux:  SHOTS=./docs/screenshots ./e2e/run.sh shots.mjs
//   Windows:        set SHOTS=docs\screenshots  &&  e2e\run.cmd -Script shots.mjs
// Set FRAMES to a folder to also save the frames of the forecast demo (docs/screenshots/forecast-demo.gif is built from them).
import { chromium } from "playwright-core";
import fs from "node:fs";

const { LIVE, OUT, FRAMES } = process.env;
fs.mkdirSync(OUT, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: true });

const ready = async (page) => {
  await page.goto(LIVE);
  await page.waitForSelector('[data-testid="latest-price"]', { timeout: 30000 });
  await page.waitForSelector('[data-testid="price-chart"] svg.recharts-surface');
  await page.evaluate(() => document.fonts.ready);
};
const runForecast = async (page, h) => {
  await page.click(`[data-testid="h-${h}"]`);
  await page.click('[data-testid="run"]');
  await page.waitForSelector('[data-testid="result"]', { timeout: 60000 });
  await page.waitForTimeout(350);
};
// A card with a margin of page background around it, so the rounded corners are not cut.
const card = async (page, selector, path, pad = 14) => {
  const el = page.locator(selector);
  await el.scrollIntoViewIfNeeded();
  const b = await el.boundingBox(), scroll = await page.evaluate(() => ({ x: scrollX, y: scrollY })), width = page.viewportSize().width;
  const x = Math.max(0, b.x + scroll.x - pad);
  await page.screenshot({ path, fullPage: true, clip: { x, y: Math.max(0, b.y + scroll.y - pad), width: Math.min(width - x, b.width + 2 * pad), height: b.height + 2 * pad } });
};

for (const scheme of ["light", "dark"]) {
  // Desktop: the workspace, the backtest and the stage-by-stage walk-through of a 5-session forecast.
  const ctx = await browser.newContext({ viewport: { width: 1360, height: 900 }, deviceScaleFactor: 1.5, colorScheme: scheme });
  const page = await ctx.newPage();
  await ready(page);
  await runForecast(page, 5);
  const ws = await page.locator(".workspace").boundingBox();
  await page.screenshot({ path: `${OUT}/app-${scheme}.png`, fullPage: true, clip: { x: 0, y: 0, width: 1360, height: Math.ceil(ws.y + ws.height + 18) } });
  await page.click('[data-testid="tab-backtest"]');
  await page.waitForSelector('[data-testid="backtest"] svg.recharts-surface');
  await page.waitForTimeout(350);
  await card(page, ".research", `${OUT}/backtest-${scheme}.png`);
  await page.click('[data-testid="tab-method"]');
  await page.waitForSelector('[data-testid="method-verdict"]');
  await card(page, ".research", `${OUT}/walkthrough-${scheme}.png`);
  await ctx.close();

  // Phone: the same workspace, stacked.
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, colorScheme: scheme });
  const mobile = await phone.newPage();
  await ready(mobile);
  await runForecast(mobile, 20);
  const side = await mobile.locator(".side").boundingBox(), top = await mobile.evaluate(() => scrollY);
  await mobile.screenshot({ path: `${OUT}/mobile-${scheme}.png`, fullPage: true, clip: { x: 0, y: 0, width: 390, height: Math.ceil(side.y + top + side.height + 16) } });
  await phone.close();
  console.log(`saved ${scheme}: app, backtest, walkthrough, mobile`);
}

if (FRAMES) {
  // Frames of the forecast demo: each horizon selected and run, then the backtest opened.
  fs.mkdirSync(FRAMES, { recursive: true });
  const ctx = await browser.newContext({ viewport: { width: 1200, height: 800 }, colorScheme: "light" });
  const page = await ctx.newPage();
  await ready(page);
  let n = 0;
  const frame = async () => { await page.waitForTimeout(250); await page.screenshot({ path: `${FRAMES}/${String(n++).padStart(2, "0")}.png` }); };
  await frame();
  for (const h of [1, 5, 20]) {
    await page.click(`[data-testid="h-${h}"]`);
    await frame();
    await page.click('[data-testid="run"]');
    await page.waitForSelector('[data-testid="result"]', { timeout: 60000 });
    await frame();
  }
  await page.click('[data-testid="open-backtest"]');
  await page.waitForSelector('[data-testid="backtest"] svg.recharts-surface');
  await page.waitForTimeout(900);
  await frame();
  await ctx.close();
  console.log(`saved ${n} demo frames`);
}

await browser.close();
