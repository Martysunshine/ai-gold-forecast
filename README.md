# Gold Outlook

A small, gold-only forecasting dashboard: historical prices, a simple Ridge-regression forecast for the next
**1, 5 and 20 trading sessions**, an honest backtest against a "price stays unchanged" baseline, and recent
gold headlines shown as separate context.

> **Not financial advice.** On real data the model currently does **not** beat the baseline (see
> [Validation results](#validation-results)). The app says so on screen. No accuracy is promised.

- **Backend:** Python, FastAPI, scikit-learn, pandas
- **Frontend:** React, TypeScript, Vite, Recharts
- **Instrument:** COMEX gold futures `GC=F` (front-month continuous), USD per troy ounce. This is a **futures** price, **not spot gold**.

## Run it

Requires Python 3.11+ and Node 20+.

```bash
# 1. Backend
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --port 8742

# 2. Frontend (new terminal)
cd frontend
npm install
npm run dev            # http://localhost:5173  (proxies /api to port 8742)
```

Single-process alternative (serves the built UI from FastAPI at http://127.0.0.1:8742):

```bash
cd frontend && npm install && npm run build && cd ../backend
.venv/bin/uvicorn app.main:app --port 8742
```

Port 8742 is arbitrary; if you change it, set `GOLD_API_URL=http://127.0.0.1:<port>` for `npm run dev`.
All settings are optional — see [.env.example](.env.example). There are no API keys and no secrets.

### Tests

```bash
cd backend && .venv/bin/python -m pytest -q
cd frontend && npm run build        # type-checks and builds
```

## Data provenance

| Item | Detail |
|---|---|
| Prices | Yahoo Finance public chart endpoint, symbol `GC=F`, 5 years, daily. Unofficial and unauthenticated; may be rate-limited or change without notice. |
| Caching | Last successful fetch saved to `backend/data/active.csv` (git-ignored); reused for 6 h. |
| In-progress session | If the latest daily bar is still forming, it is dropped; the latest value is the last *completed* close. |
| Failure handling | Provider fails → last cached real data (flagged) → otherwise an empty state asking for a CSV. **Synthetic data is never substituted.** |
| CSV upload | Needs a date column (`Date`, `Datetime`, …) and a price column (`Close`, `Adj Close`, `Price`, …); ≥400 rows. Validated for dates, positive numeric prices, duplicates, ordering, future dates and implausible (>30%) daily jumps. Units and instrument of an upload cannot be verified, and the UI labels it as user-supplied. |
| News | Google News RSS search results (four gold-related queries, last 14 days), de-duplicated, with publication dates and links to the original articles. Context only. |

## Model

- **Features** (all computed from prices up to and including day *t*): 5 lagged daily returns, 5/10/20/60-session momentum, price vs 10- and 50-session averages, 10-vs-50 average cross, 10/20/60-session rolling volatility.
- **Target:** forward log return over *h* sessions, converted back to a price. One `StandardScaler → Ridge(alpha=100)` per horizon. `alpha` is fixed a priori, not tuned.
- **Validation:** expanding-window walk-forward, ~63-session test blocks (~2 years minimum training history). Training rows whose target window reaches the test block are **purged**; the scaler is fitted on training rows only.
- **Baseline:** price unchanged (predicted return 0).
- **Metrics:** MAE in USD, directional accuracy (shown next to an "always up" reference), folds won vs baseline. Verdict rule fixed in advance: *beats* = MAE ≥2% lower **and** better in more than half the folds; *worse* = higher MAE; otherwise *no reliable edge*.
- **Range:** 80% interval from the 10th/90th percentile of pooled out-of-sample residuals.
- **Scenario control:** bullish/base/bearish shifts the forecast by ±0.5× the typical out-of-sample error. It is a user assumption, not learned, and not connected to the news.
- **News is not a model input.** No historical news features are implemented or validated.

## Validation results

Run on 13 Nov 2026 against live `GC=F` data (1,256 daily closes, 15 Nov 2021 → 12 Nov 2026), 11 folds, out-of-sample 13 Feb 2024 → 11 Nov 2026:

| Horizon | MAE model | MAE baseline | Change vs baseline | Directional acc. (always-up) | Folds won | Verdict |
|---|---|---|---|---|---|---|
| 1 session | $37.95 | $37.67 | +0.7% worse | 51% (56%) | 4/11 | Does not beat baseline |
| 5 sessions | $87.58 | $86.53 | +1.2% worse | 55% (61%) | 7/11 | Does not beat baseline |
| 20 sessions | $186.78 | $173.89 | +7.4% worse | 54% (65%) | 5/11 | Does not beat baseline |

These numbers change as data updates. They were not adjusted to reach any target. Gold rallied strongly over much of the test period, which is why a naive "always up" call scores higher on direction than the model.

Automated tests (41, all passing) cover: CSV/provider validation, cache and outage fallbacks (including that no synthetic data is returned), feature causality (changing future prices does not change past features), purge correctness (no training target window overlaps a test block), scaler fitted on training rows only, walk-forward predictions being independent of later prices, forecast generation, a random-walk sanity check (no material "edge" is found in pure noise), and the API contract and error codes.

## Limitations

- Gold is close to a random walk at these horizons; a Ridge model on past prices has little to exploit. Expect the baseline to be hard to beat.
- The 80% range comes from past errors and assumes the future resembles the test period; shocks and regime changes can fall outside it. Interval coverage is not separately back-tested.
- Multi-day test windows overlap, so out-of-sample errors are autocorrelated and effective sample sizes are smaller than the counts shown. Eleven folds is a small number to judge consistency.
- `GC=F` is a futures series, not spot, and contract rolls can introduce small jumps. Forecast dates skip weekends but not exchange holidays.
- Yahoo's endpoint is unofficial. The news tags are keyword matches on headlines — they show what is discussed, not sentiment or impact.
- No trading, execution, or portfolio features.

## Layout

```
backend/app/   data.py (provider, validation, cache)  features.py  model.py  news.py  main.py (API)
backend/tests/ validation, model/leakage, API tests
frontend/src/  App.tsx  components/  api.ts  styles.css
```

API: `GET /api/prices`, `POST /api/prices/refresh`, `POST /api/prices/upload`, `GET /api/forecast`, `GET /api/forecast/download` (CSV), `GET /api/news`, `GET /api/health`.
