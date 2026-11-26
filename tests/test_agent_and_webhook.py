"""Agent loop and webhook, with Claude, Sarvam and WhatsApp replaced by fakes."""

import hashlib
import hmac
import json
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from app import agent, config, db, inventory, main, stt, whatsapp

SHOP = "919000000001"


@pytest.fixture(autouse=True)
def fresh_db():
    db.connect(":memory:")


class FakeClaude:
    """Replays scripted responses and records the requests it got."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs, default=lambda o: o.__dict__)))
        return self.responses.pop(0)


def tool_use(id_, name, input_):
    return NS(type="tool_use", id=id_, name=name, input=input_)


def text(t):
    return NS(type="text", text=t)


def msg(stop, *blocks):
    return NS(stop_reason=stop, content=list(blocks), stop_details=None)


def test_agent_runs_tools_then_replies(monkeypatch):
    fake = FakeClaude([
        msg("tool_use",
            tool_use("t1", "record_stock_in", {"items": [{"product": "Maggi", "qty": 10}]}),
            tool_use("t2", "record_sale", {"items": [{"product": "Maggi", "qty": 3}]})),
        msg("end_turn", text("Maggi 10 aaye, 3 bike. Ab *7 packet* bacha hai.")),
    ])
    monkeypatch.setattr(agent, "_client", fake)

    reply = agent.handle_message(SHOP, "10 packet Maggi aaye, 3 bik gaye", message_id="wamid.1")

    assert "7 packet" in reply
    assert inventory.get_stock(SHOP, "Maggi")["stock"] == 7
    # both tool results went back together in one user message
    results = fake.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert not any(r["is_error"] for r in results)
    # request shape
    req = fake.requests[0]
    assert req["model"] == config.CLAUDE_MODEL and req["thinking"] == {"type": "adaptive"}
    # memory is saved for the next turn
    assert [c["role"] for c in db.recent_chat(SHOP, 10)] == ["user", "assistant"]


def test_tool_error_is_reported_to_claude(monkeypatch):
    fake = FakeClaude([
        msg("tool_use", tool_use("t1", "record_sale", {"items": [{"product": "Kurkure", "qty": 2}]})),
        msg("end_turn", text("Kurkure abhi stock mein nahi hai. Pehle kitne aaye the?")),
    ])
    monkeypatch.setattr(agent, "_client", fake)
    agent.handle_message(SHOP, "2 Kurkure bike")
    result = fake.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "Kurkure" in result["content"]


def test_refusal_gives_fallback(monkeypatch):
    monkeypatch.setattr(agent, "_client", FakeClaude([msg("refusal")]))
    assert agent.handle_message(SHOP, "hello") == agent.FALLBACK_REPLY


# --- webhook -----------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    sent = []
    monkeypatch.setattr(whatsapp, "send_text", lambda to, body, reply_to=None: sent.append((to, body)))
    monkeypatch.setattr(whatsapp, "mark_read", lambda _id: None)
    monkeypatch.setattr(whatsapp, "download_media", lambda _id: (b"OggS...", "audio/ogg; codecs=opus"))
    monkeypatch.setattr(config, "WHATSAPP_APP_SECRET", "secret")
    monkeypatch.setattr(config, "ALLOWED_NUMBERS", set())
    c = TestClient(main.app)
    c.sent = sent
    return c


def _post(client, payload):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": sig,
                                                         "Content-Type": "application/json"})


def _payload(message):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp", "messages": [message]}}]}]}


def test_verify_handshake(client, monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_VERIFY_TOKEN", "tok")
    r = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "tok",
                                       "hub.challenge": "123"})
    assert r.status_code == 200 and r.text == "123"
    r = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope"})
    assert r.status_code == 403


def test_bad_signature_rejected(client):
    r = client.post("/webhook", content=b"{}", headers={"X-Hub-Signature-256": "sha256=00"})
    assert r.status_code == 401


def test_voice_note_end_to_end(client, monkeypatch):
    seen_keyterms = []

    def fake_stt(audio, mime, keyterms=None):
        seen_keyterms.append(keyterms)
        return "10 packet Maggi aaye, 3 bik gaye"

    monkeypatch.setattr(stt, "transcribe", fake_stt)
    monkeypatch.setattr(agent, "_client", FakeClaude([
        msg("tool_use",
            tool_use("t1", "record_stock_in", {"items": [{"product": "Maggi", "qty": 10}]}),
            tool_use("t2", "record_sale", {"items": [{"product": "Maggi", "qty": 3}]})),
        msg("end_turn", text("Ho gaya ✅ Maggi: *7 packet* bacha.")),
    ]))
    message = {"from": SHOP, "id": "wamid.A", "type": "audio", "audio": {"id": "media1"}}

    assert _post(client, _payload(message)).status_code == 200
    assert _post(client, _payload(message)).status_code == 200  # Meta retry: ignored

    assert len(client.sent) == 1
    to, body = client.sent[0]
    assert to == SHOP and "10 packet Maggi aaye" in body and "7 packet" in body
    assert inventory.get_stock(SHOP, "Maggi")["stock"] == 7
    assert seen_keyterms == [[]]  # new shop: no product names yet


def test_unsupported_message_type(client):
    _post(client, _payload({"from": SHOP, "id": "wamid.B", "type": "image", "image": {"id": "x"}}))
    assert client.sent == [(SHOP, main.UNSUPPORTED)]
