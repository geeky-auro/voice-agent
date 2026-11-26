"""SQLite storage: products, stock movements, and a short chat memory per shop.

One shop = one WhatsApp number. Everything is keyed by `shop_id` (the sender's
phone number) so a single deployment can serve several shopkeepers.
"""

from __future__ import annotations

import sqlite3
import threading
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config

IST = ZoneInfo(config.TIMEZONE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id            INTEGER PRIMARY KEY,
    shop_id       TEXT    NOT NULL,
    name          TEXT    NOT NULL,          -- canonical display name, e.g. "Maggi"
    key           TEXT    NOT NULL,          -- normalised name used for lookup
    unit          TEXT    NOT NULL DEFAULT 'packet',
    qty           REAL    NOT NULL DEFAULT 0,
    low_threshold REAL    NOT NULL DEFAULT 5,
    sell_price    REAL,                      -- per unit, optional
    UNIQUE (shop_id, key)
);

CREATE TABLE IF NOT EXISTS aliases (
    shop_id    TEXT    NOT NULL,
    alias      TEXT    NOT NULL,             -- normalised
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    PRIMARY KEY (shop_id, alias)
);

CREATE TABLE IF NOT EXISTS movements (
    id         INTEGER PRIMARY KEY,
    shop_id    TEXT    NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL CHECK (kind IN ('in', 'sale', 'adjust')),
    qty        REAL    NOT NULL,             -- signed change to stock
    amount     REAL,                         -- money for sales, if known
    ts         TEXT    NOT NULL,             -- ISO timestamp, shop timezone
    note       TEXT,
    batch      TEXT,                         -- id of the message that caused it
    undone     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS movements_shop_ts ON movements (shop_id, ts);

CREATE TABLE IF NOT EXISTS chat (
    id      INTEGER PRIMARY KEY,
    shop_id TEXT NOT NULL,
    role    TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text    TEXT NOT NULL,
    ts      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_messages (
    message_id TEXT PRIMARY KEY,
    ts         TEXT NOT NULL
);
"""

# Set per incoming message so "undo" can reverse everything one message did.
current_batch: ContextVar[str | None] = ContextVar("current_batch", default=None)

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open (or reopen) the global connection. Tests pass ':memory:'."""
    global _conn
    _conn = sqlite3.connect(path or config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA foreign_keys = ON")
    _conn.executescript(SCHEMA)
    return _conn


@contextmanager
def tx():
    """Serialised transaction. SQLite + a small shop = a single lock is plenty."""
    if _conn is None:
        connect()
    with _lock:
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def now() -> datetime:
    return datetime.now(IST)


def day_bounds(day: datetime) -> tuple[str, str]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


# --- dedupe WhatsApp retries -------------------------------------------------

def mark_seen(message_id: str) -> bool:
    """Return True the first time a message id is seen, False on a retry."""
    with tx() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO seen_messages (message_id, ts) VALUES (?, ?)",
            (message_id, now().isoformat()),
        )
        return cur.rowcount == 1


# --- chat memory -------------------------------------------------------------

def add_chat(shop_id: str, role: str, text: str) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO chat (shop_id, role, text, ts) VALUES (?, ?, ?, ?)",
            (shop_id, role, text, now().isoformat()),
        )


def recent_chat(shop_id: str, limit: int) -> list[dict]:
    with tx() as c:
        rows = c.execute(
            "SELECT role, text FROM chat WHERE shop_id = ? ORDER BY id DESC LIMIT ?",
            (shop_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]
