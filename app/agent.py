"""The Claude side: system prompt, tool definitions, and the tool-use loop."""

from __future__ import annotations

import json
import logging

import anthropic

from . import config, db, inventory
from .inventory import ToolError

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the stock-keeping assistant for a small Indian kirana shop. The shopkeeper \
talks to you on WhatsApp, mostly through voice notes that have been transcribed for \
you. They speak the way shopkeepers do: Hindi (or another Indian language) mixed \
with English brand names and numbers, e.g. "10 packet Maggi aaye, 3 bik gaye" or \
"aaj kitna becha?".

How to read their messages:
- "aaye / aaya / aa gaye / mangwaye / liye / stock aaya" = new stock arrived -> record_stock_in.
- "bika / bik gaye / becha / bech diye / gaye / nikle" = sold -> record_sale.
- One message can contain both; record each part with the right tool.
- "kitna bacha / kitna hai / stock" = stock question -> get_stock or low_stock.
- "kitna becha / bikri / sale / kamai / galla" = sales question -> sales_report.
- "galat ho gaya / cancel / undo / wapas karo" = undo the last entry -> undo_last.
- Number words in any language are numbers: "das" = 10, "teen" = 3, "aadha" = 0.5, \
"darjan" = 12.
- Transcripts can be imperfect. Use product names exactly as the shop already \
knows them (check get_stock when unsure) instead of creating near-duplicates such \
as "Magi" next to "Maggi". Write product names in Latin script with normal brand \
spelling (Maggi, Parle-G, Surf Excel, Tata Salt).
- If something important is unclear (which product, how many), ask one short \
question instead of guessing. If the shopkeeper confirms a product you did not \
recognise, record it.

Facts come only from tools. Never state a stock level, sale figure or amount you \
did not get from a tool result in this conversation.

How to reply:
- Reply in the same language and script the shopkeeper used (Roman Hindi in, \
Roman Hindi out; Devanagari in, Devanagari out).
- This is WhatsApp: 1-4 short lines, no markdown headings or tables. Use *bold* \
sparingly for the key number. Use ₹ for money.
- After recording, confirm briefly what you recorded and the new stock.
- If a tool result contains warnings or LOW_STOCK, always mention it, e.g. \
"⚠️ Maggi sirf 2 packet bacha hai, order kar lo."
- If an amount is unknown because no price is set, give quantities and offer to \
save a price ("Maggi ka rate batao to paise bhi jod dunga").
"""


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


_ITEM_IN = _schema({
    "product": {"type": "string", "description": "Product name, e.g. 'Maggi'"},
    "qty": {"type": "number", "description": "How many units arrived"},
    "unit": {"type": "string", "description": "packet, kg, litre, bottle, piece... (new products only)"},
}, ["product", "qty"])

_ITEM_SALE = _schema({
    "product": {"type": "string"},
    "qty": {"type": "number", "description": "How many units were sold"},
    "amount": {"type": "number", "description": "Total rupees received for this line, only if the shopkeeper said it"},
}, ["product", "qty"])

_PERIOD = {"type": "string",
           "description": "today, yesterday, this_week, last_7_days, this_month, or a date YYYY-MM-DD"}

TOOLS = [
    {
        "name": "record_stock_in",
        "description": "Record new stock that arrived at the shop (purchase / delivery). "
                       "Creates the product if the shop has never stocked it before.",
        "input_schema": _schema({"items": {"type": "array", "items": _ITEM_IN, "minItems": 1}}, ["items"]),
    },
    {
        "name": "record_sale",
        "description": "Record items sold to customers. Reduces stock and returns warnings "
                       "for anything now running low. Fails for unknown products, with suggestions.",
        "input_schema": _schema({"items": {"type": "array", "items": _ITEM_SALE, "minItems": 1}}, ["items"]),
    },
    {
        "name": "get_stock",
        "description": "Current stock of one product, or of every product if 'product' is omitted.",
        "input_schema": _schema({"product": {"type": "string"}}, []),
    },
    {
        "name": "low_stock",
        "description": "List products at or below their low-stock level.",
        "input_schema": _schema({}, []),
    },
    {
        "name": "sales_report",
        "description": "Units sold (and rupees, where prices are known) for a period, "
                       "optionally for one product.",
        "input_schema": _schema({"period": _PERIOD, "product": {"type": "string"}}, ["period"]),
    },
    {
        "name": "update_product",
        "description": "Change a product's settings: selling price per unit, low-stock alert "
                       "level, unit, an alternative name (alias), a rename, or set the stock to "
                       "a counted number. Only pass the fields that change.",
        "input_schema": _schema({
            "product": {"type": "string"},
            "sell_price": {"type": "number", "description": "Rupees per unit"},
            "low_threshold": {"type": "number", "description": "Warn when stock is at or below this"},
            "unit": {"type": "string"},
            "set_stock": {"type": "number", "description": "Physically counted stock"},
            "add_alias": {"type": "string", "description": "Another name the shopkeeper uses"},
            "rename_to": {"type": "string"},
        }, ["product"]),
    },
    {
        "name": "undo_last",
        "description": "Reverse everything recorded by the shopkeeper's previous message.",
        "input_schema": _schema({}, []),
    },
]

_HANDLERS = {
    "record_stock_in": lambda shop, a: inventory.record_stock_in(shop, a["items"]),
    "record_sale": lambda shop, a: inventory.record_sale(shop, a["items"]),
    "get_stock": lambda shop, a: inventory.get_stock(shop, a.get("product")),
    "low_stock": lambda shop, a: inventory.low_stock(shop),
    "sales_report": lambda shop, a: inventory.sales_report(shop, a.get("period", "today"), a.get("product")),
    "update_product": lambda shop, a: inventory.update_product(
        shop, a["product"], **{k: v for k, v in a.items() if k != "product"}),
    "undo_last": lambda shop, a: inventory.undo_last(shop),
}

MAX_TOOL_ROUNDS = 8
FALLBACK_REPLY = "Maaf kijiye, abhi samajh nahi paaya. Ek baar phir se bolenge?"

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def run_tool(shop_id: str, name: str, args: dict) -> tuple[str, bool]:
    """Execute one tool call. Returns (json_result, is_error)."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"unknown tool {name}"}), True
    try:
        return json.dumps(handler(shop_id, args), ensure_ascii=False), False
    except ToolError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False), True
    except (KeyError, TypeError, ValueError) as e:
        log.warning("bad tool input for %s: %r (%s)", name, args, e)
        return json.dumps({"error": f"invalid input: {e}"}), True


def _create(messages: list) -> anthropic.types.beta.BetaMessage:
    return client().beta.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=messages,
        thinking={"type": "adaptive"},
        output_config={"effort": config.CLAUDE_EFFORT},
        # If a safety classifier declines, the API retries on a suitable model itself.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )


def handle_message(shop_id: str, text: str, message_id: str | None = None) -> str:
    """Process one shopkeeper message (already transcribed) and return the reply text."""
    token = db.current_batch.set(message_id or f"{shop_id}:{db.now().isoformat()}")
    try:
        return _handle(shop_id, text)
    finally:
        db.current_batch.reset(token)


def _handle(shop_id: str, text: str) -> str:
    known = inventory.product_names(shop_id)
    context = (f"[{db.now():%A %d %b %Y, %I:%M %p} · products in this shop: "
               f"{', '.join(known) if known else 'none yet'}]\n\n")

    # Short text-only memory so follow-ups like "haan, wahi" make sense.
    messages: list[dict] = []
    for turn in db.recent_chat(shop_id, config.CHAT_MEMORY_TURNS * 2):
        if messages and messages[-1]["role"] == turn["role"]:
            messages[-1]["content"] += "\n" + turn["text"]
        else:
            messages.append({"role": turn["role"], "content": turn["text"]})
    if messages and messages[0]["role"] != "user":
        messages.pop(0)
    if messages and messages[-1]["role"] == "user":  # previous reply was lost; keep alternation
        messages.append({"role": "assistant", "content": "(no reply)"})
    messages.append({"role": "user", "content": context + text})

    reply = FALLBACK_REPLY
    for _ in range(MAX_TOOL_ROUNDS):
        response = _create(messages)
        if response.stop_reason == "refusal":
            log.warning("refusal: %s", response.stop_details)
            break
        messages.append({"role": "assistant", "content": response.content})

        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_calls:
            texts = [b.text for b in response.content if b.type == "text"]
            reply = "\n".join(t.strip() for t in texts if t.strip()) or FALLBACK_REPLY
            break

        results = []
        for call in tool_calls:
            output, is_error = run_tool(shop_id, call.name, call.input)
            log.info("tool %s(%s) -> %s", call.name, json.dumps(call.input, ensure_ascii=False), output)
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": output, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    else:
        log.warning("tool loop hit MAX_TOOL_ROUNDS for %s", shop_id)

    db.add_chat(shop_id, "user", text)
    db.add_chat(shop_id, "assistant", reply)
    return reply
