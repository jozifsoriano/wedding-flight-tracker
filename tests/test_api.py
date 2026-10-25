import pytest
import yaml
from fastapi.testclient import TestClient

from app import db, main, tracker
from app.config import load_config
from app.searches import build_searches
from tests.conftest import at, make_cfg


@pytest.fixture
def client(monkeypatch, cfg):
    """TestClient without the lifespan, so the scheduler never starts and no refresh runs."""
    monkeypatch.setattr(main, "cfg", cfg)
    monkeypatch.setattr(tracker, "state", {"running": False, "progress": 0, "total": 0, "current": None})
    return TestClient(main.app)


def price_everything(cfg, price=1000, fetched_at=None):
    for s in build_searches(cfg):
        db.quotes.insert_one({**s.to_doc(), "min_price": price, "fetched_at": fetched_at or at(1), "offers": []})


def test_index_serves_dashboard(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_overview_with_no_data(client, cfg):
    body = client.get("/api/overview").json()
    assert body["trip"]["home"] == "DFW" and body["trip"]["adults"] == 2
    assert [d["id"] for d in body["destinations"]] == ["tokyo"]
    assert body["best_by_destination"] == []
    assert all(p["total"] is None for p in body["plans"])
    assert body["status"]["search_count"] == len(build_searches(cfg))
    assert body["status"]["last_run"] is None
    assert body["status"]["serpapi_enabled"] is False


def test_overview_with_prices_and_run(client, cfg):
    price_everything(cfg)
    db.runs.insert_one({"_id": "r1", "started_at": at(1), "finished_at": at(1, 1), "searches": 6, "ok": 6, "errors": 0})
    body = client.get("/api/overview").json()
    assert [(p["destination_id"], p["total"]) for p in body["best_by_destination"]] == [
        ("wedding-only", 1000), ("tokyo", 2000)]
    assert len(body["best_by_destination_type"]) == 3
    assert body["status"]["last_run"] == {"started_at": "2026-10-01T00:00:00+00:00",
                                          "finished_at": "2026-10-01T01:00:00+00:00",
                                          "ok": 6, "errors": 0, "searches": 6}


def test_history_groups_by_plan_type(client):
    db.plan_history.insert_many([
        {"destination_id": "tokyo", "type": "rt_side", "total": 9000, "at": at(2), "run_id": "b"},
        {"destination_id": "tokyo", "type": "rt_side", "total": 9500, "at": at(1), "run_id": "a"},
        {"destination_id": "tokyo", "type": "open_jaw", "total": 8000, "at": at(1), "run_id": "a"},
        {"destination_id": "osaka", "type": "rt_side", "total": 1, "at": at(1), "run_id": "a"},
    ])
    assert client.get("/api/history", params={"destination_id": "tokyo"}).json() == {
        "rt_side": [{"at": "2026-10-01T00:00:00+00:00", "total": 9500}, {"at": "2026-10-02T00:00:00+00:00", "total": 9000}],
        "open_jaw": [{"at": "2026-10-01T00:00:00+00:00", "total": 8000}],
    }


def test_sparklines_cheapest_per_destination_per_run(client):
    db.plan_history.insert_many([
        {"destination_id": "tokyo", "type": "rt_side", "total": 9000, "at": at(1), "run_id": "a"},
        {"destination_id": "tokyo", "type": "open_jaw", "total": 8000, "at": at(1), "run_id": "a"},
        {"destination_id": "tokyo", "type": "open_jaw", "total": 8500, "at": at(2), "run_id": "b"},
    ])
    assert client.get("/api/sparklines").json() == {"tokyo": [
        {"at": "2026-10-01T00:00:00+00:00", "total": 8000},
        {"at": "2026-10-02T00:00:00+00:00", "total": 8500},
    ]}


def test_searches_include_last_quote(client, cfg):
    price_everything(cfg, 500, at(1))
    first = build_searches(cfg)[0]
    db.quotes.insert_one({**first.to_doc(), "min_price": None, "error": "blocked", "fetched_at": at(2), "offers": []})
    rows = {r["key"]: r for r in client.get("/api/searches").json()}
    assert set(rows) == {s.key for s in build_searches(cfg)}
    assert rows[first.key]["last"]["error"] == "blocked"                 # latest attempt, even if it failed
    assert rows[first.key]["last"]["fetched_at"] == "2026-10-02T00:00:00+00:00"


def test_search_history_skips_failed_quotes(client):
    db.quotes.insert_many([
        {"key": "k", "min_price": 700, "fetched_at": at(2)},
        {"key": "k", "min_price": None, "fetched_at": at(3)},
        {"key": "k", "min_price": 800, "fetched_at": at(1)},
        {"key": "other", "min_price": 1, "fetched_at": at(1)},
    ])
    assert client.get("/api/search_history", params={"key": "k"}).json() == [
        {"at": "2026-10-01T00:00:00+00:00", "total": 800},
        {"at": "2026-10-02T00:00:00+00:00", "total": 700},
    ]


def test_run_now_starts_background_refresh(client, monkeypatch, cfg):
    started = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            started.append((self.target, self.args))

    monkeypatch.setattr(main.threading, "Thread", FakeThread)
    assert client.post("/api/run").json() == {"started": True}
    assert started == [(tracker.run_all, (cfg,))]


def test_run_now_conflict_when_running(client, monkeypatch):
    tracker.state["running"] = True
    monkeypatch.setattr(main.threading, "Thread", lambda **kw: pytest.fail("should not start"))
    r = client.post("/api/run")
    assert r.status_code == 409


def test_reload_config(client, monkeypatch):
    new = make_cfg(gateways=["DAD", "SGN"])
    scheduled = []
    monkeypatch.setattr(main, "load_config", lambda: new)
    monkeypatch.setattr(main, "schedule_job", lambda run_now: scheduled.append(run_now))
    assert client.post("/api/reload").json() == {"reloaded": True, "searches": len(build_searches(new))}
    assert main.cfg is new
    assert scheduled == [False]


def test_reload_invalid_config_keeps_old_one(client, monkeypatch, cfg, tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text("home: DFW\n")   # no dates
    monkeypatch.setattr(main, "load_config", lambda: load_config(bad))
    monkeypatch.setattr(main, "schedule_job", lambda run_now: pytest.fail("should not reschedule"))
    r = client.post("/api/reload")
    assert r.status_code == 400
    assert r.json()["detail"].startswith("config.yaml is invalid")
    assert main.cfg is cfg


def test_add_destination_persists_itinerary_dates_and_reloads_config(client, monkeypatch, cfg, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg.model_dump(), sort_keys=False))
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    response = client.post("/api/destinations", json={
        "name": "Seoul", "country": "South Korea", "airports": ["icn"],
        "start_date": "2027-03-15", "end_date": "2027-03-19", "extended_stay": True,
    })

    assert response.status_code == 201
    assert response.json()["destination"]["id"] == "seoul"
    assert response.json()["searches"] > 0
    assert main.cfg.honeymoon[-1].model_dump()["start_dates"] == ["2027-03-15"]
    assert main.cfg.honeymoon[-1].extended_stay is True
    assert (tmp_path / "user-data" / "user_destinations.yaml").exists()


def test_add_destination_rejects_invalid_dates_and_duplicates(client, monkeypatch, cfg, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg.model_dump(), sort_keys=False))
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    payload = {
        "name": "Tokyo", "country": "Japan", "airports": ["HND"],
        "start_date": "2027-03-19", "end_date": "2027-03-15",
    }
    assert client.post("/api/destinations", json=payload).status_code == 422
    payload.update(name="Tokyo", start_date="2027-03-15", end_date="2027-03-19")
    assert client.post("/api/destinations", json=payload).status_code == 409
