import pytest

from app import db, tracker
from app.providers import google, serpapi
from app.searches import build_searches
from tests.conftest import at, make_cfg


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    sleeps = []
    monkeypatch.setattr(tracker.time, "sleep", sleeps.append)
    return sleeps


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(tracker, "notify", lambda cfg, title, message, url="": sent.append((title, message, url)))
    return sent


def fake_google(monkeypatch, price_for):
    """Patch the fast-flights provider; `price_for(search)` returns a price, None, or raises."""
    calls = []

    def search(s, adults, currency):
        calls.append(s.key)
        price = price_for(s)
        return {"min_price": price, "price_level": "typical", "offers": [{"price": price}] if price else []}

    monkeypatch.setattr(google, "search", search)
    return calls


def test_run_all_stores_quotes_runs_and_plan_history(monkeypatch, cfg, alerts, no_waiting):
    calls = fake_google(monkeypatch, lambda s: 1000)
    run_id = tracker.run_all(cfg)

    searches = build_searches(cfg)
    assert sorted(calls) == sorted(s.key for s in searches)
    assert db.quotes.count_documents({"run_id": run_id}) == len(searches)
    q = db.quotes.find_one({"key": searches[0].key})
    assert q["min_price"] == 1000 and q["error"] is None
    assert q["booking_url"].startswith("https://www.google.com/travel/flights?tfs=")
    assert q["provider"] == "google" and q["kind"] == searches[0].kind

    run = db.runs.find_one({"_id": run_id})
    assert run["searches"] == len(searches) and run["ok"] == len(searches) and run["errors"] == 0
    assert run["finished_at"] >= run["started_at"]

    history = {(r["destination_id"], r["type"]): r["total"] for r in db.plan_history.find({"run_id": run_id})}
    assert history == {("wedding-only", "wedding"): 1000, ("tokyo", "rt_side"): 2000, ("tokyo", "open_jaw"): 3000}

    assert alerts == []                        # first run: nothing to compare against, no target
    assert tracker.state["running"] is False
    assert no_waiting == [0] * (len(searches) - 1)   # polite delay between Google requests, not after the last


def test_failed_search_is_recorded_and_run_continues(monkeypatch, cfg, alerts):
    def price_for(s):
        if s.kind == "longhaul_home":
            raise RuntimeError("No flights found")
        if s.kind == "honeymoon_back":
            return None
        return 1000

    fake_google(monkeypatch, price_for)
    run_id = tracker.run_all(cfg)

    home = db.quotes.find_one({"kind": "longhaul_home"})
    assert home["min_price"] is None and home["offers"] == []
    assert home["error"] == "No flights found on Google for this route/date/cabin"
    assert db.quotes.find_one({"kind": "honeymoon_back"})["error"] == "no prices returned"

    run = db.runs.find_one({"_id": run_id})
    assert (run["ok"], run["errors"]) == (4, 2)
    assert {r["type"] for r in db.plan_history.find()} == {"wedding", "rt_side"}   # open jaw is unpriced


def test_google_retry_after_transient_error(monkeypatch, cfg, no_waiting):
    attempts = []

    def flaky(s, adults, currency):
        attempts.append(s.key)
        if len(attempts) == 1:
            raise ConnectionError("reset")
        return {"min_price": 1}

    monkeypatch.setattr(google, "search", flaky)
    s = build_searches(cfg)[0]
    assert tracker._google_with_retry(s, cfg) == {"min_price": 1}
    assert len(attempts) == 2 and no_waiting == [30]


def test_google_no_flights_is_not_retried(monkeypatch, cfg, no_waiting):
    attempts = []

    def none_found(s, adults, currency):
        attempts.append(s.key)
        raise Exception("No flights found:\n<html>…</html>")

    monkeypatch.setattr(google, "search", none_found)
    with pytest.raises(RuntimeError, match="No flights found on Google"):
        tracker._google_with_retry(build_searches(cfg)[0], cfg)
    assert len(attempts) == 1 and no_waiting == []


def test_serpapi_budget_is_enforced(monkeypatch, alerts):
    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    cfg = make_cfg(providers={"serpapi_monthly_budget": 1})
    fake_google(monkeypatch, lambda s: 1000)
    monkeypatch.setattr(serpapi, "search", lambda s, adults, currency, key: pytest.fail("budget is used up"))
    db.quotes.insert_one({"provider": "serpapi", "fetched_at": tracker.now(), "min_price": 5})

    tracker.run_all(cfg)
    mc = db.quotes.find_one({"trip": "multi-city", "run_id": {"$exists": True}})
    assert mc["error"] == "SerpApi monthly budget used up"


def test_serpapi_used_when_budget_left(monkeypatch, alerts):
    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    cfg = make_cfg()
    fake_google(monkeypatch, lambda s: 1000)
    used = []

    def serp(s, adults, currency, key):
        used.append((s.key, adults, currency, key))
        return {"min_price": 7000, "price_level": None, "offers": []}

    monkeypatch.setattr(serpapi, "search", serp)
    tracker.run_all(cfg)
    assert used == [("multi-city|business|DFW-DAD@2027-03-05,HND-DFW@2027-03-19", 2, "USD", "test-key")]
    assert db.plan_history.find_one({"type": "multicity"})["total"] == 8000   # 7000 ticket + 1000 DAD → HND


def test_serpapi_used_this_month_counts_only_current_month(monkeypatch):
    monkeypatch.setattr(tracker, "now", lambda: at(20, 12))
    db.quotes.insert_many([
        {"provider": "serpapi", "fetched_at": at(1)},
        {"provider": "serpapi", "fetched_at": at(19)},
        {"provider": "serpapi", "fetched_at": at(1).replace(month=9, day=30)},
        {"provider": "google", "fetched_at": at(19)},
    ])
    assert tracker.serpapi_used_this_month() == 2


def test_run_all_skips_when_already_running(monkeypatch, cfg):
    monkeypatch.setattr(google, "search", lambda *a: pytest.fail("should not run"))
    assert tracker._lock.acquire(blocking=False)
    try:
        assert tracker.run_all(cfg) is None
    finally:
        tracker._lock.release()
    assert db.runs.count_documents({}) == 0


# --- new-low alerts ------------------------------------------------------------

def seed_history(dest_id, total):
    db.plan_history.insert_one({"run_id": "old", "at": at(1), "destination_id": dest_id, "type": "rt_side",
                                "total": total, "plan_id": "x"})


def test_alert_on_drop_beyond_min_pct(monkeypatch, cfg, alerts):
    seed_history("tokyo", 2100)   # new best is 2000: a 4.8% drop, above the default 3%
    seed_history("wedding-only", 1010)   # 1% drop: below threshold
    fake_google(monkeypatch, lambda s: 1000)
    tracker.run_all(cfg)
    assert len(alerts) == 1
    title, message, url = alerts[0]
    assert title == "✈️ New low: Tokyo, Japan"
    assert message == "$2,000 for 2 (was $2,100) — Round trip + side trip via DAD, leaving 2027-03-05"
    assert url.startswith("https://www.google.com/travel/flights")


def test_no_alert_when_not_a_new_low(monkeypatch, cfg, alerts):
    seed_history("tokyo", 1500)
    seed_history("wedding-only", 1000)
    fake_google(monkeypatch, lambda s: 1000)
    tracker.run_all(cfg)
    assert alerts == []


def test_alert_when_under_target_total(monkeypatch, alerts):
    cfg = make_cfg(alerts={"target_total": 1500})
    fake_google(monkeypatch, lambda s: 1000)
    tracker.run_all(cfg)
    # Only the wedding-only baseline (1000) is under target; Tokyo's best is 2000.
    assert [a[0] for a in alerts] == ["✈️ New low: Wedding only"]
    assert "(was" not in alerts[0][1]
