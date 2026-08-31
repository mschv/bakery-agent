import datetime
import functools
import os
import re

from google import genai
from google.api_core import exceptions as google_exceptions
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.genai import types

# 1. Initialize Credentials & Clients
# Credentials are resolved via Application Default Credentials (ADC), never
# hardcoded or read from a file checked into the repo. Set
# GOOGLE_APPLICATION_CREDENTIALS to point at your local service account key
# (kept out of git via .gitignore), or run `gcloud auth application-default login`.
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "bakery-agentic-copilot")
# gemini-3.5-flash is only served on Vertex AI's "global" endpoint in this
# project, not regional ones (verified by probing regions directly) — hence
# the changed default from the prior "us-central1".
LOCATION = os.environ.get("GCP_LOCATION", "global")
MODEL_NAME = "gemini-3.5-flash"

db = firestore.Client(project=PROJECT_ID)
client = genai.Client(
    vertexai=True,
    project=PROJECT_ID,
    location=LOCATION,
)


# ---------------------------------------------------------
# Operational Tools
# ---------------------------------------------------------
def _handle_tool_errors(func):
    """Catches Firestore/network failures in a tool call and returns a readable
    error string instead of raising, so a transient failure doesn't crash the
    chat session or the Gemini tool-calling loop mid-turn."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except google_exceptions.GoogleAPIError as e:
            return f"Error: couldn't reach the database ({e}). Try again in a moment."
        except Exception as e:
            return f"Error: something went wrong running {func.__name__} ({e})."

    return wrapper


def _resolve_document_id(collection: str, query: str):
    """Resolves a baker-supplied name or id to an exact Firestore doc id, so
    tools never require the caller to already know internal ids like
    'strawberries_001'. Tries, in order: exact id match, exact name match,
    then substring match on name or id. Returns (resolved_id, error_message)
    — exactly one of the two is set. On multiple substring matches, the
    error message lists the candidates by name for the baker to pick from.
    """
    coll = db.collection(collection)

    exact_doc = coll.document(query).get()
    if exact_doc.exists:
        return exact_doc.id, None

    docs = {doc.id: doc.to_dict() for doc in coll.stream()}
    query_lower = query.strip().lower()

    exact_name_matches = [
        doc_id for doc_id, data in docs.items()
        if data.get("name", "").lower() == query_lower
    ]
    if len(exact_name_matches) == 1:
        return exact_name_matches[0], None

    partial_matches = [
        (doc_id, data.get("name", doc_id))
        for doc_id, data in docs.items()
        if query_lower in data.get("name", "").lower() or query_lower in doc_id.lower()
    ]
    if len(partial_matches) == 1:
        return partial_matches[0][0], None
    if len(partial_matches) > 1:
        options = ", ".join(f"{name} ({doc_id})" for doc_id, name in partial_matches)
        return None, f"Multiple matches for '{query}': {options}. Which one did you mean?"

    return None, f"No item found matching '{query}'."


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "item"


def _unique_doc_id(collection: str, name: str) -> str:
    """Generates a readable doc id from a name (e.g. 'Eggs' -> 'eggs'),
    appending a numeric suffix on collision so add_ingredient/add_recipe
    never overwrite an existing document."""
    base = _slugify(name)
    coll = db.collection(collection)
    if not coll.document(base).get().exists:
        return base
    i = 2
    while coll.document(f"{base}_{i}").get().exists:
        i += 1
    return f"{base}_{i}"


def _log_event(event_type: str, details: dict) -> None:
    """Records an entry in the activity log — the dated history of bakes,
    restocks, manual usage, and custom orders that get_activity_log reads
    from. Called internally by other tools after they succeed; not itself
    exposed to the model."""
    db.collection("activity_log").add(
        {
            "type": event_type,
            "details": details,
            "timestamp": datetime.datetime.now().isoformat(),
        }
    )


@_handle_tool_errors
def get_inventory_status() -> dict:
    """Queries low-stock items (below reorder threshold) or near-expiry items (within 3 days)."""
    three_days_out = (
        datetime.date.today() + datetime.timedelta(days=3)
    ).isoformat()

    ingredients = [doc.to_dict() for doc in db.collection("ingredients").stream()]

    expiring_soon = [
        item
        for item in ingredients
        if item.get("expiration_date") and item["expiration_date"] <= three_days_out
    ]
    low_stock = [
        item
        for item in ingredients
        if item.get("stock_qty", 0) <= item.get("reorder_threshold", 0)
    ]

    return {
        "expiring_soon": expiring_soon,
        "low_stock": low_stock,
        "all_ingredients": ingredients,
    }


@_handle_tool_errors
def calculate_recipe_margins() -> list[dict]:
    """Computes profit per unit and cost breakdown based on current ingredient costs."""
    ingredients = {
        doc.id: doc.to_dict() for doc in db.collection("ingredients").stream()
    }
    recipes = [doc.to_dict() for doc in db.collection("recipes").stream()]

    margins = []
    for recipe in recipes:
        cost = 0.0
        for ing in recipe.get("ingredients", []):
            ing_data = ingredients.get(ing["id"], {})
            unit_cost = ing_data.get("unit_cost", 0.0)
            cost += unit_cost * ing["qty"]

        selling_price = recipe.get("selling_price", 0.0)
        profit = selling_price - cost
        margin_pct = (profit / selling_price * 100) if selling_price > 0 else 0

        margins.append(
            {
                "id": recipe.get("id"),
                "recipe_name": recipe["name"],
                "cost": round(cost, 2),
                "selling_price": round(selling_price, 2),
                "profit": round(profit, 2),
                "margin_pct": f"{round(margin_pct, 2)}%",
            }
        )
    return margins


@_handle_tool_errors
def log_inventory_usage(item_id: str, qty: float, reason: str = "waste") -> str:
    """Deducts stock quantity for a specific ingredient directly, not tied to a recipe. item_id can be the exact id or just what the baker called it (e.g. 'strawberries') — it will be resolved automatically. reason should be one of: 'waste' (spoiled/thrown out), 'gift' (given away), or 'correction' (fixing a wrong stock count — no ingredient was actually consumed). This distinction matters because get_financial_summary counts waste/gift as real cost but excludes corrections."""
    if qty <= 0:
        return "Error: qty must be a positive number."

    resolved_id, error = _resolve_document_id("ingredients", item_id)
    if error:
        return error
    item_id = resolved_id

    doc_ref = db.collection("ingredients").document(item_id)
    doc = doc_ref.get()
    item_data = doc.to_dict()

    current_qty = item_data.get("stock_qty", 0.0)
    new_qty = max(0.0, current_qty - qty)
    doc_ref.update({"stock_qty": new_qty})

    _log_event(
        "manual_usage",
        {
            "item_id": item_id,
            "item_name": item_data.get("name", item_id),
            "qty": qty,
            "reason": reason,
            "cost": round(qty * item_data.get("unit_cost", 0.0), 2),
        },
    )
    return f"Successfully deducted {qty:.2f} from {item_id} ({reason}). New balance: {new_qty:.2f}"


@_handle_tool_errors
def add_ingredient(
    name: str,
    unit: str,
    unit_cost: float,
    initial_stock_qty: float = 0,
    reorder_threshold: float = 0,
    expiration_date: str = None,
) -> str:
    """Adds a brand-new ingredient to inventory tracking. Use this whenever the baker mentions an ingredient that isn't tracked yet — e.g. restock_ingredient or log_inventory_usage came back with 'no item found'. The only two things worth asking about if missing are unit (e.g. 'kg', 'g', 'dozen', 'unit' — determines how quantities are recorded going forward) and unit_cost (matters for accurate margins). Everything else — initial_stock_qty, reorder_threshold, expiration_date — is fully optional and defaults sensibly (0, 0, none); do NOT ask for these or treat any of them as required, just omit what the baker didn't mention."""
    if unit_cost < 0:
        return "Error: unit_cost can't be negative."
    if initial_stock_qty < 0 or reorder_threshold < 0:
        return "Error: stock quantities can't be negative."

    existing_id, _ = _resolve_document_id("ingredients", name)
    if existing_id:
        return f"'{name}' already exists as an ingredient ({existing_id}) — use restock_ingredient instead."

    doc_id = _unique_doc_id("ingredients", name)
    db.collection("ingredients").document(doc_id).set(
        {
            "id": doc_id,
            "name": name,
            "unit": unit,
            "unit_cost": unit_cost,
            "stock_qty": initial_stock_qty,
            "reorder_threshold": reorder_threshold,
            "expiration_date": expiration_date,
        }
    )
    _log_event(
        "add_ingredient",
        {"item_id": doc_id, "name": name, "unit": unit, "unit_cost": unit_cost, "initial_stock_qty": initial_stock_qty},
    )
    return f"Added new ingredient '{name}' ({doc_id}) — {initial_stock_qty:.2f} {unit} in stock at ${unit_cost:.2f}/{unit}."


@_handle_tool_errors
def restock_ingredient(item_id: str, qty: float, new_expiration_date: str = None) -> str:
    """Adds stock for an existing ingredient (e.g. after buying/receiving more). item_id can be the exact id or just what the baker called it (e.g. 'strawberries') — it will be resolved automatically. Optionally pass new_expiration_date (ISO format, e.g. '2026-09-15') to update the tracked expiration date for the item — this overwrites the prior date since stock isn't tracked per-batch."""
    if qty <= 0:
        return "Error: qty must be a positive number."

    resolved_id, error = _resolve_document_id("ingredients", item_id)
    if error:
        return error
    item_id = resolved_id

    doc_ref = db.collection("ingredients").document(item_id)
    doc = doc_ref.get()
    item_data = doc.to_dict()

    current_qty = item_data.get("stock_qty", 0.0)
    new_qty = current_qty + qty
    updates = {"stock_qty": new_qty}
    if new_expiration_date:
        updates["expiration_date"] = new_expiration_date
    doc_ref.update(updates)

    _log_event(
        "restock",
        {
            "item_id": item_id,
            "item_name": item_data.get("name", item_id),
            "qty": qty,
            "cost": round(qty * item_data.get("unit_cost", 0.0), 2),
        },
    )

    if new_expiration_date:
        return f"Restocked {qty:.2f} of {item_id}. New balance: {new_qty:.2f}. Expiration date updated to {new_expiration_date}."
    return f"Restocked {qty:.2f} of {item_id}. New balance: {new_qty:.2f}."


@_handle_tool_errors
def add_recipe(name: str, selling_price: float, ingredients: list[dict]) -> str:
    """Adds a brand-new recipe. Use this when the baker describes a product/recipe that doesn't exist yet. ingredients is a list like [{"item": "flour", "qty": 0.5, "unit": "kg"}, {"item": "eggs", "qty": 2, "unit": "dozen"}] — item names are resolved against existing tracked ingredients the same way other tools do. ALWAYS include "unit" for each ingredient (whatever unit the source used — cups, tbsp, kg, whatever the baker said or the photo/recipe showed) even though it's optional in the signature: without it, a quantity in the wrong unit (e.g. "2 cups" copied as qty=2 into an ingredient tracked in kg) would be silently stored as if it were already in the tracked unit, which is wrong by a large factor. If the unit you have doesn't match how the ingredient is tracked, this call will tell you the tracked unit so you can convert or ask the baker — do not guess a conversion yourself unless you're confident. Every ingredient must already be tracked for cost/margin calculations to be accurate — if one isn't found, this returns an error telling you which; use add_ingredient for those first, then retry."""
    if selling_price < 0:
        return "Error: selling_price can't be negative."

    existing_id, _ = _resolve_document_id("recipes", name)
    if existing_id:
        return f"'{name}' already exists as a recipe ({existing_id})."

    resolved_ingredients = []
    unresolved = []
    for ing in ingredients:
        item_query = ing.get("item")
        qty = ing.get("qty")
        source_unit = ing.get("unit")
        ing_id, ing_error = _resolve_document_id("ingredients", item_query)
        if not ing_id:
            unresolved.append(f"{item_query}: {ing_error}")
            continue

        tracked_unit = db.collection("ingredients").document(ing_id).get().to_dict().get("unit")
        if source_unit and tracked_unit and source_unit.strip().lower() != tracked_unit.strip().lower():
            unresolved.append(
                f"{item_query}: got {qty} {source_unit}, but this ingredient is tracked in "
                f"{tracked_unit} — convert it or ask the baker for the quantity in {tracked_unit}"
            )
            continue

        resolved_ingredients.append({"id": ing_id, "qty": qty})

    if unresolved:
        return (
            f"Can't add recipe '{name}' yet — {'; '.join(unresolved)}. "
            f"Fix these and retry (untracked ingredients need add_ingredient first)."
        )

    doc_id = _unique_doc_id("recipes", name)
    db.collection("recipes").document(doc_id).set(
        {"id": doc_id, "name": name, "selling_price": selling_price, "ingredients": resolved_ingredients}
    )
    _log_event("add_recipe", {"recipe_id": doc_id, "name": name, "selling_price": selling_price})
    return f"Added new recipe '{name}' ({doc_id}) at ${selling_price:.2f}."


@_handle_tool_errors
def bake_recipe(recipe_id: str, quantity: float = 1) -> str:
    """Deducts all ingredients for a recipe from inventory based on how many units were baked (e.g. 'made 3 pies'). recipe_id can be the exact id or just what the baker called it (e.g. 'strawberry pies') — it will be resolved automatically."""
    if quantity <= 0:
        return "Error: quantity must be a positive number."

    resolved_id, error = _resolve_document_id("recipes", recipe_id)
    if error:
        return error
    recipe_id = resolved_id

    recipe_doc = db.collection("recipes").document(recipe_id).get()
    recipe = recipe_doc.to_dict()
    needed = {}
    shortages = []

    for ing in recipe.get("ingredients", []):
        ing_id = ing["id"]
        needed_qty = ing["qty"] * quantity
        ing_doc = db.collection("ingredients").document(ing_id).get()
        ing_data = ing_doc.to_dict() if ing_doc.exists else {}
        current_qty = ing_data.get("stock_qty", 0.0)
        unit_cost = ing_data.get("unit_cost", 0.0)

        if current_qty < needed_qty:
            shortages.append(
                f"{ing_id} (need {needed_qty:.2f}, have {current_qty:.2f})"
            )
        needed[ing_id] = (needed_qty, current_qty, unit_cost)

    if shortages:
        return (
            f"Can't bake {quantity}x {recipe.get('name', recipe_id)} — "
            f"not enough stock: {', '.join(shortages)}."
        )

    deducted = []
    total_cost = 0.0
    for ing_id, (needed_qty, current_qty, unit_cost) in needed.items():
        new_qty = current_qty - needed_qty
        db.collection("ingredients").document(ing_id).update({"stock_qty": new_qty})
        deducted.append(f"{needed_qty:.2f} {ing_id}")
        total_cost += needed_qty * unit_cost

    _log_event(
        "bake",
        {
            "recipe_id": recipe_id,
            "recipe_name": recipe.get("name", recipe_id),
            "quantity": quantity,
            "ingredient_cost": round(total_cost, 2),
        },
    )

    return (
        f"Baked {quantity}x {recipe.get('name', recipe_id)} — "
        f"deducted: {', '.join(deducted)}."
    )


def _find_order(query: str):
    """Finds a custom order matching a customer name and/or item, preferring
    open (not yet paid/cancelled) orders when there's ambiguity — that's the
    common case (a baker is far more likely to mean their one open order for
    'Sarah' than an old paid one). Returns (doc, order_data, error_message).
    """
    query_lower = query.strip().lower()
    docs = list(db.collection("custom_orders").stream())

    all_matches = []
    open_matches = []
    for doc in docs:
        data = doc.to_dict()
        haystack = f"{data.get('customer', '')} {data.get('item', '')}".lower()
        if query_lower in haystack:
            all_matches.append((doc, data))
            if data.get("status") not in ("paid", "cancelled"):
                open_matches.append((doc, data))

    candidates = open_matches if open_matches else all_matches
    if len(candidates) == 1:
        return candidates[0][0], candidates[0][1], None
    if not candidates:
        return None, None, f"No order found matching '{query}'."

    options = ", ".join(
        f"{data.get('customer', '?')} — {data.get('item', '?')} ({data.get('status')})"
        for _, data in candidates
    )
    return None, None, f"Multiple orders match '{query}': {options}. Which one did you mean?"


@_handle_tool_errors
def record_custom_order(
    customer: str, item: str, quantity: float = 1, price: float = None, due_date: str = None
) -> str:
    """Records a custom order (e.g. from an Instagram DM). price and due_date are optional if not agreed yet — they, and the order's status, can be updated later with update_order_status as the order progresses (pending -> fulfilled -> paid). due_date MUST be ISO format ('2026-09-05'), never a relative word like 'Friday' or 'next week' — resolve relative dates against the current date yourself before calling this; other tools compare due_date as a plain string, so a non-ISO value silently breaks date filtering."""
    if quantity <= 0:
        return "Error: quantity must be a positive number."

    now = datetime.datetime.now().isoformat()
    db.collection("custom_orders").add(
        {
            "customer": customer,
            "item": item,
            "quantity": quantity,
            "price": price,
            "due_date": due_date,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        }
    )
    _log_event(
        "custom_order",
        {"customer": customer, "item": item, "quantity": quantity, "price": price},
    )

    summary = f"Recorded order: {quantity}x {item} for {customer}"
    if due_date:
        summary += f", due {due_date}"
    summary += f", ${price:.2f}." if price is not None else " (price not yet set)."
    return summary


@_handle_tool_errors
def update_order_status(query: str, new_status: str, price: float = None) -> str:
    """Updates a custom order's status as it progresses. query matches against the order's customer name and/or item (e.g. 'Sarah', 'sourdough') — if it's ambiguous you'll get a list of matches to disambiguate. new_status must be 'fulfilled' (baked/delivered), 'paid' (payment received), or 'cancelled'.

    IMPORTANT: marking an order 'paid' automatically logs that payment as revenue (same as record_sale). If the order already has a price on file and the baker doesn't mention a different amount, just use the existing price — don't ask for it again. Only ask for a price if the order has none, or explicitly pass one to override it (e.g. for a BOGO or discount, pass the actual amount received, not list price x quantity). Do NOT also call record_sale for the same order — that would double-count the revenue. record_sale is only for sales that were never tracked as a custom order (walk-ins, spontaneous same-day sales)."""
    valid_statuses = {"pending", "fulfilled", "paid", "cancelled"}
    if new_status not in valid_statuses:
        return f"Error: status must be one of {sorted(valid_statuses)}."

    doc, order, error = _find_order(query)
    if error:
        return error

    if new_status == "paid":
        if order.get("status") == "paid":
            return (
                f"Order for {order.get('customer')} ({order.get('item')}) is already "
                f"marked paid — not logging the revenue again."
            )

        final_price = price if price is not None else order.get("price")
        if final_price is None:
            return (
                "Error: this order has no price set yet. Pass a price to mark it paid, "
                "e.g. update_order_status(query, 'paid', price=15)."
            )

        doc.reference.update(
            {
                "status": "paid",
                "price": final_price,
                "updated_at": datetime.datetime.now().isoformat(),
            }
        )
        _log_event(
            "sale",
            {
                "recipe_id": None,
                "item_label": order.get("item"),
                "quantity": order.get("quantity", 1),
                "revenue": final_price,
                "order_id": doc.id,
            },
        )
        return (
            f"Order for {order.get('customer')} ({order.get('item')}) marked paid — "
            f"${final_price:.2f} recorded as revenue."
        )

    doc.reference.update(
        {"status": new_status, "updated_at": datetime.datetime.now().isoformat()}
    )
    return f"Order for {order.get('customer')} ({order.get('item')}) updated to '{new_status}'."


@_handle_tool_errors
def get_open_orders() -> list[dict]:
    """Returns custom orders that aren't yet paid or cancelled, sorted by due date. Use this for 'what do I still owe / what's outstanding' questions."""
    orders = [doc.to_dict() for doc in db.collection("custom_orders").stream()]
    open_orders = [o for o in orders if o.get("status") not in ("paid", "cancelled")]
    open_orders.sort(key=lambda o: o.get("due_date") or "9999-99-99")
    return open_orders


@_handle_tool_errors
def record_sale(item: str, quantity: float, revenue: float) -> str:
    """Records a direct sale not tied to a tracked custom order — money received for baked goods (e.g. 'sold 3 tarts for $24', walk-in/spontaneous sales). If the sale is closing out an order created with record_custom_order, use update_order_status(..., 'paid') instead — do not use both for the same transaction, that would double-count the revenue. Always pass the real amount actually received as revenue, never a recipe's selling_price x quantity — e.g. for a BOGO where 2 go out but only 1 is paid for, pass quantity=2 and revenue=<price of one>. item is matched against your recipes by name when possible, but this won't fail if it's a one-off/custom item not in your recipe list — it just records the label as given. This does not touch inventory (bake_recipe already handles stock)."""
    if quantity <= 0:
        return "Error: quantity must be a positive number."
    if revenue < 0:
        return "Error: revenue can't be negative."

    resolved_id, error = _resolve_document_id("recipes", item)
    if error and error.startswith("Multiple matches"):
        return error

    item_label = item
    if resolved_id:
        recipe_doc = db.collection("recipes").document(resolved_id).get()
        item_label = recipe_doc.to_dict().get("name", item)

    _log_event(
        "sale",
        {
            "recipe_id": resolved_id,
            "item_label": item_label,
            "quantity": quantity,
            "revenue": revenue,
            "order_id": None,
        },
    )
    return f"Recorded sale: {quantity}x {item_label} for ${revenue:.2f}."


@_handle_tool_errors
def record_expense(description: str, amount: float) -> str:
    """Records a non-ingredient business expense (e.g. packaging, utilities, marketing, shop fees) — money spent that isn't captured by restock_ingredient. Counted in get_financial_summary alongside restock spend and waste."""
    if amount <= 0:
        return "Error: amount must be a positive number."

    _log_event("expense", {"description": description, "amount": amount})
    return f"Recorded expense: {description} — ${amount:.2f}."


@_handle_tool_errors
def get_activity_log(days: int = 7) -> list[dict]:
    """Returns recent activity (bakes, restocks, manual usage/waste, custom orders) from the last N days, most recent first. Use this to answer questions about what happened over a period, e.g. 'what did I bake this week?'."""
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
    events = [
        doc.to_dict()
        for doc in db.collection("activity_log")
        .where(filter=FieldFilter("timestamp", ">=", cutoff))
        .stream()
    ]
    events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return events


@_handle_tool_errors
def get_financial_summary(days: int = 7) -> dict:
    """Summarizes money in vs. money out over the last N days (e.g. 'how did this week go?', 'give me a monthly summary'). Revenue = money received from record_sale events in the period. Cost = ingredient cost of what was actually baked (cost-of-goods-sold, from bake_recipe events) + waste/gift cost (excludes manual_usage logged as a 'correction', since no ingredient was actually lost) + non-ingredient expenses in the period. Ingredient cost is counted when it's baked, not when it's restocked — buying a big batch of flour isn't a 'cost' yet, it becomes one as it's used. restock_spend is still reported separately for reference (what you actually spent restocking this period), it's just not part of total_cost."""
    events = get_activity_log(days=days)
    if isinstance(events, str):
        return {"error": events}

    revenue = 0.0
    restock_spend = 0.0
    production_cost = 0.0
    waste_cost = 0.0
    other_expenses = 0.0
    sales_count = 0

    for event in events:
        details = event.get("details", {})
        event_type = event.get("type")
        if event_type == "sale":
            revenue += details.get("revenue", 0.0)
            sales_count += 1
        elif event_type == "restock":
            restock_spend += details.get("cost", 0.0)
        elif event_type == "bake":
            production_cost += details.get("ingredient_cost", 0.0)
        elif event_type == "manual_usage" and details.get("reason") != "correction":
            waste_cost += details.get("cost", 0.0)
        elif event_type == "expense":
            other_expenses += details.get("amount", 0.0)

    total_cost = production_cost + waste_cost + other_expenses

    return {
        "period_days": days,
        "revenue": round(revenue, 2),
        "sales_count": sales_count,
        "production_cost": round(production_cost, 2),
        "restock_spend": round(restock_spend, 2),
        "waste_cost": round(waste_cost, 2),
        "other_expenses": round(other_expenses, 2),
        "total_cost": round(total_cost, 2),
        "net": round(revenue - total_cost, 2),
    }


def _ingredient_shortfalls(recipe_quantities: dict) -> list[dict]:
    """Given {recipe_id: total_quantity}, totals ingredient needs across all of
    them and returns only the ones short of current stock. Shared by
    generate_shopping_list and get_production_plan."""
    ingredients_needed = {}
    for recipe_id, quantity in recipe_quantities.items():
        recipe = db.collection("recipes").document(recipe_id).get().to_dict()
        for ing in recipe.get("ingredients", []):
            ingredients_needed[ing["id"]] = (
                ingredients_needed.get(ing["id"], 0.0) + ing["qty"] * quantity
            )

    to_buy = []
    for ing_id, needed_qty in ingredients_needed.items():
        ing_doc = db.collection("ingredients").document(ing_id).get()
        ing_data = ing_doc.to_dict() if ing_doc.exists else {}
        current_qty = ing_data.get("stock_qty", 0.0)
        shortfall = needed_qty - current_qty
        if shortfall > 0:
            to_buy.append(
                {
                    "item": ing_data.get("name", ing_id),
                    "need": round(needed_qty, 2),
                    "have": round(current_qty, 2),
                    "buy": round(shortfall, 2),
                    "unit": ing_data.get("unit", ""),
                }
            )
    return to_buy


@_handle_tool_errors
def generate_shopping_list(planned_bakes: list[dict]) -> dict:
    """Given a list of planned bakes, e.g. [{"recipe": "strawberry tart", "quantity": 5}, {"recipe": "sourdough", "quantity": 3}], totals the ingredients needed across all of them and compares against current stock. Returns what needs buying (shortfalls only) and their quantities. Use this when the baker describes what they're planning to bake for the day/week and wants a shopping list. recipe values are matched by name automatically, same as other tools."""
    recipe_quantities = {}
    unresolved = []

    for entry in planned_bakes:
        recipe_query = entry.get("recipe")
        quantity = entry.get("quantity", 1)
        resolved_id, error = _resolve_document_id("recipes", recipe_query)
        if error:
            unresolved.append(f"{recipe_query}: {error}")
            continue
        recipe_quantities[resolved_id] = recipe_quantities.get(resolved_id, 0.0) + quantity

    result = {"shopping_list": _ingredient_shortfalls(recipe_quantities)}
    if unresolved:
        result["unresolved"] = unresolved
    return result


@_handle_tool_errors
def get_production_plan(days: int = 7) -> dict:
    """Builds a bake list from open orders due within the next N days (orders with no due date are included too, treated as needed soon), aggregated by recipe, plus what ingredients are short to cover it. Use this for 'what do I need to bake this week' / 'plan my week' questions. Orders whose item doesn't match a known recipe are listed separately since they can't be planned for automatically — mention those to the baker rather than dropping them silently."""
    cutoff = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
    orders = get_open_orders()
    if isinstance(orders, str):
        return {"error": orders}

    recipe_quantities = {}
    recipe_names = {}
    unmatched_orders = []

    for order in orders:
        due = order.get("due_date")
        if due and due > cutoff:
            continue

        item = order.get("item", "")
        quantity = order.get("quantity", 1)
        resolved_id, error = _resolve_document_id("recipes", item)
        if not resolved_id:
            unmatched_orders.append(
                {"customer": order.get("customer"), "item": item, "quantity": quantity, "note": error}
            )
            continue

        recipe_quantities[resolved_id] = recipe_quantities.get(resolved_id, 0.0) + quantity
        if resolved_id not in recipe_names:
            recipe_doc = db.collection("recipes").document(resolved_id).get()
            recipe_names[resolved_id] = recipe_doc.to_dict().get("name", resolved_id)

    result = {
        "bake_list": [
            {"recipe": recipe_names[rid], "quantity": qty}
            for rid, qty in recipe_quantities.items()
        ],
        "shopping_list": _ingredient_shortfalls(recipe_quantities),
    }
    if unmatched_orders:
        result["unmatched_orders"] = unmatched_orders
    return result


@_handle_tool_errors
def get_accounts_receivable() -> dict:
    """Returns total money owed from open (not yet paid) orders that have a price set, plus the list of those orders. Use this for 'what do I still owe / what's owed to me' questions. Orders without a price yet are listed separately since the amount owed isn't known."""
    orders = get_open_orders()
    if isinstance(orders, str):
        return {"error": orders}

    owed_orders = [o for o in orders if o.get("price") is not None]
    missing_price = [o for o in orders if o.get("price") is None]
    total_owed = sum(o.get("price", 0.0) for o in owed_orders)

    result = {"total_owed": round(total_owed, 2), "orders": owed_orders}
    if missing_price:
        result["orders_missing_price"] = missing_price
    return result


@_handle_tool_errors
def suggest_price(recipe_id: str, target_margin_pct: float = 70) -> dict:
    """Suggests a selling price for a recipe given a target profit margin percentage (default 70, a reasonable bakery starting point). Computed from current ingredient cost: price = cost / (1 - target_margin_pct/100). This is a suggestion only — it does not change the recipe's stored selling_price."""
    if not (0 <= target_margin_pct < 100):
        return {"error": "target_margin_pct must be between 0 and 100 (exclusive of 100)."}

    resolved_id, error = _resolve_document_id("recipes", recipe_id)
    if error:
        return {"error": error}

    recipe = db.collection("recipes").document(resolved_id).get().to_dict()
    ingredients = {doc.id: doc.to_dict() for doc in db.collection("ingredients").stream()}
    cost = 0.0
    for ing in recipe.get("ingredients", []):
        ing_data = ingredients.get(ing["id"], {})
        cost += ing_data.get("unit_cost", 0.0) * ing["qty"]

    suggested_price = cost / (1 - target_margin_pct / 100)

    return {
        "recipe_name": recipe.get("name", resolved_id),
        "ingredient_cost": round(cost, 2),
        "target_margin_pct": target_margin_pct,
        "suggested_price": round(suggested_price, 2),
        "current_selling_price": recipe.get("selling_price"),
    }


# ---------------------------------------------------------
# Memory Bank Tools & Partner System Prompt
# ---------------------------------------------------------
@_handle_tool_errors
def get_baker_preferences() -> list[str]:
    """Fetches long-term preferences and rules set by the baker from Firestore."""
    docs = db.collection("baker_preferences").stream()
    return [doc.to_dict().get("rule") for doc in docs if "rule" in doc.to_dict()]


@_handle_tool_errors
def save_baker_preference(rule: str) -> str:
    """Saves a new rule/preference into long-term memory (e.g., 'Reduce tart production on rainy days')."""
    db.collection("baker_preferences").add(
        {"rule": rule, "saved_at": datetime.datetime.now().isoformat()}
    )
    return f"Saved rule to Memory Bank: '{rule}'"


@_handle_tool_errors
def save_customer_note(customer: str, note: str) -> str:
    """Saves a note about a specific customer — allergies, preferences, repeat-order habits (e.g. 'Sarah is allergic to nuts', 'Mike always orders sourdough on Fridays')."""
    db.collection("customer_notes").add(
        {"customer": customer, "note": note, "saved_at": datetime.datetime.now().isoformat()}
    )
    return f"Saved note for {customer}: '{note}'"


@_handle_tool_errors
def get_customer_notes(customer: str = None) -> list[dict]:
    """Fetches saved customer notes. Pass a customer name to filter to just that person (substring match), or omit to get all notes."""
    notes = [doc.to_dict() for doc in db.collection("customer_notes").stream()]
    if customer:
        customer_lower = customer.strip().lower()
        notes = [n for n in notes if customer_lower in n.get("customer", "").lower()]
    return notes


# Master Tools List registered for Gemini
tools = [
    get_inventory_status,
    calculate_recipe_margins,
    log_inventory_usage,
    add_ingredient,
    restock_ingredient,
    add_recipe,
    bake_recipe,
    record_custom_order,
    record_sale,
    record_expense,
    update_order_status,
    save_baker_preference,
    get_activity_log,
    get_financial_summary,
    get_open_orders,
    generate_shopping_list,
    get_production_plan,
    get_accounts_receivable,
    suggest_price,
    save_customer_note,
    get_customer_notes,
]

SYSTEM_INSTRUCTION = """
You are an interactive Bakery Operations Co-Pilot working closely with the head baker.

TOOL-FIRST RULE (most important — follow this before anything else below): when the baker describes something that happened (bought X, made X, sold X), call the matching tool immediately with whatever details you already have. Do not ask a clarifying question before attempting the tool call — the tool's response (success, or an error like "no item found") is what tells you what to ask next, not your own guess. Only fall back to asking first if the baker's message is missing something no tool call could possibly work without (e.g. no quantity at all).

Key Behaviors:
1. COLLABORATIVE PARTNER: Act like an experienced assistant manager, not a passive chatbot. Proactively flag problems (e.g. expiring stock) and suggest clear operational choices.
2. MEMORY BANK INTEGRATION: Use save_baker_preference whenever the baker gives high-level rules or preferences (e.g. "We don't bake tarts on rainy days").
3. INVENTORY CONTROL: Call bake_recipe when the baker says they made/baked something tied to a recipe (e.g. "I made 3 strawberry tarts") — it looks up the recipe's ingredients and deducts them automatically. When the baker says they bought/received something, DON'T ask clarifying questions up front — call restock_ingredient right away with the quantity given. Its response tells you what to do next:
   - If it succeeds, you're done (see the expiration-date handling below).
   - If it says "no item found," that's your signal this is a brand-new ingredient — switch to the add_ingredient flow (rule 4) instead of retrying restock_ingredient or asking about expiration dates, which don't apply yet.
   Restocking an EXISTING ingredient overwrites its tracked expiration date rather than tracking separate batches, so once you know restock_ingredient is the right call: if the baker already mentioned a best-by/expiration date, use it; if they didn't, ask them for one (e.g. "Got it — when do these expire?") rather than silently leaving the old date in place; if they say they don't know or don't want to specify, restock without changing it rather than blocking. This expiration-date question is ONLY relevant once you're sure you're restocking an existing item — never ask it before you've confirmed that, and it never applies to add_ingredient (rule 4 covers what that actually needs). Use log_inventory_usage instead of restock_ingredient only for one-off manual deductions not tied to a recipe — always pass the correct reason ('waste' for spoiled/thrown out, 'gift' for given away, 'correction' for fixing a wrong stock count), since this affects what counts as real cost in get_financial_summary.
4. NEVER ask the baker for an internal item/recipe id (e.g. "strawberries_001") — they don't know it and shouldn't need to. Just pass whatever name they used (e.g. "strawberries") straight into the tool; it resolves names automatically. If a tool comes back saying multiple items matched, relay those options to the baker by name so they can pick, then retry with their choice. If a tool comes back saying "no item found" for an ingredient or recipe, don't just report that you can't do it — that almost always means it needs to be added first. Use add_ingredient (asking for unit and unit_cost if not given) or add_recipe (asking for the ingredient list and selling price if not given), then retry the original action.
5. ACTIVITY HISTORY: Every bake, restock, manual usage, custom order, and sale is automatically logged with a timestamp — you don't need to log anything yourself. Use get_activity_log for "what happened" questions (e.g. "what did I bake this week?"), and get_financial_summary for "how did I do" questions (e.g. "how was this week?", "give me a monthly summary") — it computes actual revenue vs. money spent, not theoretical recipe margins.
6. REVENUE: record_sale is only for sales never tracked as a custom order — walk-ins, spontaneous same-day sales (e.g. "sold 3 tarts for $24"). If a sale is closing out an order the baker previously placed with record_custom_order, use update_order_status(query, "paid", price) instead — NEVER call both for the same transaction, that double-counts the revenue. Always pass the real amount actually received, never a recipe's selling_price x quantity (e.g. a BOGO: 2 units go out, but revenue is only the price of 1). Call record_expense for non-ingredient business spending (packaging, utilities, marketing, fees) — this also feeds into get_financial_summary.
7. CUSTOM ORDERS: Call record_custom_order when the baker mentions a custom order (e.g. from an Instagram DM) — price and due date are optional if not agreed yet. As the order progresses, call update_order_status to move it to "fulfilled" (baked/delivered) or "paid" (payment received — this auto-logs the revenue, see #6). Use get_open_orders for "what's outstanding / what do I still owe" questions. If update_order_status reports multiple matching orders, relay the options to the baker by customer/item so they can pick.
8. PLANNING: Call generate_shopping_list when the baker describes what they're planning to bake (for the day/week) and wants to know what to buy — pass each planned item as a recipe name + quantity. It compares total ingredient needs against current stock and returns only the shortfalls. Use get_production_plan instead when the baker wants a plan based on what's already been ordered (e.g. "what do I need to bake this week?") — it builds the bake list from open orders automatically rather than requiring the baker to list items manually.
9. MONEY OWED & PRICING: Use get_accounts_receivable for "what's owed to me / what am I still waiting on" questions — total across unpaid priced orders. Use suggest_price when the baker wants pricing help (e.g. "what should I charge for this?") — it suggests a price from ingredient cost and a target margin, but never changes the recipe's actual stored price yourself.
10. CUSTOMER NOTES: Use save_customer_note for anything worth remembering about a specific customer (allergies, preferences, order habits) and get_customer_notes to recall it later — check this before finalizing orders/sales for a customer you have notes on, e.g. to flag an allergy.
11. PHOTO/PDF UPLOADS: When the baker attaches a photo or PDF (a handwritten order note, a supplier invoice, a recipe card, etc.), read it and summarize what you found, but do NOT immediately call a tool that changes data (record_custom_order, restock_ingredient, record_sale, record_expense, add_recipe, etc.) if any of it is hard to read, ambiguous, or ambiguous about dates/amounts — ask the baker to confirm the specific uncertain details first. Only call tools right away if the content is clearly legible and unambiguous. This includes units: a recipe card or note may use different units (cups, tbsp) than what's tracked in inventory (kg, dozen) — always pass the unit you actually read to add_recipe rather than assuming it matches, and never invent a conversion you're not confident about.
"""


# ---------------------------------------------------------
# Main Execution / Query Function
# ---------------------------------------------------------
def create_chat_session():
    """Starts a new multi-turn chat session with tools + current baker preferences.

    The SDK's chat object keeps the running message history internally, so
    each subsequent send_message() call remembers prior turns. Preferences
    are pulled in once, at session start.
    """
    preferences = get_baker_preferences()
    pref_context = (
        f"\n\nActive Baker Preferences (Memory Bank): {preferences}"
        if preferences
        else ""
    )

    return client.chats.create(
        model=MODEL_NAME,
        config={
            "tools": tools,
            "system_instruction": SYSTEM_INSTRUCTION + pref_context,
        },
    )


def ask_bakery_copilot(prompt: str):
    """One-shot query with no conversation history (e.g. for scripts/testing)."""
    return create_chat_session().send_message(prompt).text


def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/wav") -> str:
    """One-shot transcription with no tools/history — the transcript is then
    fed through the normal chat session's send_message like any typed
    message, so voice input inherits all the same tool-calling behavior
    (and safety guards) without any special-casing."""
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=[
            types.Part.from_text(
                text="Transcribe this audio exactly as spoken. Return only the transcript, nothing else."
            ),
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
        ],
    )
    return response.text