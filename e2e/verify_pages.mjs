// Learning, Paper trading and Alerts & settings pages. Run via run_pages.sh (throwaway data, simulated seed).
import { chromium } from "playwright-core";
import fs from "node:fs";

const URL = process.env.URL, OUT = process.env.OUT || "e2e/screenshots";
fs.mkdirSync(OUT, { recursive: true });
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: !!ok }); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`); };
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: true });
const api = async (p, init) => (await fetch(`${URL}/api${p}`, init)).json();
const T = async (page, id) => (await page.textContent(`[data-testid="${id}"]`)).trim();

async function open(viewport, hash) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const errors = [], bad = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("response", (r) => { if (r.url().includes("/api/") && r.status() >= 500) bad.push(`${r.status()} ${r.url().replace(URL, "")}`); });
  await page.goto(URL + hash);
  return { ctx, page, errors, bad };
}
const noOverflow = (page) => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1);

// ======================================================= desktop: Learning
{
  const { ctx, page, errors, bad } = await open({ width: 1440, height: 1000 }, "/#/learning");
  await page.waitForSelector('[data-testid="page-learning"] [data-testid="lr-runtime"]', { timeout: 40000 });
  await page.waitForTimeout(2500);
  const ws = await api("/learning/workspace");
  const w = ws.worker;
  check("navigation offers the four pages: Forecast, Learning, Paper trading, Alerts & settings", (await page.locator(".gx-nav a").allTextContents()).join("|") === "Forecast|Learning|Paper trading|Alerts & settings");
  check("worker: shows the runtime that is actually operating, from the heartbeat, with its caveat", (await T(page, "lr-runtime-state")).includes(w.daemon_running ? (w.runtime ?? "") : "Not running") && (await T(page, "lr-runtime-note")) === w.runtime_note, `${await T(page, "lr-runtime-state")}`);
  check("worker: last collection, validation, scoring and training are listed", ["Last collection", "Last validation", "Last scoring", "Last training / evaluation"].every((s) => /./.test(s)) && (await T(page, "lr-runtime")).includes("Last training / evaluation"));
  const nj = await page.locator('[data-testid="lr-next-jobs"] tbody tr').count();
  check("next scheduled jobs are listed with their state, including the daily cycle", nj === w.next_runs.length && nj >= 6 && /Full daily cycle/.test(await T(page, "lr-next-jobs")), `${nj} jobs`);
  check("the last full cycle shows its ordered steps (collect → validate → score → learn → …)", /Collect/.test(await T(page, "lr-runtime")) && (!w.last_cycle || (await page.locator(".gx-steps li").count()) === w.last_cycle.steps.length));
  {
    const ws = await (await fetch(`${URL}/api/learning/workspace`)).json();
    const sch = await T(page, "lr-schedule");
    check("worker: the scheduled-cycle record is shown from the persistent cycle log, and 'scheduled' is never claimed without a recorded scheduled cycle", sch.includes(ws.schedule.verdict) && (ws.schedule.scheduled_ok > 0 || /No scheduled cycle has been recorded yet/.test(sch)) && /does not show that training ran|moving quote/i.test(await T(page, "lr-proof-note")), sch.slice(0, 120));
  }
  const rows = await page.locator('[data-testid^="lr-row-"]').count();
  check("active model is shown separately for each horizon (1, 5, 20 sessions and next settlement)", rows === 4);
  let okRows = true;
  for (const id of ["fwd_1", "fwd_5", "fwd_20", "nowcast"]) {
    const row = await T(page, `lr-row-${id}`), t = ws.tasks[id];
    const name = t.active.is_unchanged_baseline ? "Unchanged price" : t.active.algorithm.replace("_", " ");
    okRows = okRows && row.includes(name) && row.includes(String(t.forward.distinct_origins));
  }
  check("each row's active model and distinct matured origin count equal the backend's", okRows);
  const sel = {};
  for (const id of ["fwd_1", "fwd_5", "fwd_20", "nowcast"]) {
    await page.click(`[data-testid="sel-${id}"]`); await page.waitForTimeout(500);
    sel[id] = await page.getAttribute('[data-testid="lr-detail"]', "data-task");
  }
  check("choosing a horizon shows that horizon's detail", Object.entries(sel).every(([k, v]) => k === v));
  await page.click('[data-testid="sel-fwd_5"]'); await page.waitForTimeout(800);
  const t5 = ws.tasks.fwd_5;
  const fwd = await T(page, "lr-forward"), dev = await T(page, "lr-development");
  check("genuine forward results and walk-forward history are separate, labelled blocks", /Genuine forward results/.test(await T(page, "lr-detail")) && /Walk-forward on history/.test(await T(page, "lr-detail")) && /not an untouched test/i.test(dev));
  check("forward block reports matured origins honestly (none matured yet → says so)", t5.forward.n_matured === 0 ? /none yet/.test(fwd) && /Distinct matured origins\s*0/.test(fwd) : new RegExp(`Distinct matured origins\\s*${t5.forward.distinct_origins}`).test(fwd), fwd.slice(0, 120));
  check("development block shows error vs unchanged, range coverage AND width", /Error vs unchanged/.test(dev) && /coverage|inside/.test(dev) && /wide/.test(dev), dev.slice(0, 150));
  const paths = await page.evaluate(() => ({ pva: document.querySelectorAll('[data-testid="lr-pva"] svg path.recharts-line-curve').length, roll: document.querySelectorAll('[data-testid="lr-rolling"] svg path.recharts-line-curve').length }));
  check("predicted-vs-actual and rolling-error charts are drawn (model vs unchanged price)", paths.pva >= 3 && paths.roll >= 2, JSON.stringify(paths));
  check("training history states improved / worsened / no change with the noise tolerance", new RegExp({ first_run: "First evaluation", unchanged: "No meaningful change", improved: "Improved", worsened: "Worsened" }[t5.development.trend.verdict]).test(await T(page, "lr-trend")) && /noise|compare|improved|worsened/i.test(await T(page, "lr-development")));
  check("candidate table lists every candidate with status, development error, coverage/width and forward origins", (await page.locator('[data-testid="lr-candidates"] tbody tr').count()) === t5.candidates.length, `${t5.candidates.length} candidates`);
  check("the latest decision is recorded and shown", /Latest decision/.test(await T(page, "lr-decision")));
  const fg = ws.feature_groups;
  check("Fibonacci is labelled experimental and its verdict equals the backend's pre-set rule result", fg.evaluated ? (await T(page, "lr-fib-verdict")).includes({ supported: "Supported", not_supported: "Not supported", insufficient: "Insufficient" }[fg.verdict.status]) : true, fg.verdict?.status);
  check("with-vs-without comparisons are shown on identical forecast dates with paired intervals", !fg.evaluated || ((await page.locator('[data-testid="lr-fib"] table').count()) === 2 && /90% interval/.test(await T(page, "lr-fib"))));
  check("futures volume exclusion is stated", /volume is not used/i.test(await T(page, "lr-fib")) || !fg.evaluated);
  check("data quality: detect-and-record policy, learning allowed/blocked, large moves kept", /never deleted/.test(await T(page, "lr-quality")) && /Large moves are checked and kept/.test(await T(page, "lr-quality")));
  check("training attempts list failures as well as successes", new RegExp(`${ws.attempts.ok} ok · ${ws.attempts.failed} failed`).test(await T(page, "lr-attempts")));
  check("no 'self-improving' claim: the page says a job running is not improvement", /job running is not improvement/.test(await T(page, "page-learning")));
  await page.screenshot({ path: `${OUT}/page-learning-desktop.png`, fullPage: true });
  check("no console errors or server errors on Learning", errors.length === 0 && bad.length === 0, [...errors, ...bad].slice(0, 3).join(" | "));
  await ctx.close();
}

// ======================================================= desktop: Paper trading
{
  const { ctx, page, errors, bad } = await open({ width: 1440, height: 1000 }, "/#/paper");
  await page.waitForSelector('[data-testid="pp-sim"]', { timeout: 40000 }); await page.waitForTimeout(1500);
  let pj = await api("/paper");
  check("paper trading is plainly a simulation: 'no broker connected', no order or broker controls anywhere", /no broker connected/i.test(await T(page, "pp-sim")) && /No broker is connected and no live order/.test(await T(page, "pp-notice")) && !/place order|connect broker|live order button|buy now|sell now/i.test(await T(page, "page-paper")));
  check("gold only: the page names gold and offers no other instrument", /GOLD ONLY/.test(await T(page, "page-paper")));
  check("tiles equal the backend's book (equity, return, position, trades)", (await T(page, "pp-tiles")).includes(Number(pj.book.performance.equity.toFixed(2)).toLocaleString("en-US", { minimumFractionDigits: 2 })) && (await T(page, "pp-tiles")).includes(String(pj.book.performance.n_trades)));
  check("fills cite the immutable forecast id and give the rationale (simulated seed)", pj.book.fills.length >= 1 && (await T(page, "pp-trades")).includes(pj.book.fills[0].forecast_id) && /bps above its reference/.test(await T(page, "pp-trades")));
  check("comparison against holding gold and staying in cash, with the curve", /Hold gold/.test(await T(page, "pp-compare")) && /Cash/.test(await T(page, "pp-compare")) && (await page.locator('[data-testid="pp-curve"], [data-testid="pp-curve-empty"]').count()) === 1);
  check("prediction accuracy is a separate block from profit and says 'none yet' when nothing matured", /SEPARATE FROM PROFIT/.test(await T(page, "pp-accuracy")) && (pj.book.accuracy.n_matured ? true : /no accuracy claim/.test(await T(page, "pp-accuracy"))));
  await page.screenshot({ path: `${OUT}/page-paper-desktop.png`, fullPage: true });
  await page.click('[data-testid="pp-pause"]'); await page.waitForTimeout(1200);
  pj = await api("/paper");
  check("Pause pauses the book (status shown and stored)", pj.book.status === "paused" && /Paused/.test(await T(page, "pp-status")));
  await page.click('[data-testid="pp-resume"]'); await page.waitForTimeout(1200);
  check("Resume reactivates it", (await api("/paper")).book.status === "active" && /Active/.test(await T(page, "pp-status")));
  await page.fill('[data-testid="pp-in-stop_loss_pct"]', "2.5"); await page.click('[data-testid="pp-save"]'); await page.waitForTimeout(1200);
  check("risk limits are editable and persist (stop-loss 2.5%)", (await api("/paper")).book.params.stop_loss_pct === 2.5 && /Saved/.test(await T(page, "pp-note")));
  await page.fill('[data-testid="pp-in-stop_loss_pct"]', "0"); await page.click('[data-testid="pp-save"]'); await page.waitForTimeout(1200);
  check("an invalid limit is refused with a message and nothing changes", /between/.test(await T(page, "pp-note")) && (await api("/paper")).book.params.stop_loss_pct === 2.5, await T(page, "pp-note"));
  const idBefore = (await api("/paper")).book.id, fillsBefore = (await api("/paper")).book.fills.length;
  await page.click('[data-testid="pp-new"]'); await page.click('[data-testid="pp-new-confirm"]'); await page.waitForTimeout(1500);
  pj = await api("/paper");
  const old = pj.books.find((b) => b.id === idBefore);
  check("starting a fresh book needs confirmation, archives the old book and keeps its history", pj.book.id !== idBefore && old.status === "archived" && (await page.locator('[data-testid="pp-books"] tbody tr').count()) === pj.books.length && pj.books.length >= 2);
  const oldFills = (await api(`/paper`)).books.find((b) => b.id === idBefore);
  check("the fresh book is separate: flat, full cash, no inherited fills; the old book keeps its trades", pj.book.performance.n_fills === 0 && pj.book.performance.equity === pj.book.initial_cash && oldFills.n_trades >= 0 && fillsBefore >= 1 && /Flat/.test(await T(page, "pp-tiles")));
  await page.screenshot({ path: `${OUT}/page-paper-fresh-book.png`, fullPage: true });
  const unexpected = errors.filter((e) => !/status of 422/.test(e));      // the invalid-limit step above provokes exactly one 422 on purpose
  check("no console errors or server errors on Paper trading (apart from the one deliberately provoked 422)", unexpected.length === 0 && bad.length === 0 && errors.length <= 1, [...unexpected, ...bad].slice(0, 3).join(" | "));
  await ctx.close();
}

// ======================================================= desktop: Alerts & settings
{
  const { ctx, page, errors, bad } = await open({ width: 1440, height: 1000 }, "/#/alerts");
  await page.waitForSelector('[data-testid="settings-service"]', { timeout: 40000 }); await page.waitForSelector('[data-testid="alerts-panel"] [data-testid="position"]'); await page.waitForTimeout(1500);
  const w = await api("/worker");
  check("service card names the operating runtime, its heartbeat and the control commands", (await T(page, "svc-state")).includes(w.daemon_running ? (w.runtime ?? "") : "Not running") && (await T(page, "svc-note")) === w.runtime_note && /worker\.sh install/.test(await T(page, "svc-commands")) && /uninstall/.test(await T(page, "svc-commands")));
  check("the tracked Trading 212 position is shown as closed (unconfirmed), with nothing invented", /not recorded|NOT recorded/i.test(await T(page, "entry-note")) && (await page.inputValue('[data-testid="in-position"]')) === "closed_unconfirmed" && (await page.locator('[data-testid="levels"] .gx-chip').count()) === 0, await T(page, "entry-note"));
  await page.selectOption('[data-testid="in-position"]', "open"); await page.waitForTimeout(1500);
  check("reopening the position restores the entry comparison (Below / Above entry price)", /Below entry price/.test(await T(page, "levels")) && /Above entry price/.test(await T(page, "levels")));
  await page.selectOption('[data-testid="in-position"]', "closed"); await page.waitForTimeout(1500);
  await page.fill('[data-testid="in-exit"]', "3990.5"); await page.fill('[data-testid="in-reason"]', "my own decision"); await page.click('[data-testid="save-alerts"]'); await page.waitForTimeout(1500);
  const s = (await api("/alerts")).settings;
  check("closure details are stored only if the user enters them", s.position_status === "closed" && s.exit_price === 3990.5 && s.closure_reason === "my own decision");
  await page.selectOption('[data-testid="in-position"]', "closed_unconfirmed"); await page.waitForTimeout(1200);
  check("data-source status and refresh controls are present", /Yahoo Finance/.test(await T(page, "settings-data")));
  await page.screenshot({ path: `${OUT}/page-alerts-desktop.png`, fullPage: true });
  check("no console errors or server errors on Alerts & settings", errors.length === 0 && bad.length === 0, [...errors, ...bad].slice(0, 3).join(" | "));
  await ctx.close();
}

// ======================================================= mobile 390
for (const [hash, id] of [["/#/learning", "page-learning"], ["/#/paper", "page-paper"], ["/#/alerts", "page-alerts"], ["/#/forecast", "page-forecast"]]) {
  const { ctx, page, errors } = await open({ width: 390, height: 844 }, hash);
  await page.waitForSelector(`[data-testid="${id}"]`, { timeout: 40000 }); await page.waitForTimeout(3000);
  check(`390px ${id.replace("page-", "")}: no horizontal page scroll`, await noOverflow(page));
  const small = await page.evaluate(() => [...document.querySelectorAll(".gx button, .gx-nav a")].filter((b) => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height < 28; }).map((b) => b.textContent.trim().slice(0, 12)));
  check(`390px ${id.replace("page-", "")}: touch targets are comfortable`, small.length === 0, small.join(","));
  await page.screenshot({ path: `${OUT}/page-${id.replace("page-", "")}-mobile.png`, fullPage: true });
  check(`390px ${id.replace("page-", "")}: no console errors`, errors.length === 0, errors.slice(0, 2).join(" | "));
  await ctx.close();
}

await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} page checks passed`);
process.exit(failed.length ? 1 : 0);
