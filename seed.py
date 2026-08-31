import datetime
import os

from google.cloud import firestore

# Credentials are resolved via Application Default Credentials (ADC); see
# agent.py for setup notes. Never load a key file checked into the repo.
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "bakery-agentic-copilot")
db = firestore.Client(project=PROJECT_ID)

# 1. Seed Ingredients
ingredients = [
    {
        "id": "flour_001",
        "name": "Bread Flour",
        "stock_qty": 45.0,
        "unit": "kg",
        "unit_cost": 1.20,
        "reorder_threshold": 10.0,
        "expiration_date": (
            datetime.date.today() + datetime.timedelta(days=60)
        ).isoformat(),
    },
    {
        "id": "butter_001",
        "name": "Unsalted Butter",
        "stock_qty": 8.0,
        "unit": "kg",
        "unit_cost": 6.50,
        "reorder_threshold": 10.0,
        "expiration_date": (
            datetime.date.today() + datetime.timedelta(days=14)
        ).isoformat(),
    },
    {
        "id": "strawberries_001",
        "name": "Fresh Strawberries",
        "stock_qty": 4.0,
        "unit": "kg",
        "unit_cost": 5.00,
        "reorder_threshold": 2.0,
        "expiration_date": (
            datetime.date.today() + datetime.timedelta(days=1)
        ).isoformat(),
    },
]

for item in ingredients:
    db.collection("ingredients").document(item["id"]).set(item)
print("✅ Ingredients seeded successfully.")

# 2. Seed Recipes
recipes = [
    {
        "id": "strawberry_tart",
        "name": "Strawberry Tart",
        "selling_price": 8.00,
        "ingredients": [
            {"id": "strawberries_001", "qty": 0.3},
            {"id": "flour_001", "qty": 0.1},
            {"id": "butter_001", "qty": 0.1},
        ],
    },
    {
        "id": "sourdough_bread",
        "name": "Artisan Sourdough",
        "selling_price": 7.50,
        "ingredients": [
            {"id": "flour_001", "qty": 0.5},
        ],
    },
]

for recipe in recipes:
    db.collection("recipes").document(recipe["id"]).set(recipe)
print("✅ Recipes seeded successfully.")