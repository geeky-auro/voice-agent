# Kirana voice agent

A WhatsApp assistant that keeps stock for a small shop. The shopkeeper sends a
voice note in their own mix of Hindi and English:

> 🎙️ "10 packet Maggi aaye, 3 bik gaye"

and gets back:

> 🎙️ _10 packet Maggi aaye, 3 bik gaye_
>
> Ho gaya ✅ Maggi: 10 aaye, 3 bike. Ab *7 packet* bacha hai.

It also warns when something is running low (`⚠️ Maggi sirf 2 packet bacha hai`)
and answers questions like *"aaj kitna becha?"*, *"Parle-G kitna bacha hai?"*,
*"is hafte ki bikri batao"* or *"galat ho gaya, cancel karo"*.

## How it works

```
WhatsApp voice note
   │  (Meta webhook → POST /webhook, acknowledged immediately)
   ▼
download OGG/Opus ──► Sarvam AI saaras:v4, mode=codemix ──► "10 packet Maggi aaye, 3 bik gaye"
                       (shop's product names sent as keyterms)
   ▼
Claude + tools ──► record_stock_in · record_sale · get_stock · low_stock
                   sales_report · update_product · undo_last
   ▼                     │
SQLite (per-shop stock, every movement logged)
   ▼
WhatsApp text reply (echoes what it heard, so mistakes are easy to spot)
```

| File | What it does |
|---|---|
| `app/main.py` | FastAPI webhook: verification handshake, signature check, dedupe, background processing |
| `app/whatsapp.py` | WhatsApp Cloud API: download media, send text, read receipts |
| `app/stt.py` | Speech-to-text via Sarvam AI (Indian languages, code-mixed speech) |
| `app/agent.py` | System prompt, tool definitions, Claude tool-use loop |
| `app/inventory.py` | Inventory logic: fuzzy product matching, low-stock warnings, sales reports, undo |
| `app/db.py` | SQLite schema, short per-shop chat memory |
| `cli.py` | Chat with the agent in a terminal, no WhatsApp needed |

Design choices:

- **Facts come from tools, not from the model.** Stock and sales numbers are
  computed in SQL; Claude only interprets the message and words the reply.
- **Every change is a logged movement** (`in` / `sale` / `adjust`), so reports are
  exact and "undo" reverses everything one message did.
- **One phone number = one shop**, so one deployment can serve several shops.
- **Sales without a price still count.** Quantities are always tracked; rupee
  totals appear once a price is set ("Maggi 14 rupaye ka hai").

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in ANTHROPIC_API_KEY at least
pytest                      # offline tests, no keys needed
python cli.py -v            # chat by typing
python cli.py --audio note.ogg   # try a real voice note (needs SARVAM_API_KEY)
```

## Connect WhatsApp

1. In the [Meta developer dashboard](https://developers.facebook.com/apps), create
   an app and add the **WhatsApp** product. Under *API Setup*, note the
   **Phone number ID** and create an access token. Use a permanent System User
   token for anything beyond a quick test, because the temporary one expires in 24h.
2. Add the shopkeeper's number as a test recipient (while in development mode).
3. Fill `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`,
   `WHATSAPP_VERIFY_TOKEN` and `ALLOWED_NUMBERS` in `.env`.
4. Start the server and expose it over HTTPS:
   ```bash
   uvicorn app.main:app --port 8000
   ngrok http 8000            # or cloudflared, or deploy anywhere with HTTPS
   ```
5. In *WhatsApp → Configuration*, set the callback URL to
   `https://<your-host>/webhook`, the verify token to your `WHATSAPP_VERIFY_TOKEN`,
   and subscribe to the **messages** field.
6. Send a voice note from the shopkeeper's phone.

WhatsApp only allows free-form replies within 24 hours of the user's last
message, which is always the case here because the bot only replies. Proactive
alerts (e.g. a morning low-stock summary) will need an approved message template.

## Status / next steps

- [x] Voice + text messages, Hinglish STT, stock in/out, low-stock warnings,
      sales reports, prices, aliases, stock counts, undo
- [x] Offline tests for inventory, the agent loop (fake Claude) and the webhook
- [ ] Test with the real shopkeeper and collect transcripts that go wrong → turn them into an eval set
- [ ] Morning summary / low-stock alert via a message template
- [ ] Voice replies (TTS) for shopkeepers who prefer listening
- [ ] Deploy (Fly.io / Railway / a small VM) with a persistent volume for `shop.db`
