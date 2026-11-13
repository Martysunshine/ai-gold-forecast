# Security policy

This app forecasts gold prices. It places no orders, holds no account and needs no API key to run. Its server binds to `127.0.0.1`, so it answers only on the machine it runs on. Security reports are still welcome.

## Reporting a vulnerability

Report it privately through GitHub: open the **Security** tab of this repository and choose **Report a vulnerability**. Please do not open a public issue for a vulnerability or a leaked key.

Useful to include: what you found, the file and line if you have them, how to reproduce it, and what an attacker could do with it.

You can expect an acknowledgement within a week. This is a small project, so fixes are made on a best-effort basis, on the `main` branch only.

## What counts

- An Alpaca key value that reaches an API response, a log, an error message, the interface or a cached file. Tests in [`backend/tests/test_alpaca.py`](backend/tests/test_alpaca.py) check the responses.
- Any call to an order, account or position endpoint of a broker.
- A way to make the server answer on an address other than the local machine by default.
- An uploaded CSV that can write outside `backend/data/`, run code, or replace the active data without passing validation.
- A way to make the app show a forecast for data other than the data on screen. Results are tied to a `data_key` for this reason.

## If you find a secret in this repository

Report it privately as above. Removing a key from git history is not enough; the key has to be revoked at the provider.

## Using this project safely yourself

- The optional GLD tab needs Alpaca keys. Use your **own paper or market-data keys**, never a live trading key.
- Keys live in your local `.env`, which git ignores. See [CONTRIBUTING.md](CONTRIBUTING.md) section 2.
- If you paste a key anywhere public, revoke it at the provider straight away.
- The forecasts are model outputs, not trading signals. The app's own test shows no advantage over assuming the price will not change.
