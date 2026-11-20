# n8n: morning outlook and SMS delivery

n8n does two jobs: **scheduling** (the morning outlook) and **SMS delivery** (through Twilio). The forecasts, thresholds and wording all
come from this app. The app never holds your phone number or Twilio credentials; n8n does.

## The one thing a hosted schedule cannot do

A cloud schedule cannot call a Mac that is asleep, and it cannot create a forecast that was never made. So the app does **not** wait for
n8n to call it. Instead:

```
 Mac awake                                    hosted n8n (always on)                         your phone
 worker issues forecast ──push snapshot──▶  stores forecast + morning text + schedule ─┐
 price crosses a level ───push alert─────▶  texts it, records provider acceptance      ├──▶ SMS
                                              every 5 min: is it 07:30 Berlin? ─────────┘
 Mac wakes ◀──pull delivery receipts──────  receipts from Twilio (accepted ≠ delivered)
```

* **Durable forecast hand-off.** Whenever a forecast, a setting or the schedule changes, the app POSTs a snapshot to n8n. n8n keeps it in
  workflow static data. The 07:30 job runs from that stored copy, so it works while the Mac is off.
* **Staleness is decided at send time, in n8n.** The snapshot carries a `valid_until` (the earlier of the target time and 40 h after issue).
  After that, the SMS says the forecast has expired and is not repeated as new. If nothing was ever received, it says that.
* **What stays impossible:** if the Mac is off when a forecast should have been issued, no forecast exists and the SMS says so. Price alerts
  need the Mac awake too, because it is the Mac that watches the quote.
* **Review links** are served by n8n from the stored page (`/webhook/gold-review?t=…`), so they open even while the Mac sleeps. The quote on
  that page is as of the moment the page was stored and says so. The Mac also serves a live version at `/review?t=…` while it is on.

## Setup (once)

1. **n8n**: n8n Cloud, or a self-hosted instance with a public HTTPS URL (required: Twilio and your phone must reach the webhooks).
2. **Twilio**: an account, an SMS-capable number, and (for a trial account) your own number verified.
3. In n8n create two credentials:
   * *Header Auth* named **Gold shared secret**: name `X-Gold-Secret`, value = a long random string.
   * *Twilio API* named **Twilio account**.
4. Import `n8n/gold-alerts.workflow.json`. Open the Code nodes **Build alert SMS**, **Decide morning** and **Record receipt** and replace the
   `CFG` block (same three values in each): your number, your Twilio number, the public status-callback URL and one random key.
   Assign the two credentials to the Webhook and Twilio nodes. **Activate** the workflow (static data persists only for active workflows).
5. In the repository `.env`:
   ```
   GOLD_ALERTS_MODE=live
   N8N_WEBHOOK_BASE=https://YOUR-INSTANCE.app.n8n.cloud/webhook
   N8N_AUTH_SECRET=<the same random string>
   ```
6. Open the Alerts panel: the hand-off line should read `ok`. Until then it reads `simulated` or `not configured`, and nothing is sent.

## Delivery states

`pending → handed_off → accepted (provider took it) → delivered / undelivered / failed`, or `expired` (too old to send). **Accepted is not
delivered**; Twilio's status callback upgrades it, and the app pulls those receipts (also the morning ones) when it is awake.

## Tests

```bash
node --test n8n/test/logic.test.mjs n8n/test/workflow.test.mjs   # the real Code-node source, run against a stub of n8n's globals
python3 n8n/build_workflow.py                                      # regenerate the workflow after editing lib/logic.js
```

These tests exercise the node logic (schedule window, DST, stale vs fresh text, once-per-day, retries, review tokens, receipts) with a fake
Twilio. **The workflow JSON has not been imported into a live n8n instance in this repository's verification**: node parameter formats can
differ slightly between n8n versions, so import it, check each node opens without warnings, and send one test.
