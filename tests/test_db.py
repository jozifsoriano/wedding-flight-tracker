from app import db
from tests.conftest import at


def test_latest_quotes_returns_newest_successful_quote_per_key():
    db.quotes.insert_many([
        {"key": "a", "min_price": 500, "fetched_at": at(1)},
        {"key": "a", "min_price": 450, "fetched_at": at(2)},
        {"key": "a", "min_price": None, "fetched_at": at(3), "error": "blocked"},   # failed run is ignored
        {"key": "b", "min_price": 900, "fetched_at": at(1)},
        {"key": "c", "min_price": 100, "fetched_at": at(5)},                         # not requested
    ])
    latest = db.latest_quotes({"a", "b", "missing"})
    assert set(latest) == {"a", "b"}
    assert latest["a"]["min_price"] == 450
    assert latest["b"]["min_price"] == 900


def test_latest_quotes_empty():
    assert db.latest_quotes(["a"]) == {}
