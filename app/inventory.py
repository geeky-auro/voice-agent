"""Inventory operations. These are the functions Claude calls as tools.

Every function takes `shop_id` first, returns a plain dict (sent back to Claude
as JSON), and raises `ToolError` for problems the model should explain or
resolve with the shopkeeper (unknown product, bad quantity, ...).
"""

from __future__ import annotations

import difflib
import re
from datetime import datetime, timedelta

from . import db


class ToolError(Exception):
    pass


# --- product lookup ----------------------------------------------------------

_UNIT_WORDS = {
    "packet", "packets", "pkt", "pkts", "pack", "packs", "piece", "pieces", "pcs", "pc",
    "kg", "kilo", "gram", "grams", "g", "litre", "liter", "litres", "liters", "l", "ml",
    "bottle", "bottles", "box", "boxes", "dabba", "dabbe", "bag", "bags", "bori",
}


def normalise(name: str) -> str:
    """'  MAGGI  Packets ' -> 'maggi'. Keeps Devanagari and other scripts intact."""
    s = re.sub(r"[^\w\s]|_", " ", name.casefold())  # "Parle-G" == "Parle G"
    words = [w for w in s.split() if w not in _UNIT_WORDS]
    s = " ".join(words) or " ".join(s.split())
    # crude plural folding for English names: "biscuits" -> "biscuit"
    if len(s) > 3 and s.endswith("s") and not s.endswith("ss"):
        s = s[:-1]
    return s


def _all_keys(shop_id: str) -> dict[str, int]:
    with db.tx() as c:
        rows = c.execute("SELECT key, id FROM products WHERE shop_id = ?", (shop_id,)).fetchall()
        alias_rows = c.execute(
            "SELECT alias, product_id FROM aliases WHERE shop_id = ?", (shop_id,)
        ).fetchall()
    keys = {r["key"]: r["id"] for r in rows}
    keys.update({r["alias"]: r["product_id"] for r in alias_rows})
    return keys


def find_product(shop_id: str, name: str) -> tuple[dict | None, list[str]]:
    """Exact/alias match first, then a close fuzzy match. Returns (product, suggestions)."""
    key = normalise(name)
    keys = _all_keys(shop_id)
    pid = keys.get(key)
    suggestions: list[str] = []
    if pid is None:
        close = difflib.get_close_matches(key, keys.keys(), n=3, cutoff=0.6)
        if close and difflib.SequenceMatcher(None, key, close[0]).ratio() >= 0.85:
            pid = keys[close[0]]
        else:
            # also suggest names that contain the query word: "surf" -> "Surf Excel"
            close += [k for k in keys if key and (key in k.split() or k in key.split())]
            suggestions = [_get(shop_id, keys[k])["name"] for k in close]
    return (_get(shop_id, pid) if pid is not None else None), sorted(set(suggestions))


def _get(shop_id: str, pid: int) -> dict:
    with db.tx() as c:
        row = c.execute(
            "SELECT * FROM products WHERE shop_id = ? AND id = ?", (shop_id, pid)
        ).fetchone()
    return dict(row)


def _require(shop_id: str, name: str) -> dict:
    product, suggestions = find_product(shop_id, name)
    if product is None:
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        raise ToolError(f"No product called '{name}' in this shop.{hint}")
    return product


def _qty(value) -> float:
    try:
        q = float(value)
    except (TypeError, ValueError):
        raise ToolError(f"Quantity must be a number, got {value!r}")
    if q <= 0:
        raise ToolError(f"Quantity must be positive, got {q:g}")
    return q


def _view(p: dict) -> dict:
    out = {"product": p["name"], "stock": _num(p["qty"]), "unit": p["unit"],
           "low_stock_at": _num(p["low_threshold"])}
    if p["sell_price"] is not None:
        out["price_per_unit"] = _num(p["sell_price"])
    if p["qty"] <= p["low_threshold"]:
        out["LOW_STOCK"] = True
    return out


def _num(x: float | None):
    if x is None:
        return None
    return int(x) if float(x).is_integer() else round(x, 2)


def _move(c, shop_id: str, pid: int, kind: str, qty: float, amount=None, note=None) -> int:
    c.execute("UPDATE products SET qty = qty + ? WHERE id = ?", (qty, pid))
    cur = c.execute(
        "INSERT INTO movements (shop_id, product_id, kind, qty, amount, ts, note, batch) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (shop_id, pid, kind, qty, amount, db.now().isoformat(), note, db.current_batch.get()),
    )
    return cur.lastrowid


# --- tools -------------------------------------------------------------------

def record_stock_in(shop_id: str, items: list[dict]) -> dict:
    """New stock arrived. Unknown products are created on the fly."""
    results = []
    for item in items:
        name = str(item.get("product", "")).strip()
        if not name:
            raise ToolError("Each item needs a product name")
        qty = _qty(item.get("qty"))
        product, _ = find_product(shop_id, name)
        with db.tx() as c:
            if product is None:
                cur = c.execute(
                    "INSERT INTO products (shop_id, name, key, unit) VALUES (?, ?, ?, ?)",
                    (shop_id, name, normalise(name), item.get("unit") or "packet"),
                )
                pid, created = cur.lastrowid, True
            else:
                pid, created = product["id"], False
            _move(c, shop_id, pid, "in", qty, note=item.get("note"))
        view = _view(_get(shop_id, pid))
        view["added"] = _num(qty)
        if created:
            view["new_product"] = True
        results.append(view)
    return {"ok": True, "items": results}


def record_sale(shop_id: str, items: list[dict]) -> dict:
    """Items sold. Reduces stock and reports anything that is now low or negative."""
    # Validate everything first so a bad item doesn't leave a half-recorded note.
    resolved = []
    for item in items:
        product = _require(shop_id, str(item.get("product", "")))
        qty = _qty(item.get("qty"))
        amount = item.get("amount")
        if amount is None and product["sell_price"] is not None:
            amount = product["sell_price"] * qty
        resolved.append((product, qty, amount))

    results, warnings = [], []
    with db.tx() as c:
        for product, qty, amount in resolved:
            _move(c, shop_id, product["id"], "sale", -qty, amount=amount)
    for product, qty, amount in resolved:
        p = _get(shop_id, product["id"])
        view = _view(p)
        view["sold"] = _num(qty)
        if amount is not None:
            view["amount"] = _num(amount)
        results.append(view)
        if p["qty"] < 0:
            warnings.append(f"{p['name']} stock is negative ({_num(p['qty'])}) — "
                            "probably some stock-in was never recorded.")
        elif p["qty"] <= p["low_threshold"]:
            warnings.append(f"{p['name']} is running low: only {_num(p['qty'])} {p['unit']} left.")
    return {"ok": True, "items": results, "warnings": warnings}


def get_stock(shop_id: str, product: str | None = None) -> dict:
    if product:
        return _view(_require(shop_id, product))
    with db.tx() as c:
        rows = c.execute(
            "SELECT * FROM products WHERE shop_id = ? ORDER BY name", (shop_id,)
        ).fetchall()
    return {"products": [_view(dict(r)) for r in rows]}


def low_stock(shop_id: str) -> dict:
    with db.tx() as c:
        rows = c.execute(
            "SELECT * FROM products WHERE shop_id = ? AND qty <= low_threshold ORDER BY qty",
            (shop_id,),
        ).fetchall()
    return {"low_stock": [_view(dict(r)) for r in rows]}


def _period(period: str) -> tuple[datetime, datetime, str]:
    today = db.now().replace(hour=0, minute=0, second=0, microsecond=0)
    p = (period or "today").strip().lower()
    if p == "today":
        return today, today + timedelta(days=1), "today"
    if p == "yesterday":
        return today - timedelta(days=1), today, "yesterday"
    if p == "this_week":  # Monday to now
        start = today - timedelta(days=today.weekday())
        return start, today + timedelta(days=1), "this week"
    if p == "last_7_days":
        return today - timedelta(days=6), today + timedelta(days=1), "last 7 days"
    if p == "this_month":
        return today.replace(day=1), today + timedelta(days=1), "this month"
    try:
        day = datetime.strptime(p, "%Y-%m-%d").replace(tzinfo=db.IST)
    except ValueError:
        raise ToolError("period must be today, yesterday, this_week, last_7_days, "
                        "this_month or a date YYYY-MM-DD")
    return day, day + timedelta(days=1), p


def sales_report(shop_id: str, period: str = "today", product: str | None = None) -> dict:
    start, end, label = _period(period)
    sql = ("SELECT p.name, p.unit, -SUM(m.qty) AS qty, SUM(m.amount) AS amount, "
           "COUNT(m.amount) AS priced, COUNT(*) AS n "
           "FROM movements m JOIN products p ON p.id = m.product_id "
           "WHERE m.shop_id = ? AND m.kind = 'sale' AND m.undone = 0 AND m.ts >= ? AND m.ts < ?")
    args: list = [shop_id, start.isoformat(), end.isoformat()]
    if product:
        sql += " AND p.id = ?"
        args.append(_require(shop_id, product)["id"])
    sql += " GROUP BY p.id ORDER BY qty DESC"
    with db.tx() as c:
        rows = c.execute(sql, args).fetchall()

    lines, total, unpriced = [], 0.0, []
    for r in rows:
        line = {"product": r["name"], "qty_sold": _num(r["qty"]), "unit": r["unit"]}
        if r["amount"] is not None:
            line["amount"] = _num(r["amount"])
            total += r["amount"]
        if r["priced"] < r["n"]:
            unpriced.append(r["name"])
        lines.append(line)
    out = {"period": label, "from": start.date().isoformat(),
           "to": (end - timedelta(days=1)).date().isoformat(), "sales": lines,
           "total_amount": _num(total)}
    if unpriced:
        out["no_price_known_for"] = unpriced
    return out


def update_product(shop_id: str, product: str, *, sell_price=None, low_threshold=None,
                   unit=None, set_stock=None, add_alias=None, rename_to=None) -> dict:
    p = _require(shop_id, product)
    with db.tx() as c:
        if sell_price is not None:
            c.execute("UPDATE products SET sell_price = ? WHERE id = ?", (float(sell_price), p["id"]))
        if low_threshold is not None:
            c.execute("UPDATE products SET low_threshold = ? WHERE id = ?",
                      (float(low_threshold), p["id"]))
        if unit:
            c.execute("UPDATE products SET unit = ? WHERE id = ?", (unit, p["id"]))
        if rename_to:
            c.execute("UPDATE products SET name = ?, key = ? WHERE id = ?",
                      (rename_to, normalise(rename_to), p["id"]))
            c.execute("INSERT OR REPLACE INTO aliases (shop_id, alias, product_id) VALUES (?, ?, ?)",
                      (shop_id, p["key"], p["id"]))
        if add_alias:
            c.execute("INSERT OR REPLACE INTO aliases (shop_id, alias, product_id) VALUES (?, ?, ?)",
                      (shop_id, normalise(add_alias), p["id"]))
        if set_stock is not None:
            diff = float(set_stock) - p["qty"]
            if diff:
                _move(c, shop_id, p["id"], "adjust", diff, note="stock count")
    return {"ok": True, **_view(_get(shop_id, p["id"]))}


def undo_last(shop_id: str) -> dict:
    """Reverse everything recorded by the most recent message (stock-in, sales, adjustments)."""
    with db.tx() as c:
        last = c.execute(
            "SELECT id, batch FROM movements WHERE shop_id = ? AND undone = 0 "
            "ORDER BY id DESC LIMIT 1",
            (shop_id,),
        ).fetchone()
        if last is None:
            raise ToolError("Nothing to undo")
        where, arg = ("m.batch = ?", last["batch"]) if last["batch"] else ("m.id = ?", last["id"])
        rows = c.execute(
            "SELECT m.id, m.product_id, m.kind, m.qty, p.name FROM movements m "
            "JOIN products p ON p.id = m.product_id "
            f"WHERE m.shop_id = ? AND m.undone = 0 AND {where} ORDER BY m.id",
            (shop_id, arg),
        ).fetchall()
        for r in rows:
            c.execute("UPDATE products SET qty = qty - ? WHERE id = ?", (r["qty"], r["product_id"]))
            c.execute("UPDATE movements SET undone = 1 WHERE id = ?", (r["id"],))
    return {"ok": True, "undone": [
        {"product": r["name"], "kind": r["kind"], "qty": _num(abs(r["qty"]))} for r in rows
    ]}


def product_names(shop_id: str) -> list[str]:
    with db.tx() as c:
        rows = c.execute("SELECT name FROM products WHERE shop_id = ? ORDER BY name", (shop_id,))
        return [r["name"] for r in rows]
