import os

from pymongo import ASCENDING, DESCENDING, MongoClient

_client = MongoClient(os.environ.get("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = _client[os.environ.get("MONGO_DB", "wedding_flights")]

quotes = db.quotes              # one doc per search per run
plan_history = db.plan_history  # best total per (destination, plan type) per run
runs = db.runs                  # run log + SerpApi usage counting


def ensure_indexes() -> None:
    quotes.create_index([("key", ASCENDING), ("fetched_at", DESCENDING)])
    quotes.create_index([("run_id", ASCENDING)])
    plan_history.create_index([("destination_id", ASCENDING), ("at", ASCENDING)])
    runs.create_index([("started_at", DESCENDING)])


def latest_quotes(keys) -> dict:
    """Most recent successful quote for each search key."""
    pipeline = [
        {"$match": {"key": {"$in": list(keys)}, "min_price": {"$ne": None}}},
        {"$sort": {"fetched_at": -1}},
        {"$group": {"_id": "$key", "doc": {"$first": "$$ROOT"}}},
    ]
    return {row["_id"]: row["doc"] for row in quotes.aggregate(pipeline)}
