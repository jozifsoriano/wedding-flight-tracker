import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CONFIG_PATH", str(ROOT / "config.yaml"))
os.environ.setdefault("MONGO_URI", "mongodb://localhost:1")   # collections are swapped for mongomock below, never contacted

import mongomock  # noqa: E402
import pytest  # noqa: E402

from app import db  # noqa: E402
from app.config import Config  # noqa: E402


@pytest.fixture(autouse=True)
def mdb(monkeypatch):
    """Every test gets a fresh in-memory Mongo and no SerpApi key from the real environment."""
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    client = mongomock.MongoClient(tz_aware=True)
    database = client["wedding_flights_test"]
    monkeypatch.setattr(db, "db", database)
    for name in ("quotes", "plan_history", "runs"):
        monkeypatch.setattr(db, name, database[name])
    db.ensure_indexes()
    yield database


def make_cfg(**overrides) -> Config:
    """Small config: one outbound date, one honeymoon window, DAD gateway, one destination."""
    data = {
        "dates": {"outbound": ["2027-03-05"], "honeymoon_start": ["2027-03-15"], "honeymoon_end": ["2027-03-19"]},
        "gateways": ["DAD"],
        "schedule": {"request_delay_seconds": 0},
        "honeymoon": [{"id": "tokyo", "name": "Tokyo", "country": "Japan", "airports": ["HND"]}],
    }
    data.update(overrides)
    return Config.model_validate(data)


@pytest.fixture
def cfg() -> Config:
    return make_cfg()


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


def quote(price, fetched_at=None, **extra) -> dict:
    """A stored quote doc as tracker.run_all writes it (only the fields plans/db read)."""
    return {"min_price": price, "fetched_at": fetched_at or at(1), "price_level": "typical",
            "booking_url": "https://example.test/flights", "offers": [{"airline": "X", "price": price}], **extra}


def priced(searches, price=100) -> dict:
    """`latest` mapping that prices every search at the same amount."""
    return {s.key: quote(price) for s in searches}
