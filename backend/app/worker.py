"""Background worker: collection, forecasting, scoring and learning. Independent of the web UI and the browser.

    python -m app.worker run        long-running daemon (what launchd keeps alive)
    python -m app.worker once       one full cycle now (collect -> score -> issue -> learn), then exit
    python -m app.worker status     what ran, when, and what is in charge
    python -m app.worker rollback <task> [version]

Guarantees:
  * One daemon at a time (file lock). A second ``run`` exits immediately; the web app only starts an in-process worker when
    no other one holds the lock.
  * Each job has its own lock, so retraining can never overlap itself.
  * A failing job is logged and skipped; the last valid data, forecasts and champion stay in place.
  * A sleeping or powered-off Mac records nothing. On wake the daemon catches up by re-downloading history (futures OHLC,
    daily closes, drivers) and issuing any overdue cycle. Spot ticks and hourly intraday forecasts missed while asleep
    cannot be reconstructed, so those periods stay as visible gaps.
"""
from __future__ import annotations

import argparse
import socket
import json
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd

from . import alerts, config, db, drivers, ledger, learning, n8n_link, news_store, nowcast, quality, quote, registry, threshold_research
from .data import DataStore, DataUnavailable

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)   # one byte at offset 0; locking past the end of an empty file is allowed
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

log = logging.getLogger("gold.worker")
SPOT_EVERY = 10
CYCLE_UTC = (23, 30)           # daily full cycle (after the 17:00 ET close and the daily bar completes)
ALERT_EVERY = 120             # forecast + quote checks, hand-off and delivery
SCORE_EVERY = 30 * 60          # look for newly matured outcomes this often
LEARN_CHECK_EVERY = 5 * 60     # check whether a newly completed session needs forecasts (cheap; the data is cached)
OVERDUE_AFTER = 26 * 3600
COLLECT_RETRY = 15 * 60       # while a completed session's data has not arrived, re-download at most this often
BACKOFF = (30, 120, 600, 3600)  # seconds to wait after the 1st, 2nd, 3rd, 4th+ consecutive failure of a job
MAX_FAILURES = 4              # after this many in a row a job is "at its retry limit": it is retried hourly and a scheduled cycle starts it fresh
STEP_ORDER = ("collect", "validate", "score", "learn", "nowcast", "threshold", "alerts", "paper")
INTERVALS = {"spot": SPOT_EVERY, "score": SCORE_EVERY, "learn": LEARN_CHECK_EVERY, "alerts": ALERT_EVERY, "nowcast": 3600}
RUNTIME = os.environ.get("GOLD_RUNTIME", "manual")    # launchd | windows | container | web-embedded | manual: which process is actually operating


def _lock_dir():
    d = config.DATA_DIR / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def file_lock(name: str, blocking: bool = False):
    """Exclusive lock (flock on macOS/Linux, a byte-range lock on Windows). Yields True if acquired, False if another holder has it.
    With ``blocking`` it waits until the lock is free."""
    fd = os.open(_lock_dir() / f"{name}.lock", os.O_CREAT | os.O_RDWR)
    got = False
    try:
        got = _try_lock(fd)
        while blocking and not got:
            time.sleep(0.5)
            got = _try_lock(fd)
        yield got
    finally:
        if got:
            _unlock(fd)
        os.close(fd)


def _state(job: str) -> dict:
    r = db.rows("SELECT * FROM job_state WHERE job=?", (job,))
    return r[0] if r else {"job": job, "consecutive_failures": 0, "last_attempt_at": None, "last_success_at": None, "last_error": None, "next_retry_at": None, "gave_up": 0}


def _record_attempt(job: str, ok: bool, err: str | None, now: float) -> None:
    st = _state(job)
    fails = 0 if ok else st["consecutive_failures"] + 1
    nxt = None if ok else now + BACKOFF[min(fails, len(BACKOFF)) - 1]
    db.connect().execute(
        "INSERT INTO job_state (job,consecutive_failures,last_attempt_at,last_success_at,last_error,next_retry_at,gave_up) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(job) DO UPDATE SET consecutive_failures=excluded.consecutive_failures, last_attempt_at=excluded.last_attempt_at, "
        "last_success_at=COALESCE(excluded.last_success_at, job_state.last_success_at), last_error=excluded.last_error, next_retry_at=excluded.next_retry_at, gave_up=excluded.gave_up",
        (job, fails, now, now if ok else None, None if ok else (err or "")[:300], nxt, int(fails >= MAX_FAILURES)))


def reset_state(job: str | None = None) -> None:
    """A scheduled cycle or a manual run starts every job fresh: back-off and the retry limit apply to unattended retrying, not to a deliberate run."""
    db.connect().execute("UPDATE job_state SET consecutive_failures=0, next_retry_at=NULL, gave_up=0" + ("" if job is None else " WHERE job=?"), () if job is None else (job,))


def run_job(name: str, fn, *args, lock: bool = True, force: bool = False):
    """Run ``fn`` under a per-job lock and record the outcome (durably: job_runs for history, job_state for back-off). Never raises.

    After repeated failures the job backs off (30 s, 2 min, 10 min, then hourly) instead of hammering a provider; ``force`` bypasses that."""
    conn = db.connect()
    now = time.time()
    st = _state(name)
    if not force and st["next_retry_at"] and now < st["next_retry_at"]:
        return None            # backing off: silent, the wait is visible in job_state / the Learning page
    with file_lock(name) as got:
        if lock and not got:
            conn.execute("INSERT INTO job_runs (job,started_at,finished_at,status,detail) VALUES (?,?,?,?,?)",
                         (name, time.time(), time.time(), "skipped", "another instance of this job is already running"))
            return None
        cur = conn.execute("INSERT INTO job_runs (job,started_at,status) VALUES (?,?,?)", (name, time.time(), "running"))
        rid = cur.lastrowid
        try:
            out = fn(*args)
            conn.execute("UPDATE job_runs SET finished_at=?, status='ok', detail=? WHERE id=?", (time.time(), json.dumps(out, default=str)[:1500] if out is not None else None, rid))
            _record_attempt(name, True, None, time.time())
            return out
        except Exception as exc:  # noqa: BLE001 - a failing job keeps the previous valid state
            log.warning("job %s failed: %s", name, exc)
            conn.execute("UPDATE job_runs SET finished_at=?, status='error', detail=? WHERE id=?", (time.time(), f"{exc.__class__.__name__}: {exc}"[:1500], rid))
            _record_attempt(name, False, f"{exc.__class__.__name__}: {exc}", time.time())
            return None


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
_store = DataStore()


def job_spot() -> dict:
    raw = quote.poll_spot()
    return {"price": raw["price"], "tick_epoch": raw["tick_epoch"]}


def job_collect() -> dict:
    """Download fresh completed-session data and driver inputs. Existing valid data survives any failure."""
    out: dict = {}
    ds = _store.get(force_refresh=True)
    out["data_through"] = ds.series.index[-1].date().isoformat()
    out["provider_error"] = ds.meta.get("provider_error")
    if out["provider_error"]:
        quality.provider_error("yahoo_daily", out["provider_error"])
    else:
        quality.provider_ok("yahoo_daily")
    for name, fn in (("drivers", lambda: drivers.load(force=True)), ("news", news_store.collect)):
        try:
            r = fn()
            out[name] = "ok" if name == "drivers" else r
        except Exception as exc:  # noqa: BLE001
            out[name] = f"failed: {exc.__class__.__name__}"
    return out


def job_validate() -> dict:
    """Detect, record and quarantine (never delete) data problems. The result decides whether the learning step may run."""
    return quality.run()


def job_score() -> dict:
    ds = _store.get()
    return {"matured": ledger.mature(ds.series), "data_through": ds.series.index[-1].date().isoformat()}


def issue_nowcast(daily: pd.Series) -> dict:
    """Write this hour's today's-close forecasts (champion, shadows, baseline). Idempotent within the hour."""
    p = nowcast.get(daily)
    if not p["market_open"] or p["hours_left"] <= 0:
        return {"issued": 0, "why": "market closed or target passed"}
    registry.seed()
    champ = registry.champion("nowcast")["version"]
    hour = pd.Timestamp(p["anchor_time_epoch"], unit="s", tz="UTC").floor("h").isoformat()
    n = 0
    packs = [("unchanged", "price_now_v1", p["baseline_forecast"])] + [(c["algorithm"], "intraday_v1", c) for c in p["candidates"]]
    for algo, schema, pk in packs:
        v = registry.version_id("nowcast", algo, schema)
        role = "champion" if v == champ else ("baseline" if algo == "unchanged" else "shadow")
        registry.mark_trained(v, p["target_date"])  # these models are refit on every intraday state rebuild
        if ledger.issue(task="nowcast", role=role, version=v, algorithm=algo, feature_schema=schema, input_cutoff=p["anchor_time_epoch"],
                        base_price=p["anchor_price"], base_time=hour, target_date=p["target_date"], target_time=p["target_time"],
                        predicted=pk["predicted_close"], low=pk["low"], high=pk["high"], level=p["level"], horizon=None,
                        hours_left=p["hours_left"], data_key=f"{daily.index[-1].date()}|{len(daily)}"):
            n += 1
    return {"issued": n, "target_date": p["target_date"], "hours_left": p["hours_left"]}


def job_nowcast() -> dict:
    return issue_nowcast(_store.get().series)


def job_learn(force: bool = False) -> dict:
    ok, why = quality.ok_to_train()
    if not ok:     # the working models and forecasts stay exactly as they are; the reason is recorded and shown
        return {"skipped": "blocked by a data-quality finding: " + "; ".join(f"{b['kind']} ({b['detail'][:80]})" for b in why), "blocking": why}
    ds = _store.get()
    try:
        d = drivers.load()
    except drivers.DriversUnavailable:
        d = None
    return learning.run_learning(ds.series, d, ds.key, force=force)


def job_threshold(force: bool = False) -> dict:
    """Touch/endpoint threshold research (chronological, once per data version) and the forward touch record. Never touches the models."""
    ds = _store.get()
    try:
        d = drivers.load()
    except drivers.DriversUnavailable:
        d = None
    levels = alerts.get_settings()["levels"]
    ohlc = threshold_research.get_ohlc()
    prev = threshold_research.latest()
    through = ds.series.index[-1].date().isoformat()
    if force or prev is None or prev.get("data_through") != through or prev.get("levels") != levels:
        prev = threshold_research.run(ds.series, ohlc, d, levels, through)
    rec = threshold_research.record_forward(ds.series, ohlc, d, levels, prev)
    return {"status": prev["status"], "variant": prev["selected_variant"], "forward": rec, "matured": threshold_research.mature_forward(ohlc)}


def job_alerts() -> dict:
    """Evaluate review levels (forecast heads-up and observed-price crossing are independent), then hand off to n8n. Read-only on forecasts."""
    out = {"forecast": alerts.check_forecasts()["new_events"], "price": alerts.check_price()["new_events"]}
    out["delivery"] = n8n_link.deliver_pending()
    out["handoff"] = n8n_link.push_snapshot()["state"]
    out["sync"] = n8n_link.sync_status().get("state")
    return out


def job_paper() -> dict:
    """Paper trading: evaluate each active simulated book against the latest stored forecasts. Simulation only; it never reaches a broker."""
    from . import paper

    return paper.run_all(lambda: alerts.fetch_quote("futures"))


def _step(name: str, fn, *, force: bool = True):
    t0 = time.time()
    out = run_job(name, fn, force=force)
    skipped = isinstance(out, dict) and "skipped" in out
    failed = out is None
    return {"step": name, "status": "failed" if failed else ("skipped" if skipped else "ok"), "seconds": round(time.time() - t0, 1),
            "detail": (None if out is None else {k: v for k, v in out.items() if k in ("data_through", "skipped", "matured", "ok_to_train", "new_findings", "decisions", "forward_issue", "issued", "new_events")}
                       if isinstance(out, dict) else out)}


def full_cycle(trigger: str = "manual") -> dict:
    """collect -> validate -> score -> (features -> train candidates -> evaluate -> record decision -> publish forecasts) -> nowcast -> thresholds -> alerts -> paper.

    The middle group runs inside ``learning.run_learning`` in that order. Every step is recorded durably in ``cycle_runs``; a failing step is logged
    and the rest still run, so one provider hiccup cannot stop forecasts, alerts or paper trading. A scheduled/manual cycle starts all jobs fresh."""
    reset_state()
    conn = db.connect()
    cur = conn.execute("INSERT INTO cycle_runs (started_at, trigger, runtime, steps, status) VALUES (?,?,?,?,?)", (time.time(), trigger, RUNTIME, "[]", "running"))
    rid, steps = cur.lastrowid, []
    fns = {"collect": job_collect, "validate": job_validate, "score": job_score, "learn": job_learn, "nowcast": job_nowcast,
           "threshold": job_threshold, "alerts": job_alerts, "paper": job_paper}
    for name in STEP_ORDER:
        steps.append(_step(name, fns[name]))
        conn.execute("UPDATE cycle_runs SET steps=? WHERE id=?", (json.dumps(steps, default=str), rid))
    bad = [x for x in steps if x["status"] == "failed"]
    through = next((x["detail"]["data_through"] for x in steps if x["step"] == "collect" and isinstance(x.get("detail"), dict) and x["detail"].get("data_through")), None)
    conn.execute("UPDATE cycle_runs SET finished_at=?, data_through=?, status=? WHERE id=?",
                 (time.time(), through, "ok" if not bad else ("failed" if len(bad) == len(steps) else "partial"), rid))
    return {"cycle": rid, "trigger": trigger, "status": "ok" if not bad else "partial", "steps": steps}


def needs_learning(daily: pd.Series) -> bool:
    """True when a completed session exists for which no forward forecast has been issued yet (fresh data arrived)."""
    r = db.rows("SELECT MAX(base_time) AS m FROM forecasts WHERE task LIKE 'fwd_%'")[0]["m"]
    return r != daily.index[-1].date().isoformat()


def last_ok(job: str) -> float | None:
    r = db.rows("SELECT MAX(finished_at) AS t FROM job_runs WHERE job=? AND status='ok'", (job,))
    return r[0]["t"] if r else None


def cycle_overdue(now: float | None = None) -> bool:
    t = last_ok("learn")
    return t is None or (now or time.time()) - t > OVERDUE_AFTER


def session_gap() -> dict | None:
    """Which completed session SHOULD be in the data (exchange calendar + settlement grace) versus what is. None if there is no data yet."""
    try:
        return quality.staleness(_store.get().series)
    except Exception:  # noqa: BLE001
        return None


def next_daily_cycle(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    t = now.replace(hour=CYCLE_UTC[0], minute=CYCLE_UTC[1], second=0, microsecond=0)
    return t if t > now else t + pd.Timedelta(days=1).to_pytimedelta()


def next_runs(now: float | None = None) -> list[dict]:
    """When each recurring job will next run (from its recorded last attempt and cadence), and what is waiting in back-off."""
    now = now or time.time()
    out = []
    for job, every in INTERVALS.items():
        st = _state(job)
        if job == "nowcast":
            at = (int(now // 3600) + 1) * 3600.0
        else:
            at = max(now, (st["last_attempt_at"] or 0) + every)
        if st["next_retry_at"] and st["next_retry_at"] > at:
            at = st["next_retry_at"]
        out.append({"job": job, "every_s": every, "next_at": at, "backing_off": bool(st["next_retry_at"] and st["next_retry_at"] > now), "failures": st["consecutive_failures"], "at_retry_limit": bool(st["gave_up"])})
    out.append({"job": "daily cycle", "every_s": 86400, "next_at": next_daily_cycle(datetime.fromtimestamp(now, timezone.utc)).timestamp(), "backing_off": False, "failures": 0, "at_retry_limit": False})
    return sorted(out, key=lambda x: x["next_at"])


# --------------------------------------------------------------------------- #
# Daemon
# --------------------------------------------------------------------------- #
def standalone_installed() -> bool:
    """True when the launchd agent is installed: the web app then leaves the jobs to it instead of starting an embedded worker."""
    from pathlib import Path

    return (Path.home() / "Library" / "LaunchAgents" / "ai-gold-forecast.worker.plist").exists()


def _heartbeat(state: dict) -> None:
    p = config.DATA_DIR / "worker_heartbeat.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({**state, "pid": os.getpid(), "runtime": RUNTIME, "host": socket.gethostname(), "at": time.time()}))
    tmp.replace(p)


def run_loop(stop: threading.Event | None = None, once_overdue: bool = True, runtime: str | None = None, wait_for_lock: bool = False) -> bool:
    """Run until ``stop`` is set. Returns False immediately if another worker holds the daemon lock (or, with ``wait_for_lock``, waits for it)."""
    global RUNTIME
    stop = stop or threading.Event()
    if runtime:
        RUNTIME = runtime
    registry.seed()
    while True:
        with file_lock("daemon") as got:
            if not got:
                if not wait_for_lock or stop.is_set():
                    return False
                stop.wait(15.0)       # standby: take over as soon as the current holder (e.g. an embedded web worker) exits
                if stop.is_set():
                    return False
                continue
            log.info("worker started (pid %s, runtime %s)", os.getpid(), RUNTIME)
            last = {"spot": 0.0, "score": 0.0, "learn": 0.0, "alerts": 0.0, "hb": 0.0, "collect": 0.0, "paper": 0.0}
            last_hour, last_cycle_day = -1, None
            _heartbeat({"state": "running"})
            if once_overdue and cycle_overdue():
                log.info("catch-up: the last full cycle is overdue; running it now (also re-downloads history after any sleep)")
                full_cycle("catch-up")
                last_cycle_day = datetime.now(timezone.utc).date()
            while not stop.is_set():
                now = time.time()
                if now - last["spot"] >= SPOT_EVERY:
                    run_job("spot", job_spot, lock=False)
                    last["spot"] = now
                hour = int(now // 3600)
                if hour != last_hour:
                    run_job("nowcast", job_nowcast)
                    last_hour = hour
                if now - last["score"] >= SCORE_EVERY:
                    run_job("score", job_score)
                    last["score"] = now
                if now - last["learn"] >= LEARN_CHECK_EVERY:
                    last["learn"] = now
                    try:
                        gap = session_gap()
                        if gap and gap["sessions_behind"] >= 1 and now - last["collect"] >= COLLECT_RETRY:
                            last["collect"] = now   # a completed session should exist but its data has not arrived: ask the provider again (bounded)
                            run_job("collect", job_collect)
                            run_job("validate", job_validate)
                        if needs_learning(_store.get().series):   # fresh completed-session data -> the whole sequence for the new session
                            run_job("validate", job_validate)
                            run_job("learn", job_learn)
                            run_job("threshold", job_threshold)
                            last["alerts"] = last["paper"] = 0.0    # a newly published forecast is evaluated now, not later
                    except Exception as exc:  # noqa: BLE001
                        log.warning("learn trigger failed: %s", exc)
                if now - last["alerts"] >= ALERT_EVERY:
                    last["alerts"] = now
                    run_job("alerts", job_alerts)
                if now - last["paper"] >= ALERT_EVERY:
                    last["paper"] = now
                    run_job("paper", job_paper)
                utc = datetime.now(timezone.utc)
                if (utc.hour, utc.minute) >= CYCLE_UTC and last_cycle_day != utc.date():
                    full_cycle("scheduled")
                    last_cycle_day = utc.date()
                if now - last["hb"] >= 20:
                    _heartbeat({"state": "running"})
                    last["hb"] = now
                stop.wait(2.0)
            return True


def heartbeat() -> dict | None:
    try:
        return json.loads((config.DATA_DIR / "worker_heartbeat.json").read_text())
    except Exception:  # noqa: BLE001
        return None


def heartbeat_age() -> float | None:
    hb = heartbeat()
    return None if not hb else time.time() - hb["at"]


def runtime_note(hb: dict | None, running: bool) -> str:
    rt = (hb or {}).get("runtime")
    if not running:
        return "No worker is running. Nothing is being collected, trained or scored until one starts (./scripts/worker.sh install, or python -m app.worker run)."
    notes = {"launchd": "The launchd agent is operating: it restarts after a crash or login and runs with the browser and web server closed. It stops while this Mac sleeps or is off.",
             "windows": "The Windows learning window is operating: its launcher restarts it after a crash, and it runs with the browser and web server closed. It stops while this PC sleeps or is off.",
             "container": "An always-on container worker is operating: it does not depend on this Mac being awake.",
             "web-embedded": "Only the worker embedded in the web server is operating: it stops when the server stops. Install the launchd agent for independence from the web server.",
             "manual": "A manually started worker is operating: it stops when its terminal closes."}
    return notes.get(rt, f"A worker with runtime '{rt}' is operating.")


def last_cycle() -> dict | None:
    r = db.rows("SELECT * FROM cycle_runs ORDER BY id DESC LIMIT 1")
    if not r:
        return None
    c = r[0]
    c["steps"] = json.loads(c["steps"] or "[]")
    return c


def status() -> dict:
    registry.seed()
    hb = heartbeat()
    age = None if not hb else time.time() - hb["at"]
    running = hb is not None and age is not None and age < 90
    jobs = {}
    for j in ("spot", "collect", "validate", "score", "learn", "nowcast", "threshold", "alerts", "paper"):
        r = db.rows("SELECT status, started_at, finished_at, detail FROM job_runs WHERE job=? ORDER BY id DESC LIMIT 1", (j,))
        ok = last_ok(j)
        st = _state(j)
        jobs[j] = {"last": r[0] if r else None, "last_ok": ok, "age_s": None if ok is None else round(time.time() - ok), "failures": st["consecutive_failures"],
                   "last_error": st["last_error"], "next_retry_at": st["next_retry_at"], "at_retry_limit": bool(st["gave_up"])}
    return {"daemon_running": running, "heartbeat_age_s": None if age is None else round(age), "runtime": (hb or {}).get("runtime"), "host": (hb or {}).get("host"), "pid": (hb or {}).get("pid"),
            "runtime_note": runtime_note(hb, running), "launchd_installed": standalone_installed(),
            "jobs": jobs, "champions": {t: registry.champion(t)["version"] for t in registry.TASKS}, "next_runs": next_runs(), "last_cycle": last_cycle(), "session_gap": session_gap(),
            "forecasts": db.rows("SELECT COUNT(*) AS n FROM forecasts")[0]["n"], "matured": db.rows("SELECT COUNT(*) AS n FROM outcomes")[0]["n"],
            "spot_ticks": db.rows("SELECT COUNT(*) AS n FROM spot_ticks")[0]["n"], "cycle_overdue": cycle_overdue()}


def cycle_history(limit: int = 10) -> dict:
    """The persisted full-cycle records and a plain verdict on whether any was started by the schedule (not by hand).

    Only rows in ``cycle_runs`` count as evidence. ``scheduled`` is the 23:30 UTC trigger, ``catch-up`` an overdue run at worker start, ``manual`` a hand-started run."""
    rows = db.rows("SELECT id, trigger, runtime, started_at, finished_at, data_through, status, steps FROM cycle_runs ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["steps"] = [{"step": s.get("step"), "status": s.get("status")} for s in json.loads(r["steps"] or "[]")]
    counts = {r["trigger"]: r["n"] for r in db.rows("SELECT trigger, COUNT(*) AS n FROM cycle_runs GROUP BY trigger")}
    sched_ok = db.rows("SELECT COUNT(*) AS n FROM cycle_runs WHERE trigger='scheduled' AND status='ok'")[0]["n"]
    nxt = next_daily_cycle()
    if sched_ok:
        verdict = f"{sched_ok} scheduled cycle(s) recorded with status ok."
    elif counts.get("scheduled"):
        verdict = "Scheduled cycle(s) ran but none finished ok; see the steps."
    else:
        verdict = f"No scheduled cycle has been recorded yet. The next is due {nxt:%Y-%m-%d %H:%M} UTC."
    return {"counts": counts, "scheduled_ok": sched_ok, "next_scheduled_utc": nxt.isoformat(), "verdict": verdict, "recent": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.worker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("cycles", help="the persisted full-cycle records and whether the schedule has run")
    sub.add_parser("once")
    sub.add_parser("validate")
    sub.add_parser("learn").add_argument("--force", action="store_true", help="re-run the development evaluation for the current data")
    sub.add_parser("status").add_argument("--json", action="store_true")
    al = sub.add_parser("alerts", help="evaluate review levels now, or simulate prices/forecasts safely")
    al.add_argument("--simulate", action="store_true", help="use a throwaway database; never contacts n8n or sends an SMS")
    al.add_argument("--price", type=float, help="simulated observed quote (USD/oz)")
    al.add_argument("--forecast", type=float, help="simulated next-session forecast (USD/oz)")
    sub.add_parser("threshold").add_argument("--force", action="store_true")
    rb = sub.add_parser("rollback")
    rb.add_argument("task", choices=registry.TASKS)
    rb.add_argument("version", nargs="?")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.cmd == "run":
        return 0 if run_loop(wait_for_lock=True) else 0
    if a.cmd == "cycles":
        h = cycle_history()
        print(h["verdict"])
        print("by trigger:", h["counts"] or "none")
        for r in h["recent"]:
            t0 = datetime.fromtimestamp(r["started_at"], timezone.utc)
            secs = "-" if r["finished_at"] is None else f"{r['finished_at'] - r['started_at']:.0f}s"
            print(f"  #{r['id']:<3} {r['trigger']:<9} {t0:%Y-%m-%d %H:%M} UTC  {secs:>5}  {r['status']:<8} runtime={r['runtime']}  data through {r['data_through']}")
            print("       " + " ".join(f"{s['step']}={s['status']}" for s in r["steps"]))
        return 0
    if a.cmd == "learn":
        print(json.dumps(run_job("learn", job_learn, a.force), indent=1, default=str))
        return 0
    if a.cmd == "threshold":
        print(json.dumps(run_job("threshold", job_threshold, a.force), indent=1, default=str))
        return 0
    if a.cmd == "alerts":
        if a.simulate:
            from . import alerts_sim
            print(json.dumps(alerts_sim.simulate(price=a.price, forecast=a.forecast), indent=1, default=str))
        else:
            print(json.dumps(run_job("alerts", job_alerts), indent=1, default=str))
        return 0
    if a.cmd == "once":
        print(json.dumps(full_cycle("manual"), indent=1, default=str))
        return 0
    if a.cmd == "validate":
        print(json.dumps(run_job("validate", job_validate, force=True), indent=1, default=str))
        return 0
    if a.cmd == "rollback":
        print("champion is now", registry.rollback(a.task, a.version))
        return 0
    s = status()
    if a.json:
        print(json.dumps(s, indent=1, default=str))
        return 0
    now = time.time()
    fmt = lambda t: "never" if t is None else f"{int((now - t) // 60)} min ago"  # noqa: E731
    print(f"daemon: {'RUNNING' if s['daemon_running'] else 'not running'}"
          + (f" (heartbeat {s['heartbeat_age_s']}s ago, runtime {s['runtime']}, pid {s['pid']})" if s["heartbeat_age_s"] is not None else "")
          + f"   launchd agent installed: {s['launchd_installed']}")
    print(f"  {s['runtime_note']}")
    g = s["session_gap"]
    if g:
        print(f"  data through {g['data_through']}; latest completed session per the exchange calendar: {g['expected_last_session']} ({g['sessions_behind']} behind)")
    for j, v in s["jobs"].items():
        last = v["last"]
        extra = f"   failures in a row: {v['failures']}" + (" (at retry limit)" if v["at_retry_limit"] else "") if v["failures"] else ""
        print(f"  {j:9s} last ok: {fmt(v['last_ok']):>12s}   last run: {last['status'] if last else '-'}{extra}")
    c = s["last_cycle"]
    if c:
        print(f"  last full cycle #{c['id']} ({c['trigger']}, {c['status']}): " + ", ".join(f"{x['step']}={x['status']}" for x in c["steps"]))
    print(f"forecasts logged: {s['forecasts']}  matured: {s['matured']}  spot ticks: {s['spot_ticks']}  full cycle overdue: {s['cycle_overdue']}")
    for t, v in s["champions"].items():
        print(f"  champion {t:8s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
