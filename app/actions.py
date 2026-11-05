"""Audit log of changes Claude made, plus approval-gated actions (removals)."""
import uuid
from datetime import datetime, timezone

from . import db


def _now() -> datetime:
    return datetime.now(timezone.utc)


def public(doc: dict) -> dict:
    return {"id": doc["_id"], "kind": doc["kind"], "summary": doc["summary"], "status": doc["status"],
            "created_at": doc["created_at"].isoformat(), "error": doc.get("error")}


def record(kind: str, summary: str, payload: dict, status: str = "applied") -> dict:
    doc = {"_id": uuid.uuid4().hex, "kind": kind, "summary": summary, "payload": payload,
           "status": status, "created_at": _now()}
    db.actions.insert_one(doc)
    return doc


def pending_for(kind: str, payload_id: str) -> dict | None:
    return db.actions.find_one({"kind": kind, "status": "pending", "payload.id": payload_id})


def list_recent(limit: int = 15) -> dict:
    pending = list(db.actions.find({"status": "pending"}).sort("created_at", -1))
    recent = list(db.actions.find({"status": {"$ne": "pending"}}).sort("created_at", -1).limit(limit))
    return {"pending": [public(d) for d in pending], "recent": [public(d) for d in recent]}


def claim(action_id: str, status: str) -> dict | None:
    """Atomically move a pending action to `status`; None if it was not pending (already handled)."""
    return db.actions.find_one_and_update(
        {"_id": action_id, "status": "pending"}, {"$set": {"status": status, "resolved_at": _now()}},
        return_document=True)


def finish(action_id: str, status: str, error: str | None = None) -> None:
    db.actions.update_one({"_id": action_id}, {"$set": {"status": status, "error": error}})
