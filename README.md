<h1 align="center">AI GOLD FORECAST</h1>
<h3 align="center">One metal. Four horizons. A model that learns every day and shows its work.</h3>

<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white">
  <img alt="scikit-learn" src="https://img.shields.io/badge/scikit--learn-F7931E?logo=scikitlearn&logoColor=white">
  <img alt="React + TypeScript" src="https://img.shields.io/badge/React-TypeScript-3178C6?logo=typescript&logoColor=white">
  <img src="https://img.shields.io/badge/status-in%20active%20development-8957E5" alt="In active development">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT licence"></a>
</p>

> **This is an ongoing project.** It is still being developed and will keep getting updates, so the models, the screens and the results on this page will change.

Forecasts for COMEX gold futures (GC=F): the next settlement, and 1, 5 and 20 sessions ahead, each with an 80% range and the range's measured coverage. Every forecast is stored before its outcome exists, in a ledger that refuses edits and deletes, and scored once the day has closed.

## How it learns, every day

A background worker (`python -m app.worker run`) runs the loop on its own, with the browser and web server closed:

| When | What |
|---|---|
| Every 5 minutes | When a session has closed (after 17:00 New York, once the daily close agrees with the 13:25 settlement-time price): check the data, re-evaluate every candidate model, and issue the next forecasts |
| 23:30 UTC every day | The full cycle: collect, validate, score matured forecasts, walk-forward-evaluate every candidate, decide, publish |
| On start | A catch-up cycle when the last successful learning run is more than 26 hours old |
| Every 30 minutes, hourly | Score newly matured forecasts; the next-settlement nowcast |

**Candidates and the champion.** For each horizon about a dozen candidate models (ridge, gradient boosting and random forests on gold prices, market drivers, price patterns and an experimental Fibonacci group) are refitted and scored on the same dates against "the price stays put". One of them, the champion, makes the forecast; the others are recorded in shadow.

**Promotion needs proof twice.** A challenger replaces the champion only when it beats both the unchanged price and the champion on the walk-forward test (at least 2%, in two of three periods, after a multiple-testing correction) **and** on its own live record (at least 30 independent blocks of matured forecasts). At most one promotion per horizon in 30 days. A promoted champion that falls 5% behind the unchanged price is rolled back automatically. Bad data (wrong units, stale feed, schema change) blocks training for the day and leaves the working models alone.

Details: [docs/GUIDE.md](docs/GUIDE.md#how-learning-works).

## Run it

```bash
start.cmd                        # Windows: installs on first run, builds, opens http://localhost:8742
./start.sh                       # macOS / Linux
```

The background worker:

```bash
cd backend
.venv\Scripts\python -m app.worker run        # Windows (keep the window open)
./scripts/worker.sh install                   # macOS: a launchd agent that starts at login
.venv\Scripts\python -m app.worker status     # what ran, when, and which model is in charge
.venv\Scripts\python -m app.worker cycles     # the daily-cycle record
```

Only one worker runs at a time (a lock); the web app starts its own only when no other worker is running.

## The forecast as a file

```bash
cd backend
.venv\Scripts\python -m app.export --out forecast.json
```

Writes the champion forecast of each horizon from the ledger as one JSON document (schema `ai-gold-forecast.forecast/1`): the call, its range, the period it applies to (`valid_from` to `valid_until`, the end of the target day in New York), the model's walk-forward and live record, and a `learning` block saying when the model last learned. The export never fits a model and never contacts a provider, so every run returns the same call for a session. `evaluation.advantage` is `true` only while the champion is a model the loop promoted.

## Honest status

No model beats "the price stays put" at any horizon tested yet, and live forward records are only beginning. The champions today are the seeded models; a promotion needs weeks of matured live forecasts first. The app and this file say so rather than presenting a forecast as an edge. [See the numbers](docs/GUIDE.md#current-measured-results).

> [!WARNING]
> This is a research project, not financial advice. Nothing here claims or implies profitable trading. Market data comes from Yahoo Finance, FRED and gold-api.com and, optionally, Alpaca; headlines come from Google News. Each source has its own terms of use.

## Tests

```bash
cd backend
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```

## Licence

**Licence.** The work by Martin Matysek in this repository is released under the [MIT licence](LICENSE). Code written by other contributors remains theirs and is covered by that licence only where they have agreed to it. That includes the first commits in the history, and most of the learning version added in November 2026 (the forecast ledger, the daily worker and the promotion rules), which another contributor wrote; the work done on it here is mainly the Windows support, the export for other programs and this README. Third-party packages remain under their own licences. See [SECURITY.md](SECURITY.md) for reporting a problem.
