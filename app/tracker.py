"""Runs every search, stores quotes in Mongo, rebuilds plans and sends alerts."""
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from . import db
from .alerts import notify
from .config import Config, load_config
from .plans import best_by, compute_plans
from .providers import google, google_flights_url, serpapi
from .searches import build_searches

log = logging.getLogger(__name__)

_lock = threading.Lock()
state = {"running": False, "progress": 0, "total": 0, "current": None}


def now() -> datetime:
    return datetime.now(timezone.utc)


def serpapi_used_this_month() -> int:
    start = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return db.quotes.count_documents({"provider": "serpapi", "fetched_at": {"$gte": start}})


def run_all(cfg: Optional[Config] = None) -> Optional[str]:
    """Price every search once. Returns the run id, or None if a run is already going."""
    if not _lock.acquire(blocking=False):
        return None
    run_id = uuid.uuid4().hex[:12]
    try:
        cfg = cfg or load_config()
        searches = build_searches(cfg)
        state.update(running=True, progress=0, total=len(searches), current=None)
        db.runs.insert_one({"_id": run_id, "started_at": now(), "searches": len(searches), "ok": 0, "errors": 0})
        serp_left = cfg.providers.serpapi_monthly_budget - serpapi_used_this_month()
        ok = errors = 0

        for i, s in enumerate(searches):
            state.update(progress=i, current=s.label)
            doc = {**s.to_doc(), "run_id": run_id, "fetched_at": now(),
                   "booking_url": google_flights_url(s, cfg.party.adults, cfg.currency)}
            try:
                if s.provider == "serpapi":
                    if not cfg.serpapi_key:
                        raise RuntimeError("SERPAPI_KEY not set")
                    if serp_left <= 0:
                        raise RuntimeError("SerpApi monthly budget used up")
                    serp_left -= 1
                    doc.update(serpapi.search(s, cfg.party.adults, cfg.currency, cfg.serpapi_key))
                else:
                    doc.update(_google_with_retry(s, cfg))
                doc["error"] = None if doc.get("min_price") else "no prices returned"
            except Exception as e:  # keep going; one bad route shouldn't kill the run
                log.warning("search failed %s: %s", s.key, e)
                doc.update(min_price=None, offers=[], error=str(e)[:300])
            db.quotes.insert_one(doc)
            ok, errors = (ok + 1, errors) if doc["min_price"] else (ok, errors + 1)
            if s.provider == "google" and i < len(searches) - 1:
                time.sleep(cfg.schedule.request_delay_seconds)

        _record_plans(cfg, run_id)
        db.runs.update_one({"_id": run_id}, {"$set": {"finished_at": now(), "ok": ok, "errors": errors}})
        log.info("run %s done: %d ok, %d errors", run_id, ok, errors)
        return run_id
    finally:
        state.update(running=False, current=None)
        _lock.release()


def _google_with_retry(s, cfg: Config) -> dict:
    try:
        return google.search(s, cfg.party.adults, cfg.currency)
    except Exception as e:
        if "No flights found" in str(e):
            raise RuntimeError("No flights found on Google for this route/date/cabin") from None
        time.sleep(30)
        return google.search(s, cfg.party.adults, cfg.currency)


def _record_plans(cfg: Config, run_id: str) -> None:
    keys = {s.key for s in build_searches(cfg)}
    plans = compute_plans(cfg, db.latest_quotes(keys))
    at = now()

    rows = [{
        "run_id": run_id, "at": at, "destination_id": dest_id, "type": ptype,
        "total": pl["total"], "plan_id": pl["id"],
    } for (dest_id, ptype), pl in best_by(plans, "destination_id", "type").items()]

    # Alert on new lows: compare each destination's best plan to its history.
    for dest_id, pl in best_by(plans, "destination_id").items():
        dest_id = dest_id[0]
        prev = db.plan_history.find_one({"destination_id": dest_id}, sort=[("total", 1)])
        drop_ok = prev and pl["total"] < prev["total"] * (1 - cfg.alerts.min_drop_pct / 100)
        under_target = cfg.alerts.target_total and pl["total"] <= cfg.alerts.target_total
        if drop_ok or under_target:
            was = f" (was ${prev['total']:,})" if prev else ""
            notify(cfg, f"✈️ New low: {pl['destination']}",
                   f"${pl['total']:,} for 2{was} — {pl['type_label']} via {pl['gateway']}, "
                   f"leaving {pl['dates']['outbound']}",
                   pl["components"][0].get("url") or "")

    if rows:
        db.plan_history.insert_many(rows)
