import datetime

import agent


# ---------------------------------------------------------
# get_inventory_status
# ---------------------------------------------------------
def test_get_inventory_status_handles_ingredient_with_no_expiration_date(fake_db):
    fake_db.seed(
        "ingredients",
        "eggs",
        {"id": "eggs", "name": "Eggs", "stock_qty": 5, "unit": "dozen", "unit_cost": 3.5, "expiration_date": None, "reorder_threshold": 0},
    )
    result = agent.get_inventory_status()
    assert isinstance(result, dict)  # must not degrade into an error string
    assert all(item["id"] != "eggs" for item in result["expiring_soon"])
    assert any(item["id"] == "eggs" for item in result["all_ingredients"])


# ---------------------------------------------------------
# _resolve_document_id
# ---------------------------------------------------------
def test_resolve_by_exact_id(fake_db):
    resolved_id, error = agent._resolve_document_id("ingredients", "flour_001")
    assert resolved_id == "flour_001"
    assert error is None


def test_resolve_by_exact_name(fake_db):
    resolved_id, error = agent._resolve_document_id("ingredients", "Bread Flour")
    assert resolved_id == "flour_001"
    assert error is None


def test_resolve_by_substring_single_match(fake_db):
    resolved_id, error = agent._resolve_document_id("ingredients", "strawberries")
    assert resolved_id == "strawberries_001"
    assert error is None


def test_resolve_no_match(fake_db):
    resolved_id, error = agent._resolve_document_id("ingredients", "chocolate chips")
    assert resolved_id is None
    assert "No item found" in error


def test_resolve_multiple_matches_lists_candidates(fake_db):
    fake_db.seed(
        "ingredients",
        "whole_wheat_flour",
        {"id": "whole_wheat_flour", "name": "Whole Wheat Flour", "stock_qty": 5.0, "unit_cost": 1.5},
    )
    resolved_id, error = agent._resolve_document_id("ingredients", "flour")
    assert resolved_id is None
    assert "Multiple matches" in error
    assert "Bread Flour" in error and "Whole Wheat Flour" in error


# ---------------------------------------------------------
# log_inventory_usage
# ---------------------------------------------------------
def test_log_inventory_usage_rejects_non_positive_qty(fake_db):
    assert "positive number" in agent.log_inventory_usage("butter", 0)
    assert "positive number" in agent.log_inventory_usage("butter", -1)


def test_log_inventory_usage_deducts_and_resolves_by_name(fake_db):
    result = agent.log_inventory_usage("butter", 0.5, reason="waste")
    assert "0.5" in result
    assert fake_db.collection("ingredients")._docs["butter_001"]["stock_qty"] == 7.5


def test_log_inventory_usage_logs_reason_and_cost(fake_db):
    agent.log_inventory_usage("butter", 0.5, reason="waste")
    events = fake_db.collection("activity_log")._docs
    assert len(events) == 1
    details = list(events.values())[0]["details"]
    assert details["reason"] == "waste"
    assert details["cost"] == 3.25  # 0.5 * 6.50


def test_log_inventory_usage_unknown_item(fake_db):
    result = agent.log_inventory_usage("unobtainium", 1)
    assert "No item found" in result


# ---------------------------------------------------------
# restock_ingredient
# ---------------------------------------------------------
def test_restock_adds_stock(fake_db):
    result = agent.restock_ingredient("flour", 5)
    assert fake_db.collection("ingredients")._docs["flour_001"]["stock_qty"] == 50.0
    assert "Restocked 5" in result


def test_restock_rejects_non_positive_qty(fake_db):
    assert "positive number" in agent.restock_ingredient("flour", 0)


def test_restock_updates_expiration_only_when_given(fake_db):
    agent.restock_ingredient("flour", 1)
    assert fake_db.collection("ingredients")._docs["flour_001"]["expiration_date"] == "2026-12-01"

    agent.restock_ingredient("flour", 1, new_expiration_date="2027-01-01")
    assert fake_db.collection("ingredients")._docs["flour_001"]["expiration_date"] == "2027-01-01"


def test_restock_logs_cost(fake_db):
    agent.restock_ingredient("flour", 5)
    details = list(fake_db.collection("activity_log")._docs.values())[0]["details"]
    assert details["cost"] == 6.0  # 5 * 1.20


# ---------------------------------------------------------
# add_ingredient
# ---------------------------------------------------------
def test_add_ingredient_creates_new_and_blocks_duplicate(fake_db):
    result = agent.add_ingredient("Eggs", unit="dozen", unit_cost=3.5, initial_stock_qty=5)
    assert "Added new ingredient" in result
    doc = fake_db.collection("ingredients")._docs["eggs"]
    assert doc["unit"] == "dozen"
    assert doc["unit_cost"] == 3.5
    assert doc["stock_qty"] == 5
    assert doc["expiration_date"] is None  # never a hallucinated placeholder

    duplicate = agent.add_ingredient("Eggs", unit="dozen", unit_cost=3.5)
    assert "already exists" in duplicate


def test_add_ingredient_rejects_negative_values(fake_db):
    assert "negative" in agent.add_ingredient("Eggs", unit="dozen", unit_cost=-1)
    assert "negative" in agent.add_ingredient("Eggs", unit="dozen", unit_cost=1, initial_stock_qty=-5)


def test_restock_after_add_ingredient_works(fake_db):
    agent.add_ingredient("Eggs", unit="dozen", unit_cost=3.5, initial_stock_qty=5)
    result = agent.restock_ingredient("eggs", 2)
    assert "Restocked 2" in result
    assert fake_db.collection("ingredients")._docs["eggs"]["stock_qty"] == 7


# ---------------------------------------------------------
# add_recipe
# ---------------------------------------------------------
def test_add_recipe_creates_new_with_resolved_ingredients(fake_db):
    agent.add_ingredient("Eggs", unit="dozen", unit_cost=3.5, initial_stock_qty=5)
    result = agent.add_recipe(
        "Egg Tart", selling_price=5.0, ingredients=[{"item": "eggs", "qty": 1}, {"item": "flour", "qty": 0.1}]
    )
    assert "Added new recipe" in result
    doc = fake_db.collection("recipes")._docs["egg_tart"]
    ids = {ing["id"] for ing in doc["ingredients"]}
    assert ids == {"eggs", "flour_001"}


def test_add_recipe_blocks_on_untracked_ingredient(fake_db):
    result = agent.add_recipe("Chocolate Cake", selling_price=10.0, ingredients=[{"item": "cocoa powder", "qty": 0.2}])
    assert "Can't add recipe" in result
    assert "cocoa powder" in result
    assert "recipes" not in fake_db._collections or "chocolate_cake" not in fake_db.collection("recipes")._docs


def test_add_recipe_blocks_on_unit_mismatch(fake_db):
    # flour_001 is tracked in kg — "2 cups" must not be silently stored as qty=2 kg
    result = agent.add_recipe(
        "Vanilla Scones", selling_price=5.0, ingredients=[{"item": "flour", "qty": 2, "unit": "cups"}]
    )
    assert "Can't add recipe" in result
    assert "cups" in result and "kg" in result
    assert "vanilla_scones" not in fake_db.collection("recipes")._docs


def test_add_recipe_accepts_matching_unit(fake_db):
    result = agent.add_recipe(
        "Vanilla Scones", selling_price=5.0, ingredients=[{"item": "flour", "qty": 0.2, "unit": "kg"}]
    )
    assert "Added new recipe" in result


def test_add_recipe_blocks_duplicate(fake_db):
    agent.add_recipe("Egg Tart", selling_price=5.0, ingredients=[])
    result = agent.add_recipe("Egg Tart", selling_price=5.0, ingredients=[])
    assert "already exists" in result


def test_bake_newly_added_recipe_end_to_end(fake_db):
    agent.add_ingredient("Eggs", unit="dozen", unit_cost=3.5, initial_stock_qty=5)
    agent.add_recipe("Egg Tart", selling_price=5.0, ingredients=[{"item": "eggs", "qty": 1}])
    result = agent.bake_recipe("Egg Tart", 1)
    assert "Baked 1x" in result
    assert fake_db.collection("ingredients")._docs["eggs"]["stock_qty"] == 4


# ---------------------------------------------------------
# bake_recipe
# ---------------------------------------------------------
def test_bake_recipe_deducts_all_ingredients(fake_db):
    result = agent.bake_recipe("strawberry tart", 2)
    ingredients = fake_db.collection("ingredients")._docs
    assert ingredients["strawberries_001"]["stock_qty"] == 3.4  # 4 - 0.6
    assert ingredients["flour_001"]["stock_qty"] == 44.8  # 45 - 0.2
    assert ingredients["butter_001"]["stock_qty"] == 7.8  # 8 - 0.2
    assert "Baked 2x" in result


def test_bake_recipe_blocks_all_or_nothing_on_shortage(fake_db):
    result = agent.bake_recipe("strawberry tart", 999)
    assert "Can't bake" in result
    ingredients = fake_db.collection("ingredients")._docs
    # nothing should have been deducted
    assert ingredients["strawberries_001"]["stock_qty"] == 4.0
    assert ingredients["flour_001"]["stock_qty"] == 45.0
    assert ingredients["butter_001"]["stock_qty"] == 8.0


def test_bake_recipe_rejects_non_positive_quantity(fake_db):
    assert "positive number" in agent.bake_recipe("strawberry tart", 0)


def test_bake_recipe_logs_ingredient_cost(fake_db):
    agent.bake_recipe("strawberry tart", 1)
    details = list(fake_db.collection("activity_log")._docs.values())[0]["details"]
    # 0.3*5.00 + 0.1*1.20 + 0.1*6.50 = 1.5 + 0.12 + 0.65 = 2.27
    assert details["ingredient_cost"] == 2.27


# ---------------------------------------------------------
# Custom orders / order lifecycle
# ---------------------------------------------------------
def test_record_custom_order_creates_pending_order(fake_db):
    agent.record_custom_order("Sarah", "sourdough loaves", quantity=2, due_date="2026-09-01")
    orders = list(fake_db.collection("custom_orders")._docs.values())
    assert len(orders) == 1
    assert orders[0]["status"] == "pending"
    assert orders[0]["customer"] == "Sarah"
    assert orders[0]["price"] is None


def test_get_open_orders_excludes_paid_and_cancelled(fake_db):
    fake_db.seed("custom_orders", "o1", {"customer": "A", "item": "x", "status": "pending"})
    fake_db.seed("custom_orders", "o2", {"customer": "B", "item": "y", "status": "paid"})
    fake_db.seed("custom_orders", "o3", {"customer": "C", "item": "z", "status": "cancelled"})
    open_orders = agent.get_open_orders()
    customers = {o["customer"] for o in open_orders}
    assert customers == {"A"}


def test_update_order_status_paid_requires_price(fake_db):
    agent.record_custom_order("Sarah", "sourdough loaves", quantity=2)
    result = agent.update_order_status("Sarah", "paid")
    assert "no price set" in result


def test_update_order_status_paid_logs_sale_and_is_idempotent(fake_db):
    agent.record_custom_order("Sarah", "sourdough loaves", quantity=2)

    first = agent.update_order_status("Sarah", "paid", price=15)
    assert "marked paid" in first
    assert "$15" in first

    second = agent.update_order_status("Sarah", "paid", price=15)
    assert "already" in second

    sale_events = [
        e for e in fake_db.collection("activity_log")._docs.values() if e["type"] == "sale"
    ]
    assert len(sale_events) == 1  # not double-logged
    assert sale_events[0]["details"]["revenue"] == 15
    assert sale_events[0]["details"]["order_id"] is not None


def test_update_order_status_reuses_existing_price(fake_db):
    agent.record_custom_order("Mike", "strawberry tart", quantity=1, price=8)
    result = agent.update_order_status("Mike", "paid")
    assert "$8" in result


def test_update_order_status_disambiguates_multiple_matches(fake_db):
    agent.record_custom_order("Mike", "strawberry tart", quantity=1, price=8)
    agent.record_custom_order("Maria", "strawberry tart", quantity=1, price=8)
    result = agent.update_order_status("strawberry", "paid")
    assert "Multiple orders match" in result
    assert "Mike" in result and "Maria" in result


def test_update_order_status_rejects_invalid_status(fake_db):
    agent.record_custom_order("Sarah", "sourdough loaves")
    result = agent.update_order_status("Sarah", "not_a_real_status")
    assert "must be one of" in result


# ---------------------------------------------------------
# record_sale
# ---------------------------------------------------------
def test_record_sale_resolves_known_recipe(fake_db):
    result = agent.record_sale("strawberry tart", 2, 16)
    assert "Strawberry Tart" in result
    details = list(fake_db.collection("activity_log")._docs.values())[0]["details"]
    assert details["recipe_id"] == "strawberry_tart"
    assert details["order_id"] is None


def test_record_sale_allows_unmatched_custom_item(fake_db):
    result = agent.record_sale("Custom birthday cake", 1, 40)
    assert "Custom birthday cake" in result
    details = list(fake_db.collection("activity_log")._docs.values())[0]["details"]
    assert details["recipe_id"] is None


def test_record_sale_blocks_on_ambiguous_item(fake_db):
    fake_db.seed(
        "recipes",
        "strawberry_shortcake",
        {"id": "strawberry_shortcake", "name": "Strawberry Shortcake", "selling_price": 6, "ingredients": []},
    )
    result = agent.record_sale("strawberry", 1, 5)
    assert "Multiple matches" in result


def test_record_sale_rejects_bad_input(fake_db):
    assert "positive number" in agent.record_sale("strawberry tart", 0, 10)
    assert "can't be negative" in agent.record_sale("strawberry tart", 1, -5)


# ---------------------------------------------------------
# record_expense
# ---------------------------------------------------------
def test_record_expense_logs_and_validates(fake_db):
    result = agent.record_expense("Pastry boxes", 12.50)
    assert "12.5" in result
    assert "positive number" in agent.record_expense("Boxes", 0)


# ---------------------------------------------------------
# get_activity_log
# ---------------------------------------------------------
def test_get_activity_log_filters_by_days(fake_db):
    now = datetime.datetime.now()
    fake_db.seed(
        "activity_log", "recent", {"type": "expense", "details": {}, "timestamp": (now - datetime.timedelta(days=1)).isoformat()}
    )
    fake_db.seed(
        "activity_log", "old", {"type": "expense", "details": {}, "timestamp": (now - datetime.timedelta(days=30)).isoformat()}
    )
    events = agent.get_activity_log(days=7)
    assert len(events) == 1


# ---------------------------------------------------------
# get_financial_summary
# ---------------------------------------------------------
def test_financial_summary_aggregates_correctly(fake_db):
    agent.record_sale("strawberry tart", 2, 16)
    agent.restock_ingredient("flour", 5)  # cost 6.0 — reported, but not part of total_cost
    agent.bake_recipe("strawberry_tart", 1)  # ingredient_cost 2.27
    agent.log_inventory_usage("butter", 0.5, reason="waste")  # cost 3.25
    agent.record_expense("Boxes", 12.5)

    summary = agent.get_financial_summary(days=1)
    assert summary["revenue"] == 16.0
    assert summary["restock_spend"] == 6.0
    assert summary["production_cost"] == 2.27
    assert summary["waste_cost"] == 3.25
    assert summary["other_expenses"] == 12.5
    assert summary["total_cost"] == 18.02
    assert summary["net"] == -2.02


def test_financial_summary_excludes_correction_reason(fake_db):
    agent.log_inventory_usage("flour", 1, reason="correction")
    summary = agent.get_financial_summary(days=1)
    assert summary["waste_cost"] == 0.0
    assert summary["total_cost"] == 0.0


# ---------------------------------------------------------
# generate_shopping_list / get_production_plan
# ---------------------------------------------------------
def test_generate_shopping_list_reports_shortfall_only(fake_db):
    result = agent.generate_shopping_list(
        [{"recipe": "strawberry tart", "quantity": 20}, {"recipe": "sourdough", "quantity": 5}]
    )
    shortfalls = {item["item"]: item for item in result["shopping_list"]}
    assert "Fresh Strawberries" in shortfalls
    assert shortfalls["Fresh Strawberries"]["buy"] == 2.0  # need 6, have 4
    assert "Bread Flour" not in shortfalls  # plenty in stock


def test_generate_shopping_list_reports_unresolved_recipes(fake_db):
    result = agent.generate_shopping_list([{"recipe": "nonexistent cake", "quantity": 1}])
    assert result["shopping_list"] == []
    assert any("nonexistent cake" in u for u in result["unresolved"])


def test_get_production_plan_uses_open_orders_within_window(fake_db):
    agent.record_custom_order("Sarah", "strawberry tart", quantity=10, price=60, due_date="2026-09-02")
    agent.record_custom_order("Later", "strawberry tart", quantity=5, price=30, due_date="2026-12-25")

    plan = agent.get_production_plan(days=7)
    bake_list = {b["recipe"]: b["quantity"] for b in plan["bake_list"]}
    assert bake_list.get("Strawberry Tart") == 10.0  # only Sarah's order, due soon


def test_get_production_plan_flags_unmatched_order_items(fake_db):
    agent.record_custom_order("Sarah", "custom unicorn cake", quantity=1, due_date="2026-08-30")
    plan = agent.get_production_plan(days=7)
    assert plan["bake_list"] == []
    assert len(plan["unmatched_orders"]) == 1
    assert plan["unmatched_orders"][0]["customer"] == "Sarah"


# ---------------------------------------------------------
# get_accounts_receivable
# ---------------------------------------------------------
def test_accounts_receivable_totals_priced_orders_only(fake_db):
    agent.record_custom_order("Sarah", "strawberry tart", quantity=10, price=60)
    agent.record_custom_order("Mike", "sourdough", quantity=2)  # no price

    ar = agent.get_accounts_receivable()
    assert ar["total_owed"] == 60
    assert len(ar["orders"]) == 1
    assert len(ar["orders_missing_price"]) == 1


# ---------------------------------------------------------
# suggest_price
# ---------------------------------------------------------
def test_suggest_price_computes_from_cost_and_margin(fake_db):
    result = agent.suggest_price("strawberry tart", target_margin_pct=80)
    assert result["ingredient_cost"] == 2.27
    assert result["suggested_price"] == 11.35  # 2.27 / 0.2


def test_suggest_price_rejects_invalid_margin(fake_db):
    result = agent.suggest_price("strawberry tart", target_margin_pct=150)
    assert "error" in result


def test_suggest_price_unknown_recipe(fake_db):
    result = agent.suggest_price("nonexistent recipe")
    assert "error" in result


# ---------------------------------------------------------
# Customer notes
# ---------------------------------------------------------
def test_customer_notes_save_and_filter(fake_db):
    agent.save_customer_note("Sarah", "Allergic to walnuts")
    agent.save_customer_note("Mike", "Likes extra crust")

    all_notes = agent.get_customer_notes()
    assert len(all_notes) == 2

    sarah_notes = agent.get_customer_notes("sarah")
    assert len(sarah_notes) == 1
    assert sarah_notes[0]["note"] == "Allergic to walnuts"


# ---------------------------------------------------------
# _handle_tool_errors
# ---------------------------------------------------------
def test_handle_tool_errors_catches_exceptions(fake_db, monkeypatch):
    def broken_stream():
        raise RuntimeError("boom")

    monkeypatch.setattr(fake_db.collection("ingredients"), "stream", broken_stream)
    result = agent.get_inventory_status()
    assert isinstance(result, str)
    assert "Error" in result
