"""Threshold research: two DIFFERENT events, never mixed.

  endpoint   "the official settlement of the next session is >= L"   (what the existing forecast predicts; used by the forecast heads-up)
  touch      "the next daily bar's HIGH is >= L"                       (EXPERIMENTAL; needs its own target and its own evidence)

A touch probability is never derived from a closing-price interval. It is a separate classifier trained on actual daily highs.

Event definitions (identical in training, evaluation and alerts):
  instrument   COMEX gold futures GC=F, USD per troy ounce, Yahoo Finance daily bars
  origin       the 13:30 New York settlement of a completed session (inputs timestamped by availability, see drivers.asof_features)
  touch event  high of the NEXT daily bar >= L. Audited against 5-minute data: the provider's daily-bar high equals the maximum
               5-minute high of the CME session that opens 18:00 New York the evening before and closes 17:00 (exact on 21 of 21
               recent days; the New York calendar day matches on only 9). So the window is the next CME session.
  Note: the 13:30 origin to 18:00 gap (about 3.5 trading hours that day) is covered by neither session; a stated limitation.

Levels are expressed as a distance in daily-volatility units, z = ln(L / close) / sigma20, so a fixed level such as 4000 can be
learned from every historical price regime (a fixed absolute level is a rare event in a ten-year history). The live level is
translated to its z at issue time.

Method (bounded, chronological): expanding-window walk-forward, refit every REFIT origins; training rows whose label window reaches the
test block are purged, plus a one-session embargo; scaling and the classifier are fitted inside each training fold only. The
out-of-sample region is split once into a SELECTION slice (first half: used to pick the variant and the alert probability cut-off) and a
FINAL slice (second half: reported, never used to select anything). Variants: z_only (climatology by distance), gold (price pattern),
gold_drivers (adds dollar, yields, VIX joined by availability time).

Probabilities are shown only when the evidence gate passes (enough events at the live distance, calibration within tolerance, a better
Brier score than climatology on the FINAL slice). Otherwise the target stays experimental and endpoint-based alerts are unaffected.
"""
from __future__ import annotations

import json
import time
import warnings
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from . import config, data, db, drivers, sessions
from . import research_drivers as rd
from .features import build_features

Z_GRID = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0)
GOLD_COLS = ["vol20l", "mom_5", "mom_20", "ret_t0", "ma_ratio_20"]
DRIVER_COLS = ["d_dxy_chg1", "d_y10_chg1", "d_real10_chg1", "d_vix_level"]   # bounded set: dollar, yields, real yield, volatility
VARIANTS = {"z_only": [], "gold": GOLD_COLS, "gold_drivers": GOLD_COLS + DRIVER_COLS}
REFIT = 63
OOS_START_FRAC = 0.5
MIN_TRAIN = 400
SELECTION_MARGIN = 0.01
GATE = {"min_events_in_band": 30, "max_calibration_gap": 0.05, "band_halfwidth_z": 0.75}
EVENT_DEF = ("GC=F high of the next CME session (18:00 New York the evening before to 17:00, as the provider's daily bar) >= level, USD per troy ounce; origin = 13:30 New York settlement of a completed session")
ENDPOINT_DEF = "official GC=F settlement of the next session >= level (USD per troy ounce)"
LEAD_TIME = {"min_h": 4.5, "max_h": 27.5, "note": "Issued at the 13:30 New York settlement; the event window runs from 18:00 New York that evening to 17:00 the next day. The time of the high inside the session is not in daily bars, so realised lead time is a range, not a measurement."}


# ---------------------------------------------------------------------------------------------------------------- data
def _ohlc_path():
    return config.DATA_DIR / "gc_daily_ohlc.csv"


def parse_ohlc(payload: dict, now: float | None = None, session_end: str = "17:00") -> pd.DataFrame:
    now = now if now is not None else time.time()
    res = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not res:
        raise data.DataUnavailable("provider returned no OHLC data")
    q = (res.get("indicators") or {}).get("quote") or [{}]
    stamps, hi, cl = res.get("timestamp") or [], q[0].get("high") or [], q[0].get("close") or []
    if not stamps or not (len(stamps) == len(hi) == len(cl)):
        raise data.DataUnavailable("provider OHLC response was malformed")
    tz = (res.get("meta") or {}).get("exchangeTimezoneName") or "America/New_York"
    df = pd.DataFrame({"date": data._local_dates(stamps, tz), "high": hi, "close": cl}).dropna()
    df = df[(df["high"] > 0) & (df["close"] > 0) & (df["high"] >= df["close"] - 1e-6)]   # a high below the close is a data error
    df = df.drop_duplicates("date", keep="last").set_index("date").sort_index()
    if len(df):
        hh, mm = (int(x) for x in session_end.split(":"))
        end_epoch = (df.index[-1].tz_localize(tz) + pd.Timedelta(hours=hh, minutes=mm)).timestamp()
        if now < end_epoch:   # that session is still trading: its high is not final
            df = df.iloc[:-1]
    return df


def get_ohlc(force: bool = False, now: float | None = None) -> pd.DataFrame:
    now = now or time.time()
    p = _ohlc_path()
    if p.exists() and not force and (config.OFFLINE or now - p.stat().st_mtime < 6 * 3600):
        return pd.read_csv(p, index_col=0, parse_dates=True)
    try:
        df = parse_ohlc(data._get_chart(config.SYMBOL, "10y", None), now)
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(p)
        return df
    except Exception:  # noqa: BLE001 - keep the last good file
        if p.exists():
            return pd.read_csv(p, index_col=0, parse_dates=True)
        raise


# ---------------------------------------------------------------------------------------------------------------- dataset
def build(close: pd.Series, high: pd.Series, df_drivers: pd.DataFrame | None) -> dict:
    close = close.astype(float)
    high = high.reindex(close.index).astype(float)
    feats = build_features(close)
    ret = np.log(close).diff()
    feats["vol20l"] = np.log(feats["vol_20"])
    sigma = feats["vol_20"]
    if df_drivers is not None:
        origins = pd.Series([pd.Timestamp(d).tz_localize(drivers.NY) + pd.Timedelta(hours=13, minutes=30) for d in close.index], index=close.index)
        d_ok = drivers.asof_features(origins, df_drivers)
        for c in drivers.ALL_DRIVER_FEATURES:
            feats["d_" + c] = d_ok[c]
    y_hi = np.log(high.shift(-1) / close)
    y_cl = np.log(close.shift(-1) / close)
    cols = GOLD_COLS + [c for c in DRIVER_COLS if c in feats.columns]
    complete = feats[cols].notna().all(axis=1) & sigma.notna()
    x_now = feats[cols].iloc[-1] if complete.iloc[-1] else None
    keep = complete & y_hi.notna() & y_cl.notna()      # only MATURED outcomes train or score anything
    idx = np.where(keep.to_numpy())[0]
    return {"dates": close.index[idx], "pos": idx, "X": feats[cols].iloc[idx].reset_index(drop=True), "sigma": sigma.iloc[idx].to_numpy(),
            "y_hi": y_hi.iloc[idx].to_numpy(), "y_cl": y_cl.iloc[idx].to_numpy(), "p0": close.iloc[idx].to_numpy(),
            "x_now": x_now, "sigma_now": float(sigma.iloc[-1]) if np.isfinite(sigma.iloc[-1]) else None, "price_now": float(close.iloc[-1]), "now_date": close.index[-1],
            "has_drivers": all(c in feats.columns for c in DRIVER_COLS)}


def _design(X: pd.DataFrame, rows: np.ndarray, cols: list[str], z: float | np.ndarray) -> np.ndarray:
    zz = np.asarray(z, dtype=float) * np.ones(len(rows))
    base = [zz, zz ** 2]
    return np.column_stack(base + [X[c].to_numpy()[rows] for c in cols]) if cols else np.column_stack(base)


def _model():
    return Pipeline([("s", StandardScaler()), ("c", LogisticRegression(C=0.5, max_iter=400))])


def _fit(ds: dict, rows: np.ndarray, cols: list[str], target: str):
    key = "y_hi" if target == "touch" else "y_cl"
    Xs, ys = [], []
    for z in Z_GRID:
        Xs.append(_design(ds["X"], rows, cols, z))
        ys.append((ds[key][rows] >= z * ds["sigma"][rows]).astype(int))
    y = np.concatenate(ys)
    if y.min() == y.max():
        raise ValueError("no variation in the training labels")
    return _model().fit(np.vstack(Xs), y)


def _predict(model, ds: dict, rows: np.ndarray, cols: list[str]) -> np.ndarray:
    return np.column_stack([model.predict_proba(_design(ds["X"], rows, cols, z))[:, 1] for z in Z_GRID])


def walk_forward(ds: dict, cols: list[str], target: str = "touch") -> dict:
    n = len(ds["pos"])
    start = int(n * OOS_START_FRAC)
    prob = np.full((n, len(Z_GRID)), np.nan)
    pos = ds["pos"]
    for i in range(start, n, REFIT):
        j = min(i + REFIT, n)
        train = np.where(pos + 2 < pos[i])[0]       # the label window (next bar) ends before the block, plus a one-session embargo
        if len(train) < MIN_TRAIN:
            continue
        m = _fit(ds, train, cols, target)
        prob[i:j] = _predict(m, ds, np.arange(i, j), cols)
    return {"prob": prob, "start": start}


def labels(ds: dict, target: str) -> np.ndarray:
    key = "y_hi" if target == "touch" else "y_cl"
    return ds[key][:, None] >= np.array(Z_GRID)[None, :] * ds["sigma"][:, None]


# ---------------------------------------------------------------------------------------------------------------- metrics
def calibration(p: np.ndarray, y: np.ndarray, bins: int = 10) -> dict:
    edges = np.linspace(0, 1, bins + 1)
    rel, ece = [], 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & ((p < hi) if hi < 1 else (p <= hi))
        if m.sum():
            rel.append({"lo": round(float(lo), 2), "hi": round(float(hi), 2), "n": int(m.sum()), "mean_p": round(float(p[m].mean()), 4), "observed": round(float(y[m].mean()), 4)})
            ece += m.sum() / len(p) * abs(p[m].mean() - y[m].mean())
    return {"ece": round(float(ece), 4), "reliability": rel}


def confusion(p: np.ndarray, y: np.ndarray, cut: float) -> dict:
    a = p >= cut
    tp, fp, fn, tn = int((a & y).sum()), int((a & ~y).sum()), int((~a & y).sum()), int((~a & ~y).sum())
    return {"cut": round(float(cut), 3), "events": tp + fn, "alerts": tp + fp, "true_alerts": tp, "false_alerts": fp, "missed_events": fn,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None, "recall": round(tp / (tp + fn), 4) if tp + fn else None}


def pick_cut(p: np.ndarray, y: np.ndarray) -> float:
    """Alert probability cut-off, chosen on the SELECTION slice only (max F1 with at least 5 alerts)."""
    best, best_f = 0.5, -1.0
    for c in np.arange(0.05, 0.91, 0.05):
        a = p >= c
        tp, fp, fn = int((a & y).sum()), int((a & ~y).sum()), int((~a & y).sum())
        if tp + fp < 5 or tp + fn == 0:
            continue
        f = 2 * tp / (2 * tp + fp + fn)
        if f > best_f:
            best, best_f = float(c), f
    return best


def score_slice(prob: np.ndarray, lab: np.ndarray, rows: np.ndarray, cut: float | None = None) -> dict:
    p, y = prob[rows].ravel(), lab[rows].ravel()
    ok = ~np.isnan(p)
    p, y = p[ok], y[ok]
    if len(p) == 0:
        return {"n": 0}
    clim = y.mean()
    brier = float(np.mean((p - y) ** 2))
    pc = np.clip(p, 1e-6, 1 - 1e-6)
    out = {"n": int(len(p)), "event_rate": round(float(clim), 4), "brier": round(brier, 5), "brier_climatology": round(float(np.mean((clim - y) ** 2)), 5),
           "log_loss": round(float(-np.mean(y * np.log(pc) + (1 - y) * np.log(1 - pc))), 5), **calibration(p, y)}
    if cut is not None:
        out["at_cut"] = confusion(p, y, cut)
    return out


def band_stats(prob: np.ndarray, lab: np.ndarray, rows: np.ndarray, z_live: float | None) -> dict:
    """Evidence at the distance the live level actually sits at (a handful of z columns around z_live)."""
    if z_live is None:
        return {"z_live": None, "events": 0, "n": 0, "in_range": False}
    zs = np.array(Z_GRID)
    cols = np.where(np.abs(zs - z_live) <= GATE["band_halfwidth_z"])[0]
    if len(cols) == 0:
        return {"z_live": round(float(z_live), 2), "events": 0, "n": 0, "in_range": False}
    sub_p, sub_y = prob[np.ix_(rows, cols)], lab[np.ix_(rows, cols)]
    dates_ok = ~np.isnan(sub_p).any(axis=1)
    p, y = sub_p[dates_ok].ravel(), sub_y[dates_ok].ravel()
    # Several z columns share one date and one price path, so they are not independent: count DISTINCT dates with an event.
    event_dates = int(sub_y[dates_ok].any(axis=1).sum())
    return {"z_live": round(float(z_live), 2), "in_range": True, "n": int(len(p)), "n_dates": int(dates_ok.sum()), "events": event_dates, "event_rows": int(y.sum()),
            "mean_p": round(float(p.mean()), 4) if len(p) else None, "observed": round(float(y.mean()), 4) if len(p) else None,
            "gap": round(float(abs(p.mean() - y.mean())), 4) if len(p) else None}


def run_target(ds: dict, target: str, levels: list[float]) -> dict:
    lab = labels(ds, target)
    n = len(ds["pos"])
    start = int(n * OOS_START_FRAC)
    mid = start + (n - start) // 2
    sel_rows, fin_rows = np.arange(start, mid), np.arange(mid, n)
    out: dict = {"n_origins": n, "oos_start": str(ds["dates"][start].date()), "selection": [str(ds["dates"][start].date()), str(ds["dates"][mid - 1].date())],
                 "final": [str(ds["dates"][mid].date()), str(ds["dates"][-1].date())], "variants": {}}
    probs = {}
    for name, cols in VARIANTS.items():
        if name == "gold_drivers" and not ds["has_drivers"]:
            continue
        wf = walk_forward(ds, cols, target)
        probs[name] = wf["prob"]
        ps, ys = wf["prob"][sel_rows].ravel(), lab[sel_rows].ravel()
        cut = pick_cut(ps[~np.isnan(ps)], ys[~np.isnan(ps)])
        out["variants"][name] = {"selection": score_slice(wf["prob"], lab, sel_rows, cut), "final": score_slice(wf["prob"], lab, fin_rows, cut), "cut_from_selection": round(cut, 3)}
    # Selection uses the SELECTION slice only.
    base_ll = out["variants"]["z_only"]["selection"]["log_loss"]
    best = min((v for v in out["variants"] if v != "z_only"), key=lambda v: out["variants"][v]["selection"].get("log_loss", 9), default=None)
    # A richer variant replaces the distance-only baseline only if it is >= SELECTION_MARGIN better on the SELECTION slice. The final slice never decides.
    sel = best if best and out["variants"][best]["selection"]["log_loss"] <= base_ll * (1 - SELECTION_MARGIN) else "z_only"
    out["selected"] = sel
    out["selection_rule"] = f"z_only unless a variant has >= {SELECTION_MARGIN:.0%} lower log loss on the selection slice"
    price, sig = ds["price_now"], ds["sigma_now"]
    live = []
    for L in levels:
        z = float(np.log(L / price) / sig) if sig and L > 0 else None
        b = band_stats(probs[sel], lab, fin_rows, z) if z is not None and z > 0 else {"z_live": None if z is None else round(z, 2), "events": 0, "n": 0, "in_range": False}
        b_all = band_stats(probs[sel], lab, np.arange(start, n), z) if z is not None and z > 0 else {}
        live.append({"level": L, "z": None if z is None else round(z, 2), "final_slice": b, "all_oos": b_all})
    out["live_levels"] = live
    fin = out["variants"][sel]["final"]
    out["brier_skill_vs_climatology_final"] = round(1 - fin["brier"] / fin["brier_climatology"], 4) if fin.get("n") else None
    out["improvement_vs_z_only_final"] = {v: round(1 - out["variants"][v]["final"]["log_loss"] / out["variants"]["z_only"]["final"]["log_loss"], 4) for v in out["variants"] if v != "z_only"}
    return out


def gate(result: dict, level: float) -> dict:
    """May a touch probability for ``level`` be shown? Needs enough events at that distance, calibration there, and a better score than climatology."""
    t = result.get("touch") or {}
    live = next((x for x in t.get("live_levels", []) if abs(x["level"] - level) < 1e-6), None)
    reasons = []
    if live is None or not live["final_slice"].get("in_range"):
        reasons.append(f"the level is {live['z'] if live and live['z'] is not None else 'n/a'} daily-volatility units away: beyond the distances with enough history ({Z_GRID[0]}–{Z_GRID[-1]})")
    else:
        ev = live["final_slice"]["events"]
        if ev < GATE["min_events_in_band"]:
            reasons.append(f"only {ev} historical events near that distance in the final out-of-sample slice (need {GATE['min_events_in_band']})")
        gap = live["final_slice"].get("gap")
        if gap is not None and gap > GATE["max_calibration_gap"]:
            reasons.append(f"calibration gap {gap:.3f} near that distance exceeds {GATE['max_calibration_gap']}")
    bs = t.get("brier_skill_vs_climatology_final")
    if bs is None or bs <= 0:
        reasons.append("the model does not beat climatology (Brier score) on the final slice")
    return {"display": not reasons, "reasons": reasons}


# ---------------------------------------------------------------------------------------------------------------- endpoint-based alert rule
def endpoint_evaluate(close: pd.Series, df_drivers: pd.DataFrame, levels: list[float]) -> dict:
    """Precision/recall of the heads-up RULE ('the 1-session point forecast is >= L') against the event 'next settlement >= L', chronologically.

    Compares the existing gold-only features with the bounded market-driver version on identical origins. Counts are reported as they are:
    a fixed absolute level is rarely crossed in a ten-year history, so absolute-level counts are small and say so.
    """
    warnings.filterwarnings("ignore")
    ds = rd.build_dataset(close, 1, df_drivers)
    gold = list(rd.FEATURE_NAMES)
    drv = gold + ["d_" + c for c in drivers.ALL_DRIVER_FEATURES]
    wf = {"gold": rd.walk_forward(ds, gold, "ridge", 1), "gold_drivers": rd.walk_forward(ds, drv, "ridge", 1)}
    ok = wf["gold"]["ok"] & wf["gold_drivers"]["ok"]
    idx = np.where(ok)[0]
    mid = idx[len(idx) // 2]
    p0, ph, vol = ds["p0"], ds["ph"], ds["vol60"]
    out: dict = {"event": ENDPOINT_DEF, "rule": "alert if the 1-session point forecast >= level", "n_origins": int(len(idx)), "oos": [str(ds["dates"][idx[0]].date()), str(ds["dates"][idx[-1]].date())],
                 "selection_slice_end": str(ds["dates"][mid].date()), "lead_time": {"hours": 24, "note": "issued at one 13:30 settlement for the next 13:30 settlement"}, "by_z": {}, "absolute": {}}
    for name, w in wf.items():
        pred_price = p0 * np.exp(w["pred"])
        for label, rows in (("selection", idx[idx < mid]), ("final", idx[idx >= mid])):
            for z in (0.5, 1.0, 1.5, 2.0):
                L = p0[rows] * np.exp(z * vol[rows])
                a, e = pred_price[rows] >= L, ph[rows] >= L
                out["by_z"].setdefault(str(z), {}).setdefault(name, {})[label] = {k: v for k, v in confusion(a.astype(float), e, 0.5).items() if k != "cut"}
        rows = idx
        for L in levels:
            below = p0[rows] < L
            a, e = (pred_price[rows] >= L)[below], (ph[rows] >= L)[below]
            c = confusion(a.astype(float), e, 0.5)
            out["absolute"].setdefault(f"{L:g}", {})[name] = {"sessions_below_level": int(below.sum()), **{k: v for k, v in c.items() if k != "cut"}}
    return out


# ---------------------------------------------------------------------------------------------------------------- run / persist
def _path():
    return config.DATA_DIR / "threshold_research.json"


def run(close: pd.Series, ohlc: pd.DataFrame, df_drivers: pd.DataFrame | None, levels: list[float], data_through: str | None = None) -> dict:
    warnings.filterwarnings("ignore")
    t0 = time.time()
    ds = build(close, ohlc["high"], df_drivers)
    res: dict = {"ran_at": datetime.utcnow().isoformat(timespec="seconds") + "Z", "data_through": data_through or str(close.index[-1].date()), "levels": levels,
                 "touch_event": EVENT_DEF, "lead_time": LEAD_TIME, "gate_rule": GATE, "touch": run_target(ds, "touch", levels)}
    res["endpoint"] = endpoint_evaluate(close, df_drivers, levels) if df_drivers is not None else None
    res["gates"] = {f"{L:g}": gate(res, L) for L in levels}
    res["selected_variant"] = res["touch"]["selected"]
    res["status"] = "displayed" if any(g["display"] for g in res["gates"].values()) else "experimental"
    res["duration_s"] = round(time.time() - t0, 1)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(res, indent=1, default=str))
    return res


def latest() -> dict | None:
    try:
        return json.loads(_path().read_text())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------------------------- forward record
def _touch_id(origin: str, level: float, model: str) -> str:
    return f"{origin}|{level:g}|{model}"


def record_forward(close: pd.Series, ohlc: pd.DataFrame, df_drivers: pd.DataFrame | None, levels: list[float], research: dict | None, now: float | None = None) -> dict:
    """Write today's touch probabilities BEFORE their outcome exists. Idempotent per (origin, level, model). Shown only if the gate passed."""
    now = now or time.time()
    research = research or latest()
    if research is None:
        return {"recorded": 0, "why": "no research run yet"}
    ds = build(close, ohlc["high"], df_drivers)
    if ds["x_now"] is None or not ds["sigma_now"]:
        return {"recorded": 0, "why": "latest inputs incomplete"}
    variant = research.get("selected_variant", "gold")
    cols = VARIANTS[variant]
    try:
        model = _fit(ds, np.arange(len(ds["pos"])), cols, "touch")
    except ValueError as exc:
        return {"recorded": 0, "why": str(exc)}
    origin = ds["now_date"]
    target = sessions.next_sessions(origin, 1)[0]
    n = 0
    for L in levels:
        z = float(np.log(L / ds["price_now"]) / ds["sigma_now"])
        if z <= 0:
            continue
        row = np.column_stack([np.array([z]), np.array([z ** 2])] + [np.array([float(ds["x_now"][c])]) for c in cols])
        p = float(model.predict_proba(row)[0, 1])
        shown = int((research.get("gates") or {}).get(f"{L:g}", {}).get("display", False))
        cur = db.connect().execute("INSERT OR IGNORE INTO touch_forecasts (id, issued_at, origin_date, target_date, level, z, probability, model, displayed, event_def) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (_touch_id(str(origin.date()), L, variant), now, str(origin.date()), str(pd.Timestamp(target).date()), L, z, p, variant, shown, EVENT_DEF))
        n += cur.rowcount
    return {"recorded": n, "origin": str(origin.date())}


def mature_forward(ohlc: pd.DataFrame, now: float | None = None) -> int:
    now = now or time.time()
    n = 0
    for r in db.rows("SELECT f.* FROM touch_forecasts f LEFT JOIN touch_outcomes o ON o.forecast_id=f.id WHERE o.forecast_id IS NULL"):
        d = pd.Timestamp(r["target_date"])
        if d in ohlc.index:
            h = float(ohlc.loc[d, "high"])
            db.connect().execute("INSERT OR IGNORE INTO touch_outcomes (forecast_id, actual_high, hit, scored_at) VALUES (?,?,?,?)", (r["id"], h, int(h >= r["level"]), now))
            n += 1
    return n


def forward_summary() -> dict:
    rows = db.rows("SELECT f.*, o.actual_high, o.hit FROM touch_forecasts f LEFT JOIN touch_outcomes o ON o.forecast_id=f.id ORDER BY f.issued_at DESC")
    scored = [r for r in rows if r["hit"] is not None]
    return {"recorded": len(rows), "scored": len(scored), "hits": int(sum(r["hit"] for r in scored)),
            "mean_probability_scored": round(float(np.mean([r["probability"] for r in scored])), 4) if scored else None,
            "latest": [{k: r[k] for k in ("origin_date", "target_date", "level", "z", "probability", "model", "displayed")} for r in rows[:4]]}


def panel() -> dict:
    """For the Alerts panel: whether a touch probability may be shown, and why not."""
    r = latest()
    if r is None:
        return {"status": "experimental", "reason": "The touch research has not run yet.", "forward": forward_summary()}
    shown = {k: g for k, g in r["gates"].items()}
    return {"status": r["status"], "selected_variant": r["selected_variant"], "data_through": r["data_through"], "event": r["touch_event"], "lead_time": r["lead_time"],
            "gates": shown, "forward": forward_summary(), "live_levels": r["touch"]["live_levels"], "variants": {v: {"final": d["final"], "selection": d["selection"]} for v, d in r["touch"]["variants"].items()},
            "brier_skill_vs_climatology_final": r["touch"]["brier_skill_vs_climatology_final"], "improvement_vs_z_only_final": r["touch"]["improvement_vs_z_only_final"],
            "periods": {"selection": r["touch"]["selection"], "final": r["touch"]["final"]}, "endpoint": r.get("endpoint")}
