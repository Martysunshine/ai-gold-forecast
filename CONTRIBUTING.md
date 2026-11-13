# Contributing to ai-gold-forecast

For anyone who wants to run, study or change this project. It covers setting up a machine, keeping keys private, proposing a change, and the few rules that protect the honesty of the results.

Read first: [README.md](README.md) for what the project is, and [docs/how-it-works.md](docs/how-it-works.md) for how a forecast is made.

## 1. Set up a machine (once)

You need Git, Python 3.11 or newer (the project is developed on 3.12) and Node.js 20 or newer.

```bash
git clone https://github.com/Martysunshine/ai-gold-forecast.git
cd ai-gold-forecast
./start.sh          # Windows: start.cmd
```

The launcher creates `backend/.venv`, installs the pinned packages, builds the app and opens it at http://localhost:8742. Stop it with Ctrl+C.

Then install the test tools and run the checks. All three must pass before and after your change:

| Check | Windows | macOS and Linux |
| --- | --- | --- |
| Backend tests | `backend\.venv\Scripts\python -m pip install -r backend\requirements-dev.txt` then, in `backend\`, `.venv\Scripts\python -m pytest -q` | `cd backend && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q` |
| Types and build | `npm run build` in `frontend\` | `cd frontend && npm run build` |
| Browser checks | `e2e\run.cmd` | `./e2e/run.sh` |

The browser checks need Chrome or Edge and an internet connection. They start three temporary servers on ports 8743 to 8745 and stop them afterwards.

## 2. Keys: everyone uses their own

- The app needs **no key**. The only optional ones are Alpaca market-data keys for the GLD tab.
- Use paper or market-data keys only. The app calls one read-only endpoint and never an order or account endpoint.
- Keys go in `.env` in the repository root, which git ignores. They never go into git, a config file, an issue, a pull request or an AI assistant chat.
- If a key is ever committed or pasted somewhere public, **revoke it at the provider at once**. Removing it from git history is not enough. If this repository is affected, report it privately ([SECURITY.md](SECURITY.md)).

## 3. Git workflow

- `main` must always pass every check. Work on a branch named `<name>/<topic>` and open a pull request.
- For a larger change, open an issue first and agree on the approach.
- One change per pull request. Say what changed, why, and what you ran to check it.
- Commit messages: a summary line of 72 characters or fewer in the imperative ("Add ...", "Fix ..."), then a body that explains the reason.
- **No GitHub Actions, CI workflows, scheduled jobs or any `.github/` configuration** without the owner's agreement. Checks are run locally.
- By opening a pull request you agree that your contribution is released under the [MIT licence](LICENSE).
- After changing how the app looks, retake the screenshots: `SHOTS=./docs/screenshots ./e2e/run.sh shots.mjs` (Windows: `set SHOTS=docs\screenshots` then `e2e\run.cmd -Script shots.mjs`).

## 4. Rules that protect the results

These are what make the grade trustworthy. A change that breaks one will not be merged.

- **The test period is scored once and chooses nothing.** Model choice and range calibration use the validation period only. Tests in `backend/tests/test_model.py` fail if either starts to depend on test prices.
- **Features use past prices only.** Nothing computed for a day may use a later price.
- **Model settings are fixed in advance.** Do not tune them against the test period. A new model or setting is compared on validation, and the test result is reported whatever it is.
- **The verdict is computed.** "No demonstrated advantage" comes from the 2% rule in `backend/app/model.py`. Do not hard-code it, soften it or hide it.
- **No invented data.** Missing prices are never filled in, and there is no sample-data fallback. Synthetic prices exist only inside tests.
- **Numbers on screen come from the server.** Do not write a result into the interface or the documentation as fixed text when it can be computed.
- **Forecast only.** No order, account or position endpoint of any broker is called.

## 5. Things nobody does without the owner's agreement

- Change the repository's visibility, add a licence, or publish a release.
- Add a paid service, a hosted deployment or automation that runs on a schedule.
- Add a dependency that sends data to a third party.
