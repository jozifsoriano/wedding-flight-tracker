import logging
import os
import re
import tempfile
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from . import actions, assistant, auth, db, mcp, slicktrip, tracker
from .config import (CONFIG_PATH, Config, TrackedSearch, gui_destinations_path, gui_searches_path, load_config,
                     read_search_store, read_store)
from .plans import PLAN_TYPES, alternatives, best_by, compute_plans
from .searches import build_searches, flex_dates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"

cfg = load_config(CONFIG_PATH)
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

# Reachable without a session cookie. /mcp has its own per-question bearer token; /healthz is for Docker.
PUBLIC_PATHS = {"/healthz", "/mcp", "/api/auth/status", "/api/auth/login", "/api/auth/logout",
                "/static/style.css", "/static/login.js"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    if (not auth.enabled() or request.url.path in PUBLIC_PATHS
            or auth.valid_session(request.cookies.get(auth.COOKIE))):
        return await call_next(request)
    if request.url.path == "/":
        return FileResponse(STATIC / "login.html")
    return JSONResponse({"detail": "Login required"}, status_code=401)


class LoginRequest(BaseModel):
    password: str = Field(max_length=500)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/auth/status")
def auth_status(request: Request):
    return {"enabled": auth.enabled(), "logged_in": auth.valid_session(request.cookies.get(auth.COOKIE))}


@app.post("/api/auth/login")
def login(body: LoginRequest, request: Request):
    if not auth.enabled():
        raise HTTPException(404, "Login is not configured")
    client = request.client.host if request.client else "unknown"
    if auth.throttled(client):
        raise HTTPException(429, "Too many failed attempts; try again in a few minutes")
    if not auth.check_password(body.password):
        auth.record_failure(client)
        raise HTTPException(401, "Wrong password")
    auth.clear_failures(client)
    response = JSONResponse({"ok": True})
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(auth.COOKIE, auth.make_session(), max_age=auth.SESSION_TTL, httponly=True,
                        samesite="strict", secure=secure, path="/")
    return response


@app.post("/api/auth/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE, path="/")
    return response


def _iso(d):
    return d.isoformat() if d else None


class AddDestinationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    country: str = Field(min_length=1, max_length=80)
    airports: list[str] = Field(min_length=1)
    start_date: date
    end_date: date
    extended_stay: bool = True

    @field_validator("name", "country")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("airports")
    @classmethod
    def normalize_airports(cls, airports: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(a.strip().upper() for a in airports))
        if any(not re.fullmatch(r"[A-Z]{3}", airport) for airport in normalized):
            raise ValueError("airport codes must be three letters")
        return normalized

    @model_validator(mode="after")
    def dates_are_ordered(self):
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/overview")
def overview():
    searches = build_searches(cfg)
    plans = compute_plans(cfg, db.latest_quotes({s.key for s in searches}))
    last_run = db.runs.find_one({"kind": {"$ne": "flex"}}, sort=[("started_at", -1)])
    job = scheduler.get_job("refresh")
    best_dest = sorted(best_by(plans, "destination_id").values(), key=lambda p: p["rank_score"])
    dashboard_trips = list(best_dest)
    priced_destinations = {plan["destination_id"] for plan in best_dest}
    for destination in cfg.honeymoon:
        if destination.id in priced_destinations:
            continue
        placeholder = next((plan for plan in plans if plan["destination_id"] == destination.id), None)
        if placeholder:
            dashboard_trips.append(placeholder)
    dashboard_trips.sort(key=lambda p: (p["rank_score"] is None, p["rank_score"] or 0))
    return {
        "trip": {
            "home": cfg.home, "wedding_city": cfg.wedding_city, "gateways": cfg.gateways,
            "adults": cfg.party.adults, "currency": cfg.currency, "cabins": cfg.cabins.model_dump(),
            "dates": cfg.dates.model_dump(),
        },
        "destinations": [d.model_dump() for d in cfg.honeymoon],
        "plan_types": PLAN_TYPES,
        "best_by_destination": best_dest,
        "dashboard_trips": dashboard_trips,
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


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=100)


def _best_trips() -> list[dict]:
    plans = compute_plans(cfg, db.latest_quotes({s.key for s in build_searches(cfg)}))
    return sorted(best_by(plans, "destination_id").values(), key=lambda p: p["rank_score"])


@app.get("/api/assistant")
def assistant_status():
    return {"enabled": assistant.enabled(), "can_manage": assistant.tools_enabled()}


@app.post("/api/ask")
def ask_claude(request: AskRequest):
    try:
        return assistant.ask(request.question.strip(), assistant.trip_context(_best_trips()), request.session_id)
    except assistant.AssistantError as e:
        raise HTTPException(e.status, str(e))


@app.post("/api/run")
def run_now():
    return _start_refresh()


def _start_refresh() -> dict:
    if tracker.state["running"]:
        raise HTTPException(409, "A refresh is already running")
    threading.Thread(target=tracker.run_all, args=(cfg,), daemon=True).start()
    return {"started": True}


def _start_slicktrip_refresh(search_ids: list[str] | None = None) -> dict:
    if not assistant.enabled():
        raise HTTPException(503, "SlickTrip prices need the Claude bridge: log in (APP_PASSWORD_HASH) and set BRIDGE_URL and BRIDGE_TOKEN")
    if slicktrip.state["running"]:
        raise HTTPException(409, "A SlickTrip refresh is already running")
    unknown = set(search_ids or []) - {t.id for t in cfg.tracked_searches}
    if unknown:
        raise HTTPException(404, f"Unknown search: {', '.join(sorted(unknown))}")
    slicktrip.state["running"] = True   # set now so a second click can't race the thread start
    threading.Thread(target=slicktrip.refresh, args=(cfg, search_ids), daemon=True).start()
    return {"started": True}


@app.get("/api/slicktrip")
def slicktrip_prices():
    """Latest SlickTrip price per tracked search with the change vs. the previous price and the lowest seen.
    Every price is a total for all travelers on that search, in USD."""
    app_ids = {d["id"] for d in _gui_searches()}
    return {"enabled": assistant.enabled(), "running": dict(slicktrip.state), "currency": "USD",
            "price_scope": "total",
            "searches": [{**slicktrip.summarize(t), "editable": t.id in app_ids} for t in cfg.tracked_searches]}


@app.post("/api/slicktrip/refresh")
def slicktrip_refresh(search_id: str | None = None):
    return _start_slicktrip_refresh([search_id] if search_id else None)


def _known_destination(destination_id: str) -> None:
    if destination_id != "wedding-only" and not any(d.id == destination_id for d in cfg.honeymoon):
        raise HTTPException(404, f"Unknown destination '{destination_id}'")


@app.post("/api/flex/run")
def run_flex(destination_id: str):
    """On-demand flexible-date search: prices the flex grid for the destination's best few plans.
    Never part of the scheduled refresh."""
    _known_destination(destination_id)
    if not cfg.flex.enabled:
        raise HTTPException(400, "No flex dates configured (see the flex section of config.yaml)")
    if tracker.state["running"]:
        raise HTTPException(409, "A refresh is already running")
    if not tracker.flex_searches(cfg, destination_id):
        raise HTTPException(422, "Nothing to search: no priced plan for this destination yet "
                                 "(run Refresh prices first) or its flex dates were priced recently")
    threading.Thread(target=tracker.run_flex, args=(cfg, destination_id), daemon=True).start()
    return {"started": True}


@app.get("/api/alternatives")
def get_alternatives(destination_id: str):
    """Date matrix, top-5 alternatives and the near-dates flag, from the latest quotes over the flex dates."""
    _known_destination(destination_id)
    fd = flex_dates(cfg)
    keys = {s.key for s in build_searches(cfg, dates=fd)} | {s.key for s in build_searches(cfg)}
    plans = compute_plans(cfg, db.latest_quotes(keys), dates=fd)
    last = db.runs.find_one({"kind": "flex", "destination_id": destination_id}, sort=[("started_at", -1)])
    return {
        **alternatives(cfg, plans, destination_id),
        "flex": {
            "enabled": cfg.flex.enabled,
            "outbound": fd.outbound, "honeymoon_end": fd.honeymoon_end,
            "latest_home_arrival": cfg.flex.latest_home_arrival, "top_candidates": cfg.flex.top_candidates,
            "running": bool(tracker.state["running"] and tracker.state.get("mode") == "flex"),
            "last_run": last and {
                "started_at": _iso(last["started_at"]), "finished_at": _iso(last.get("finished_at")),
                "ok": last["ok"], "errors": last["errors"], "searches": last["searches"],
            },
        },
    }


@app.post("/api/destinations", status_code=201)
def add_destination(request: AddDestinationRequest):
    """Persist a destination added through the GUI and begin tracking its itinerary."""
    return _add_destination(request)


def _destination_id(name: str) -> str:
    destination_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not destination_id:
        raise HTTPException(422, "City name must contain letters or numbers")
    return destination_id


def _validate_honeymoon(destinations: list) -> None:
    try:
        Config.model_validate({**cfg.model_dump(), "honeymoon": destinations})
    except ValidationError as e:
        raise HTTPException(400, f"Destination is invalid: {e}") from e


def _write_gui_destinations(items: list[dict], migrated: bool | None = None) -> None:
    global cfg
    path = gui_destinations_path(CONFIG_PATH)
    if migrated is None:
        migrated = _read_store()[1]
    document = {"migrated": True, "destinations": items} if migrated else items
    temp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temp:
            temp_path = Path(temp.name)
            yaml.safe_dump(document, temp, sort_keys=False, allow_unicode=True)
        os.replace(temp_path, path)
    except OSError as e:
        raise HTTPException(500, f"Could not save destination: {e}") from e
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
    cfg = load_config(CONFIG_PATH)


def _read_store() -> tuple[list[dict], bool]:
    try:
        return read_store(gui_destinations_path(CONFIG_PATH))
    except ValueError as e:
        raise HTTPException(500, str(e)) from e


def _gui_destinations() -> list[dict]:
    return _read_store()[0]


def _gui_searches() -> list[dict]:
    try:
        return read_search_store(gui_searches_path(CONFIG_PATH))
    except ValueError as e:
        raise HTTPException(500, str(e)) from e


def _add_destination(request: AddDestinationRequest) -> dict:
    destination_id = _destination_id(request.name)
    if any(d.id == destination_id for d in cfg.honeymoon):
        raise HTTPException(409, f"{request.name} is already being tracked")
    destination = {
        "id": destination_id,
        "name": request.name,
        "country": request.country,
        "airports": request.airports,
        "start_dates": [request.start_date.isoformat()],
        "end_dates": [request.end_date.isoformat()],
        "extended_stay": request.extended_stay,
    }
    _validate_honeymoon([*cfg.honeymoon, destination])
    _write_gui_destinations([*_gui_destinations(), destination])
    return {"destination": destination, "searches": len(build_searches(cfg))}


NOT_MANAGED = ("'{id}' is not managed by the app yet. config.yaml destinations become editable after the user "
               "clicks \"Move built-in destinations into the app\" on the Add a city tab.")


def _update_destination(destination_id: str, changes: dict) -> dict:
    items = _gui_destinations()
    index = next((i for i, d in enumerate(items) if d["id"] == destination_id), None)
    if index is None:
        raise HTTPException(404, NOT_MANAGED.format(id=destination_id))
    current = items[index]
    cur_start, cur_end = (current.get("start_dates") or [None])[0], (current.get("end_dates") or [None])[0]
    start, end = changes.get("start_date", cur_start), changes.get("end_date", cur_end)
    try:   # validate with the global dates standing in when a destination has none of its own
        merged = AddDestinationRequest(
            name=changes.get("name", current["name"]), country=changes.get("country", current["country"]),
            airports=changes.get("airports", current["airports"]),
            start_date=start or cfg.dates.honeymoon_start[0], end_date=end or cfg.dates.honeymoon_end[0],
            extended_stay=changes.get("extended_stay", current.get("extended_stay", False)))
    except ValidationError as e:
        raise HTTPException(422, f"Update is invalid: {e}") from e
    updated = {**current, "name": merged.name, "country": merged.country, "airports": merged.airports,
               "extended_stay": merged.extended_stay}
    if start:
        updated["start_dates"] = [merged.start_date.isoformat()]
    if end:
        updated["end_dates"] = [merged.end_date.isoformat()]
    others = [d for d in cfg.honeymoon if d.id != destination_id]
    _validate_honeymoon([*others, updated])
    items[index] = updated
    _write_gui_destinations(items)
    return {"destination": updated, "searches": len(build_searches(cfg))}


def _remove_destination(destination_id: str) -> dict:
    items = _gui_destinations()
    if not any(d["id"] == destination_id for d in items):
        raise HTTPException(404, NOT_MANAGED.format(id=destination_id))
    _write_gui_destinations([d for d in items if d["id"] != destination_id])
    return {"removed": destination_id, "searches": len(build_searches(cfg))}


def _config_file_destinations() -> list[dict]:
    with open(CONFIG_PATH) as f:
        return list((yaml.safe_load(f) or {}).get("honeymoon") or [])


def _migrate_config() -> dict:
    """Copy config.yaml's honeymoon destinations (all fields) into the app store and make the store the source of truth.
    config.yaml is not modified; its honeymoon list is simply ignored afterwards. Idempotent."""
    items, migrated = _read_store()
    if migrated:
        return {"migrated": 0, "already": True, "destinations": len(cfg.honeymoon)}
    have = {d["id"] for d in items}
    moved = [d for d in _config_file_destinations() if d.get("id") not in have]
    _validate_honeymoon([*moved, *items])
    _write_gui_destinations([*moved, *items], migrated=True)
    actions.record("migrate_config", f"Moved {len(moved)} built-in destinations into the app", {"ids": [d["id"] for d in moved]})
    return {"migrated": len(moved), "already": False, "destinations": len(cfg.honeymoon)}


@app.get("/api/migration")
def migration_status():
    items, migrated = _read_store()
    have = {d["id"] for d in items}
    return {"migrated": migrated, "pending": 0 if migrated else len([d for d in _config_file_destinations() if d.get("id") not in have])}


@app.post("/api/migrate-config")
def migrate_config():
    return _migrate_config()


def _read_gui_destinations(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    if not isinstance(data, list):
        raise HTTPException(500, "GUI destinations file must contain a YAML list")
    return data


@app.post("/api/reload")
def reload_config():
    global cfg
    try:
        cfg = load_config()
    except Exception as e:
        raise HTTPException(400, f"config.yaml is invalid: {e}")
    schedule_job(run_now=False)
    return {"reloaded": True, "searches": len(build_searches(cfg))}


# --- Claude's tools (MCP) and the approval queue --------------------------------------------------

MAX_APP_DESTINATIONS = 30   # each destination adds Google Flights searches; cap what a model can pile on


def _as_tool_error(fn):
    def run(args: dict):
        try:
            return fn(args)
        except HTTPException as e:
            raise mcp.ToolError(str(e.detail)) from e
        except ValidationError as e:
            raise mcp.ToolError("Invalid arguments: " + "; ".join(
                f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())) from e
    return run


def _tool_list_destinations(_args: dict):
    app_ids = {d["id"] for d in _gui_destinations()}
    return [{"id": d.id, "name": d.name, "country": d.country, "airports": d.airports,
             "leave_wedding_city": (d.start_dates or [None])[0], "leave_destination": (d.end_dates or [None])[0],
             "extended_stay": d.extended_stay,
             "editable": d.id in app_ids,
             **({} if d.id in app_ids else {"note": "config.yaml destination; user must click 'Move built-in destinations into the app' to make it editable"})}
            for d in cfg.honeymoon]


def _tool_best_trips(_args: dict):
    return [{"destination": p["destination"], "plan": p["type"], "total_for_both": p["total"]}
            for p in _best_trips()]


def _tool_add(args: dict):
    if len(_gui_destinations()) >= MAX_APP_DESTINATIONS:
        raise mcp.ToolError(f"Limit of {MAX_APP_DESTINATIONS} app-added destinations reached; ask the user to remove some.")
    result = _add_destination(AddDestinationRequest(**args))
    actions.record("add_destination", f"Added {result['destination']['name']}", result["destination"])
    return result


def _tool_update(args: dict):
    destination_id = str(args.get("id", ""))
    result = _update_destination(destination_id, {k: v for k, v in args.items() if k != "id"})
    actions.record("update_destination", f"Updated {result['destination']['name']}", result["destination"])
    return result


def _tool_request_remove(args: dict):
    destination_id = str(args.get("id", ""))
    match = next((d for d in _gui_destinations() if d["id"] == destination_id), None)
    if match is None:
        raise mcp.ToolError(NOT_MANAGED.format(id=destination_id))
    if not actions.pending_for("remove_destination", destination_id):
        actions.record("remove_destination", f"Remove {match['name']}", {"id": destination_id, "name": match["name"]},
                       status="pending")
    return (f"Removal of {match['name']} is waiting for the user to approve it in the Ask Claude tab. "
            "It has NOT been removed. Tell the user to approve it there.")


def _tool_refresh(_args: dict):
    return _start_refresh()


_DEST_PROPS = {
    "name": {"type": "string", "description": "City name, e.g. Seoul"},
    "country": {"type": "string"},
    "airports": {"type": "array", "items": {"type": "string"}, "description": "3-letter IATA codes, e.g. [\"ICN\"]"},
    "start_date": {"type": "string", "description": "YYYY-MM-DD: date leaving the wedding city (Da Nang)"},
    "end_date": {"type": "string", "description": "YYYY-MM-DD: date leaving the destination to fly home; after start_date"},
    "extended_stay": {"type": "boolean", "description": "true = no return hop to Da Nang"},
}
TOOLS = {t.name: t for t in [
    mcp.Tool("list_destinations", "List tracked honeymoon destinations and whether each can be edited.",
             {"type": "object", "properties": {}}, _as_tool_error(_tool_list_destinations), read_only=True),
    mcp.Tool("get_best_trips", "Cheapest complete trip per destination with current prices (totals for both travelers).",
             {"type": "object", "properties": {}}, _as_tool_error(_tool_best_trips), read_only=True),
    mcp.Tool("add_destination", "Start tracking a new destination/flight plan.",
             {"type": "object", "properties": _DEST_PROPS,
              "required": ["name", "country", "airports", "start_date", "end_date"]}, _as_tool_error(_tool_add)),
    mcp.Tool("update_destination", "Change an app-added destination (dates, airports, name, extended_stay). "
             "Cannot edit config.yaml destinations.",
             {"type": "object", "properties": {"id": {"type": "string"}, **_DEST_PROPS}, "required": ["id"]},
             _as_tool_error(_tool_update)),
    mcp.Tool("request_remove_destination", "Ask the user to approve removing an app-added destination. "
             "Nothing is removed until the user approves in the UI.",
             {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
             _as_tool_error(_tool_request_remove)),
    mcp.Tool("refresh_prices", "Start a price refresh now (takes several minutes).",
             {"type": "object", "properties": {}}, _as_tool_error(_tool_refresh)),
]}


@app.post("/mcp")
def mcp_endpoint(message: dict = Body(...), authorization: str = Header(default="")):
    """Claude's tool endpoint. Needs the per-question token the app hands the bridge; no UI route is reachable this way."""
    if not assistant.token_valid(authorization.removeprefix("Bearer ").strip()):
        raise HTTPException(401, "Invalid or expired token")
    reply = mcp.handle(message, TOOLS)
    if reply is None:
        return Response(status_code=202)
    return reply


@app.get("/api/actions")
def list_actions():
    return actions.list_recent()


@app.post("/api/actions/{action_id}/approve")
def approve_action(action_id: str):
    action = actions.claim(action_id, "approved")
    if action is None:
        raise HTTPException(404, "No pending action with that id (already handled?)")
    try:
        if action["kind"] == "remove_destination":
            _remove_destination(action["payload"]["id"])
        else:
            raise HTTPException(400, f"Cannot run action kind {action['kind']}")
    except HTTPException as e:
        actions.finish(action_id, "failed", str(e.detail))
        raise
    actions.finish(action_id, "applied")
    return {"approved": action_id}


@app.post("/api/actions/{action_id}/reject")
def reject_action(action_id: str):
    if actions.claim(action_id, "rejected") is None:
        raise HTTPException(404, "No pending action with that id (already handled?)")
    return {"rejected": action_id}
