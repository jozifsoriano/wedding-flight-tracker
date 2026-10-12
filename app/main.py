import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, tracker
from .config import load_config
from .plans import PLAN_TYPES, best_by, compute_plans
from .searches import build_searches

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"

cfg = load_config()
scheduler = BackgroundScheduler(timezone="UTC")


def schedule_job(run_now: bool) -> None:
    scheduler.add_job(
        tracker.run_all, "interval", hours=cfg.schedule.interval_hours, id="refresh",
        replace_existing=True, max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + (timedelta(seconds=5) if run_now else
                                                    timedelta(hours=cfg.schedule.interval_hours)),
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.ensure_indexes()
    scheduler.start()
    schedule_job(cfg.schedule.run_on_startup)
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Wedding Flight Tracker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _iso(d):
    return d.isoformat() if d else None


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/overview")
def overview():
    searches = build_searches(cfg)
    plans = compute_plans(cfg, db.latest_quotes({s.key for s in searches}))
    last_run = db.runs.find_one(sort=[("started_at", -1)])
    job = scheduler.get_job("refresh")
    best_dest = sorted(best_by(plans, "destination_id").values(), key=lambda p: p["total"])
    return {
        "trip": {
            "home": cfg.home, "wedding_city": cfg.wedding_city, "gateways": cfg.gateways,
            "adults": cfg.party.adults, "currency": cfg.currency, "cabins": cfg.cabins.model_dump(),
            "dates": cfg.dates.model_dump(),
        },
        "destinations": [d.model_dump() for d in cfg.honeymoon],
        "plan_types": PLAN_TYPES,
        "best_by_destination": best_dest,
        "best_by_destination_type": list(best_by(plans, "destination_id", "type").values()),
        "plans": plans,
        "status": {
            **tracker.state,
            "search_count": len(searches),
            "serpapi_enabled": bool(cfg.serpapi_key),
            "serpapi_used_this_month": tracker.serpapi_used_this_month(),
            "serpapi_budget": cfg.providers.serpapi_monthly_budget,
            "next_run": _iso(job.next_run_time) if job else None,
            "last_run": last_run and {
                "started_at": _iso(last_run["started_at"]), "finished_at": _iso(last_run.get("finished_at")),
                "ok": last_run["ok"], "errors": last_run["errors"], "searches": last_run["searches"],
            },
        },
    }


@app.get("/api/history")
def history(destination_id: str):
    """Best total for 2 per plan type, per run, for one destination."""
    rows = db.plan_history.find({"destination_id": destination_id}, {"_id": 0}).sort("at", 1)
    series = {}
    for r in rows:
        series.setdefault(r["type"], []).append({"at": _iso(r["at"]), "total": r["total"]})
    return series


@app.get("/api/sparklines")
def sparklines():
    """Cheapest plan (any type) per destination per run."""
    pipeline = [
        {"$group": {"_id": {"d": "$destination_id", "run": "$run_id"}, "at": {"$first": "$at"},
                    "total": {"$min": "$total"}}},
        {"$sort": {"at": 1}},
    ]
    out = {}
    for r in db.plan_history.aggregate(pipeline):
        out.setdefault(r["_id"]["d"], []).append({"at": _iso(r["at"]), "total": r["total"]})
    return out


@app.get("/api/searches")
def searches():
    out = []
    for s in build_searches(cfg):
        last = db.quotes.find_one({"key": s.key}, {"_id": 0}, sort=[("fetched_at", -1)])
        out.append({**s.to_doc(), "last": last and {**last, "fetched_at": _iso(last["fetched_at"])}})
    return out


@app.get("/api/search_history")
def search_history(key: str):
    rows = db.quotes.find({"key": key, "min_price": {"$ne": None}}, {"_id": 0, "fetched_at": 1, "min_price": 1})
    return [{"at": _iso(r["fetched_at"]), "total": r["min_price"]} for r in rows.sort("fetched_at", 1)]


@app.post("/api/run")
def run_now():
    if tracker.state["running"]:
        raise HTTPException(409, "A refresh is already running")
    threading.Thread(target=tracker.run_all, args=(cfg,), daemon=True).start()
    return {"started": True}


@app.post("/api/reload")
def reload_config():
    global cfg
    try:
        cfg = load_config()
    except Exception as e:
        raise HTTPException(400, f"config.yaml is invalid: {e}")
    schedule_job(run_now=False)
    return {"reloaded": True, "searches": len(build_searches(cfg))}
