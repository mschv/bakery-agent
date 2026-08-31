import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# agent.py builds a real firestore.Client and genai.Client at import time,
# which normally requires GCP credentials. Patch both before the first
# import so the whole suite runs with no credentials and no network calls.
with patch("google.cloud.firestore.Client", return_value=MagicMock()), patch(
    "google.genai.Client", return_value=MagicMock()
):
    import agent  # noqa: E402

from tests.fake_firestore import FakeFirestoreClient  # noqa: E402


@pytest.fixture
def fake_db(monkeypatch):
    """Fresh in-memory Firestore for each test, pre-seeded with the same
    ingredients/recipes shape as seed.py, so tests reflect real usage."""
    db = FakeFirestoreClient()

    db.seed(
        "ingredients",
        "flour_001",
        {
            "id": "flour_001",
            "name": "Bread Flour",
            "stock_qty": 45.0,
            "unit": "kg",
            "unit_cost": 1.20,
            "reorder_threshold": 10.0,
            "expiration_date": "2026-12-01",
        },
    )
    db.seed(
        "ingredients",
        "butter_001",
        {
            "id": "butter_001",
            "name": "Unsalted Butter",
            "stock_qty": 8.0,
            "unit": "kg",
            "unit_cost": 6.50,
            "reorder_threshold": 10.0,
            "expiration_date": "2026-09-15",
        },
    )
    db.seed(
        "ingredients",
        "strawberries_001",
        {
            "id": "strawberries_001",
            "name": "Fresh Strawberries",
            "stock_qty": 4.0,
            "unit": "kg",
            "unit_cost": 5.00,
            "reorder_threshold": 2.0,
            "expiration_date": "2026-09-01",
        },
    )
    db.seed(
        "recipes",
        "strawberry_tart",
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
    )
    db.seed(
        "recipes",
        "sourdough_bread",
        {
            "id": "sourdough_bread",
            "name": "Artisan Sourdough",
            "selling_price": 7.50,
            "ingredients": [{"id": "flour_001", "qty": 0.5}],
        },
    )

    monkeypatch.setattr(agent, "db", db)
    return db
