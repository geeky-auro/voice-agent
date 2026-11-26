"""Speech-to-text for mixed Hindi/English (and other Indian languages) via Sarvam AI.

`saaras:v4` in `codemix` mode returns Hinglish the way it was spoken:
"10 packet Maggi aaye, 3 bik gaye". It also takes `keyterms`; we pass the
shop's own product names so brands are spelled the way the inventory knows them.
WhatsApp voice notes are OGG/Opus, which Sarvam accepts directly.
"""

from __future__ import annotations

import json
import logging

import httpx

from . import config

log = logging.getLogger(__name__)

SARVAM_URL = "https://api.sarvam.ai/speech-to-text"


class STTError(Exception):
    pass


def transcribe(audio: bytes, mime_type: str = "audio/ogg", keyterms: list[str] | None = None) -> str:
    if not config.SARVAM_API_KEY:
        raise STTError("SARVAM_API_KEY is not set")
    ext = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac",
           "audio/amr": "amr", "audio/wav": "wav"}.get(mime_type.split(";")[0].strip(), "ogg")
    data = {"model": config.SARVAM_MODEL, "mode": config.SARVAM_MODE,
            "language_code": config.SARVAM_LANGUAGE}
    if keyterms:
        data["keyterms"] = json.dumps(keyterms[:50], ensure_ascii=False)
    resp = httpx.post(
        SARVAM_URL,
        headers={"api-subscription-key": config.SARVAM_API_KEY},
        files={"file": (f"voice.{ext}", audio, mime_type.split(";")[0].strip())},
        data=data,
        timeout=60,
    )
    if resp.status_code != 200:
        raise STTError(f"Sarvam STT {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    log.info("STT (%s, p=%s): %s", body.get("language_code"), body.get("language_probability"),
             body.get("transcript"))
    return (body.get("transcript") or "").strip()
