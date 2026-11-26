import pytest

from app import db, inventory as inv
from app.inventory import ToolError

SHOP = "919000000001"


@pytest.fixture(autouse=True)
def fresh_db():
    db.connect(":memory:")


def test_stock_in_creates_then_adds():
    r = inv.record_stock_in(SHOP, [{"product": "Maggi", "qty": 10}])
    assert r["items"][0]["new_product"] is True
    assert r["items"][0]["stock"] == 10
    r = inv.record_stock_in(SHOP, [{"product": "maggi packets", "qty": 5}])
    assert "new_product" not in r["items"][0]
    assert r["items"][0]["stock"] == 15


def test_fuzzy_match_avoids_duplicates():
    inv.record_stock_in(SHOP, [{"product": "Parle-G", "qty": 20}])
    inv.record_stock_in(SHOP, [{"product": "Parle G", "qty": 5}])
    assert [p["product"] for p in inv.get_stock(SHOP)["products"]] == ["Parle-G"]


def test_sale_reduces_stock_and_warns_when_low():
    inv.record_stock_in(SHOP, [{"product": "Maggi", "qty": 10}])
    r = inv.record_sale(SHOP, [{"product": "Maggi", "qty": 3}])
    assert r["items"][0]["stock"] == 7 and r["warnings"] == []
    r = inv.record_sale(SHOP, [{"product": "Maggi", "qty": 4}])
    assert r["items"][0]["LOW_STOCK"] is True
    assert "running low" in r["warnings"][0]


def test_sale_of_unknown_product_suggests_and_writes_nothing():
    inv.record_stock_in(SHOP, [{"product": "Surf Excel", "qty": 4}])
    with pytest.raises(ToolError, match="Surf Excel"):
        inv.record_sale(SHOP, [{"product": "Surf", "qty": 1}, {"product": "Surf Excel", "qty": 1}])
    assert inv.get_stock(SHOP, "Surf Excel")["stock"] == 4  # nothing half-recorded


def test_bad_quantity():
    with pytest.raises(ToolError):
        inv.record_stock_in(SHOP, [{"product": "Maggi", "qty": 0}])


def test_sales_report_with_and_without_prices():
    inv.record_stock_in(SHOP, [{"product": "Maggi", "qty": 10}, {"product": "Tata Salt", "qty": 5}])
    inv.update_product(SHOP, "Maggi", sell_price=14)
    inv.record_sale(SHOP, [{"product": "Maggi", "qty": 3}, {"product": "Tata Salt", "qty": 1}])
    inv.record_sale(SHOP, [{"product": "Maggi", "qty": 2}])
    r = inv.sales_report(SHOP, "today")
    by = {s["product"]: s for s in r["sales"]}
    assert by["Maggi"]["qty_sold"] == 5 and by["Maggi"]["amount"] == 70
    assert by["Tata Salt"]["qty_sold"] == 1 and "amount" not in by["Tata Salt"]
    assert r["total_amount"] == 70
    assert r["no_price_known_for"] == ["Tata Salt"]
    assert inv.sales_report(SHOP, "yesterday")["sales"] == []


def test_undo_reverses_whole_message():
    db.current_batch.set("msg-1")
    inv.record_stock_in(SHOP, [{"product": "Maggi", "qty": 10}])
    db.current_batch.set("msg-2")
    inv.record_stock_in(SHOP, [{"product": "Maggi", "qty": 5}])
    inv.record_sale(SHOP, [{"product": "Maggi", "qty": 3}])
    r = inv.undo_last(SHOP)
    assert len(r["undone"]) == 2
    assert inv.get_stock(SHOP, "Maggi")["stock"] == 10
    assert inv.sales_report(SHOP)["sales"] == []


def test_alias_and_set_stock():
    inv.record_stock_in(SHOP, [{"product": "Maggi", "qty": 10}])
    inv.update_product(SHOP, "Maggi", add_alias="मैगी", set_stock=8, low_threshold=3)
    p = inv.get_stock(SHOP, "मैगी")
    assert p["product"] == "Maggi" and p["stock"] == 8 and p["low_stock_at"] == 3


def test_shops_are_isolated():
    inv.record_stock_in(SHOP, [{"product": "Maggi", "qty": 10}])
    assert inv.get_stock("other-shop")["products"] == []
