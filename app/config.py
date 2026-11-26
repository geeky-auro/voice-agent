"""All settings come from environment variables (see .env.example)."""

import os

from dotenv import load_dotenv

load_dotenv()  # reads .env in the working directory, if present


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# Claude
CLAUDE_MODEL = _env("SHOP_AGENT_MODEL", "claude-opus-5-5")
# Short shop chats don't need deep reasoning; "low" keeps replies fast.
CLAUDE_EFFORT = _env("SHOP_AGENT_EFFORT", "low")

# Speech-to-text (Sarvam AI — built for Indian languages and code-mixed speech)
SARVAM_API_KEY = _env("SARVAM_API_KEY")
SARVAM_MODEL = _env("SARVAM_MODEL", "saaras:v4")
SARVAM_MODE = _env("SARVAM_MODE", "codemix")        # keeps English words in Latin script
SARVAM_LANGUAGE = _env("SARVAM_LANGUAGE", "unknown")  # auto-detect; or hi-IN, bn-IN, ...

# WhatsApp Cloud API (Meta)
WHATSAPP_TOKEN = _env("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = _env("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_VERIFY_TOKEN = _env("WHATSAPP_VERIFY_TOKEN", "change-me")
WHATSAPP_APP_SECRET = _env("WHATSAPP_APP_SECRET")   # enables webhook signature checks
GRAPH_API_VERSION = _env("GRAPH_API_VERSION", "v23.0")

# Comma-separated phone numbers allowed to use the bot (empty = anyone).
ALLOWED_NUMBERS = {n.strip() for n in (_env("ALLOWED_NUMBERS", "") or "").split(",") if n.strip()}

# App
DB_PATH = _env("DB_PATH", "shop.db")
TIMEZONE = _env("TIMEZONE", "Asia/Kolkata")
CHAT_MEMORY_TURNS = int(_env("CHAT_MEMORY_TURNS", "6"))
