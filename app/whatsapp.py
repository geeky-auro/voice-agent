"""Thin client for the WhatsApp Cloud API (Meta Graph API)."""

from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

from . import config

log = logging.getLogger(__name__)


def _graph(path: str) -> str:
    return f"https://graph.facebook.com/{config.GRAPH_API_VERSION}/{path}"


def _auth() -> dict:
    return {"Authorization": f"Bearer {config.WHATSAPP_TOKEN}"}


def verify_signature(raw_body: bytes, header: str | None) -> bool:
    """Check X-Hub-Signature-256. Skipped (allowed) only when no app secret is configured."""
    if not config.WHATSAPP_APP_SECRET:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(config.WHATSAPP_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def download_media(media_id: str) -> tuple[bytes, str]:
    """Voice notes arrive as a media id: look up its URL, then fetch the bytes."""
    with httpx.Client(timeout=30, headers=_auth()) as c:
        meta = c.get(_graph(media_id))
        meta.raise_for_status()
        info = meta.json()
        audio = c.get(info["url"])
        audio.raise_for_status()
    return audio.content, info.get("mime_type", "audio/ogg")


def send_text(to: str, body: str, reply_to: str | None = None) -> None:
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": body[:4096], "preview_url": False},
    }
    if reply_to:
        payload["context"] = {"message_id": reply_to}
    resp = httpx.post(_graph(f"{config.WHATSAPP_PHONE_NUMBER_ID}/messages"),
                      headers=_auth(), json=payload, timeout=30)
    if resp.status_code >= 300:
        log.error("WhatsApp send failed %s: %s", resp.status_code, resp.text[:500])


def mark_read(message_id: str) -> None:
    """Blue ticks, so the shopkeeper knows the note was received while we work."""
    try:
        httpx.post(_graph(f"{config.WHATSAPP_PHONE_NUMBER_ID}/messages"), headers=_auth(),
                   json={"messaging_product": "whatsapp", "status": "read",
                         "message_id": message_id}, timeout=10)
    except httpx.HTTPError as e:
        log.warning("mark_read failed: %s", e)


def extract_messages(payload: dict) -> list[dict]:
    """Flatten a webhook payload into the messages it carries (ignores status updates)."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []) or []:
                out.append(msg)
    return out
