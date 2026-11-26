"""FastAPI app: the WhatsApp webhook.

Meta expects a 200 within a few seconds and retries otherwise, so the webhook
acknowledges immediately and does the slow work (download, STT, Claude) in a
background task. Message ids are de-duplicated so retries are harmless.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response

from . import agent, config, db, inventory, stt, whatsapp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("shop-agent")

app = FastAPI(title="Kirana voice agent")

# One shopkeeper sending two notes quickly should have them applied in order.
_shop_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

UNSUPPORTED = "Abhi main sirf voice note aur text samajhta hoon 🙏"
STT_FAILED = "Voice note samajh nahi aaya, ek baar phir bhejiye ya likh dijiye."


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/webhook")
def verify(request: Request) -> Response:
    """One-time handshake when you register the webhook URL in the Meta dashboard."""
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == config.WHATSAPP_VERIFY_TOKEN:
        return Response(content=q.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status_code=403, detail="verification failed")


@app.post("/webhook")
async def receive(request: Request, background: BackgroundTasks) -> dict:
    raw = await request.body()
    if not whatsapp.verify_signature(raw, request.headers.get("x-hub-signature-256")):
        raise HTTPException(status_code=401, detail="bad signature")
    payload = json.loads(raw or b"{}")
    for msg in whatsapp.extract_messages(payload):
        if db.mark_seen(msg["id"]):
            background.add_task(process_message, msg)
    return {"ok": True}


def process_message(msg: dict) -> None:
    sender, msg_id, kind = msg.get("from", ""), msg["id"], msg.get("type")
    if config.ALLOWED_NUMBERS and sender not in config.ALLOWED_NUMBERS:
        log.info("ignoring message from unlisted number %s", sender)
        return
    whatsapp.mark_read(msg_id)

    with _shop_locks[sender]:
        try:
            heard = None
            if kind == "text":
                text = msg["text"]["body"]
            elif kind == "audio":
                audio, mime = whatsapp.download_media(msg["audio"]["id"])
                try:
                    text = stt.transcribe(audio, mime, keyterms=inventory.product_names(sender))
                except stt.STTError as e:
                    log.error("STT failed: %s", e)
                    text = ""
                if not text:
                    whatsapp.send_text(sender, STT_FAILED, reply_to=msg_id)
                    return
                heard = text
            else:
                whatsapp.send_text(sender, UNSUPPORTED, reply_to=msg_id)
                return

            reply = agent.handle_message(sender, text, message_id=msg_id)
            if heard:
                # Showing what we heard lets the shopkeeper catch transcription mistakes.
                reply = f"🎙️ _{heard}_\n\n{reply}"
            whatsapp.send_text(sender, reply, reply_to=msg_id)
        except Exception:
            log.exception("failed to process message %s", msg_id)
            whatsapp.send_text(sender, agent.FALLBACK_REPLY, reply_to=msg_id)
