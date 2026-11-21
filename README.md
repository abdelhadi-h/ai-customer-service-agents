# AI Customer-Service & Order Agents (WhatsApp / Instagram)

Two implementations of the same idea: an AI sales agent that answers customers on WhatsApp (and Instagram), collects orders, validates them with rules that do **not** depend on the AI, and hands off to a human when needed.

| Implementation | Stack | Folder |
|---|---|---|
| No-code / low-code | n8n, WhatsApp Cloud API, Google Sheets, Claude | [`n8n/`](n8n) |
| Code | Python, Flask, SQLite, Claude, Gemini | [`python/`](python) |

> Author: [Abdelhadi Habibi](https://github.com/abdelhadi-h) - [LinkedIn](https://www.linkedin.com/in/abdelhadi-habibi/)

## 1. n8n workflow (`n8n/whatsapp-sales-agent.json`)

39 nodes. Flow:

1. **Webhook (GET/POST)** - Meta verification with a verify token, then incoming WhatsApp events.
2. **Normalize Events** - converts the Meta payload into clean items; loops over each event.
3. **Context** - reads products and the customer's state from Google Sheets and builds the AI context.
4. **Bot Paused?** - if a human took over, the bot stays silent.
5. **AI Sales Agent** - Claude with chat memory and a calculator tool.
6. **Parse AI Response** - rules enforced in code, not by the AI:
   - no order before a double confirmation
   - prices and totals computed in code
   - size, color, stock and phone validated
   - human handoff pauses the bot
7. **Route** - send reply / append order / handoff.
8. **Send WhatsApp Reply** - handles error 131047 (24-hour window closed) and logs the failure.
9. **Owner alerts** - approved WhatsApp template messages, with an optional Telegram fallback; failed alerts are logged.

**Error workflow** (`n8n/error-alert-workflow.json`): an Error Trigger that alerts the owner on WhatsApp (template `workflow_error_alert`), logs every failure to a `WORKFLOW_ERRORS` sheet, and can fall back to Telegram.

### Setup

1. Import both JSON files into n8n.
2. Create a Google Sheet with the tabs the workflow uses (products, customers, orders, messages, `WORKFLOW_ERRORS`). Put its ID in the `Config` node (`cfg_sheet_id`, currently `YOUR_GOOGLE_SHEET_ID`).
3. In the `Config` node set: `cfg_phone_number_id`, `cfg_owner_wa_id` (digits only, comma-separated for several staff), `cfg_business_name`, `cfg_use_telegram_fallback`.
4. In `Verify Token OK?` replace `REPLACE_WITH_VERIFY_TOKEN` with the same value you set in Meta.
5. Create credentials in n8n: Google Sheets OAuth2, Anthropic, WhatsApp API (permanent System User token + WABA ID). Telegram only for the optional fallback.
6. Meta App > WhatsApp > Configuration: callback URL `https://YOUR-N8N/webhook/fb-sales-agent`, same verify token, subscribe to `messages`.
7. Create and get approved the template `workflow_error_alert` (4 body parameters: workflow, node, message, execution URL).
8. In the main workflow settings, set the error workflow to the one you imported.

Known limit: chat memory is in-process (Simple Memory) and resets when n8n restarts. Order state lives in the sheet and survives.

## 2. Python agent (`python/shop_agent.py`)

A Flask service for WhatsApp and Instagram.

- Understands text, voice, images, video, location, contacts, shared posts and story replies; replies in the customer's language (Algerian Arabic, French, Arabic).
- **Durable inbox (SQLite, WAL)**: messages are stored before processing, so a crash or restart does not lose them. Unfinished items return to `pending` on startup.
- **Duplicate prevention**: unique message ID per incoming message.
- **Debounce**: waits a few seconds so a customer can finish several messages before the agent replies.
- **Order creation with validation**: product, color, size, quantity, Algerian phone format, wilaya and delivery fee are checked in code.
- **Human takeover**: the bot pauses for a configurable time.
- **Webhook signature check** (HMAC SHA-256) when an app secret is set.
- **Monitoring**: `/health` endpoint and alerts to the owner on Telegram when messages are stuck or the worker loop stops.
- Retries on send, and a worker pool for parallel conversations.

### Run

```bash
cd python
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in your keys (never commit .env)
cp shop.example.json shop.json
cp catalog.example.json catalog.json   # replace with real products and remove "_example"
set -a && source .env && set +a
python shop_agent.py run
```

Other commands: `python shop_agent.py orders | flags | learn --dir chats | resume <ukey>`.

In production (`ENV=production`) the service refuses to start while `catalog.json` still contains `_example`.

## Security

- No secrets are stored in this repository. Keys are read from environment variables (`python/.env.example` lists them).
- The n8n exports were cleaned: Google Sheet ID, credential IDs and instance ID were removed. Re-create credentials after importing.
- Do not commit `.env`, `*.db`, real customer chats or real product data.

## Status

> Fill in honestly before publishing: e.g. "Built and tested with test accounts" or "Used in production for one shop since <month year>".

## License

MIT
