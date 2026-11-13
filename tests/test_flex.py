import pytest
from fastapi.testclient import TestClient

from app import db, main, tracker
from app.config import Flex, load_config
from app.plans import alternatives, compute_plans
from app.providers import google
from app.searches import build_searches, flex_dates
from tests.conftest import ROOT, at, make_cfg, quote

FLEX = {"outbound": ["2027-03-05", "2027-03-06", "2027-03-07"],
        "honeymoon_end": ["2027-03-19", "2027-03-20", "2027-03-21"],
        "latest_home_arrival": "2027-03-21"}
TOKYO = {"id": "tokyo", "name": "Tokyo", "country": "Japan", "airports": ["HND"]}
OSAKA = {"id": "osaka", "name": "Osaka", "country": "Japan", "airports": ["KIX"]}


def flex_cfg(**overrides):
    return make_cfg(flex=FLEX, **overrides)


def price_all(cfg, price_for, dates=None, fetched_at=None):
    for s in build_searches(cfg, dates=dates):
        db.quotes.insert_one({**s.to_doc(), **quote(price_for(s), fetched_at or at(1))})


def latest_for(cfg, price_for, dates):
    return {s.key: quote(price_for(s)) for s in build_searches(cfg, dates=dates)}


def end_date(s):
    return s.legs[-1].date if s.trip == "round-trip" else s.legs[0].date


# --- date filtering ------------------------------------------------------------------------------

def test_flex_dates_union_core_and_flex():
    cfg = make_cfg(flex={"outbound": ["2027-03-07"], "honeymoon_end": ["2027-03-20"]})
    fd = flex_dates(cfg)
    assert fd.outbound == ["2027-03-05", "2027-03-07"]
    assert fd.honeymoon_end == ["2027-03-19", "2027-03-20"]
    assert fd.honeymoon_start == cfg.dates.honeymoon_start


def test_latest_home_arrival_caps_every_plan():
    cfg = flex_cfg()
    fd = flex_dates(cfg)
    plans = compute_plans(cfg, {}, dates=fd)
    assert plans
    for pl in plans:
        home = pl["dates"].get("home") or pl["dates"]["honeymoon_end"]
        assert home <= "2027-03-21", pl["id"]
    # 3/21 honeymoon_end is open-jaw only: round-trip plans would get home 3/22
    assert {pl["type"] for pl in plans if pl["dates"].get("honeymoon_end") == "2027-03-21"} == {"open_jaw"}
    assert {pl["type"] for pl in plans if pl["dates"].get("home") == "2027-03-21"} == {"wedding", "rt_side"}
    assert not [pl for pl in plans if pl["type"] == "wedding" and pl["dates"]["home"] == "2027-03-22"]


def test_latest_home_arrival_caps_searches():
    cfg = flex_cfg()
    searches = build_searches(cfg, dates=flex_dates(cfg))
    for s in searches:
        if s.kind in ("longhaul_rt", "honeymoon_rt", "honeymoon_back"):
            assert end_date(s) <= "2027-03-21", s.key
    assert any(s.kind == "longhaul_home" and s.legs[0].date == "2027-03-21" for s in searches)
    assert not any(s.kind == "longhaul_rt" and s.legs[1].date == "2027-03-22" for s in searches)
    assert not any(s.kind == "honeymoon_rt" and s.legs[1].date == "2027-03-21" for s in searches)


def test_cap_unset_allows_any_date():
    cfg = make_cfg(flex={"honeymoon_end": ["2027-03-25"]})
    assert any(s.kind == "longhaul_home" and s.legs[0].date == "2027-03-25"
               for s in build_searches(cfg, dates=flex_dates(cfg)))


def test_flex_plans_only_reference_flex_searches():
    cfg = flex_cfg(honeymoon=[TOKYO, {"id": "pqc", "name": "Phu Quoc", "country": "Vietnam", "airports": ["PQC"],
                                      "fly_home_from": "SGN"}], gateways=["DAD", "SGN"])
    fd = flex_dates(cfg)
    searched = {s.key for s in build_searches(cfg, dates=fd)}
    for pl in compute_plans(cfg, {}, dates=fd):
        assert {c["key"] for c in pl["components"]} <= searched, pl["id"]


# --- the scheduled job stays the same ------------------------------------------------------------

def test_scheduled_searches_ignore_flex_config():
    with_flex, without = flex_cfg(), make_cfg()
    assert [s.key for s in build_searches(with_flex)] == [s.key for s in build_searches(without)]


def test_shipped_config_scheduled_search_count_unchanged_by_flex():
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.flex.enabled and cfg.flex.latest_home_arrival == "2027-03-21"
    assert [s.key for s in build_searches(cfg)] == [s.key for s in build_searches(cfg.model_copy(update={"flex": Flex()}))]
    assert len(build_searches(cfg, dates=flex_dates(cfg))) > len(build_searches(cfg))   # grid is only on demand


# --- flex grid for the best candidates only --------------------------------------------------------

def test_flex_searches_cover_only_top_candidates_of_one_destination():
    cfg = make_cfg(flex={**FLEX, "top_candidates": 1}, honeymoon=[TOKYO, OSAKA])
    price_all(cfg, lambda s: 1000)
    searches = tracker.flex_searches(cfg, "tokyo")
    keys = {s.key for s in searches}
    assert searches and len(keys) == len(searches)
    assert not [s for s in searches if "KIX" in s.key]                                    # other destination
    # one candidate = the cheapest shape (round trip + side trip): no open-jaw one-way home legs
    assert {s.kind for s in searches} <= {"longhaul_rt", "honeymoon_rt", "honeymoon_ow", "honeymoon_back"}
    assert {s.legs[0].date for s in searches if s.kind == "longhaul_rt"} == set(FLEX["outbound"])
    assert {s.legs[1].date for s in searches if s.kind == "longhaul_rt"} == {"2027-03-20", "2027-03-21"}
    assert len(searches) < len(build_searches(cfg, dates=flex_dates(cfg)))


def test_flex_searches_include_more_shapes_with_more_candidates():
    cfg = make_cfg(flex={**FLEX, "top_candidates": 3}, honeymoon=[TOKYO])
    price_all(cfg, lambda s: 1000)
    kinds = {s.kind for s in tracker.flex_searches(cfg, "tokyo")}
    assert {"longhaul_home", "longhaul_out"} <= kinds   # the open-jaw shape is now a candidate too


def test_flex_searches_skip_recently_priced_and_need_priced_candidates():
    cfg = make_cfg(flex=FLEX, honeymoon=[TOKYO])
    assert tracker.flex_searches(cfg, "tokyo") == []                      # nothing priced: no candidates
    price_all(cfg, lambda s: 1000)
    stale = tracker.flex_searches(cfg, "tokyo")
    core_keys = {s.key for s in build_searches(cfg)}
    price_all(cfg, lambda s: 1000, fetched_at=tracker.now())              # core dates were just refreshed
    fresh = tracker.flex_searches(cfg, "tokyo")
    assert len(fresh) < len(stale) and not ({s.key for s in fresh} & core_keys)


@pytest.fixture
def no_waiting(monkeypatch):
    sleeps = []
    monkeypatch.setattr(tracker.time, "sleep", sleeps.append)
    return sleeps


def fake_google(monkeypatch, price=1200):
    calls = []

    def search(s, adults, currency):
        calls.append(s.key)
        return {"min_price": price, "price_level": "typical", "offers": [{"price": price}]}

    monkeypatch.setattr(google, "search", search)
    return calls


def test_run_flex_stores_quotes_like_a_normal_run(monkeypatch, no_waiting):
    cfg = make_cfg(flex=FLEX, honeymoon=[TOKYO])
    price_all(cfg, lambda s: 1000)
    expected = tracker.flex_searches(cfg, "tokyo")
    calls = fake_google(monkeypatch)

    run_id = tracker.run_flex(cfg, "tokyo")

    assert sorted(calls) == sorted(s.key for s in expected)
    assert db.quotes.count_documents({"run_id": run_id}) == len(expected)
    q = db.quotes.find_one({"run_id": run_id})
    assert q["min_price"] == 1200 and q["booking_url"].startswith("https://www.google.com/travel/flights")
    run = db.runs.find_one({"_id": run_id})
    assert (run["kind"], run["destination_id"], run["ok"], run["errors"]) == ("flex", "tokyo", len(expected), 0)
    assert no_waiting == [0] * (len(expected) - 1)                        # request_delay_seconds respected
    assert db.plan_history.count_documents({}) == 0                       # no history rows or alerts from flex
    assert tracker.state["running"] is False and tracker.state["mode"] is None


def test_run_flex_respects_serpapi_budget(monkeypatch, no_waiting):
    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    cfg = make_cfg(flex=FLEX, honeymoon=[TOKYO], providers={"serpapi_monthly_budget": 0})
    price_all(cfg, lambda s: 1000)
    fake_google(monkeypatch)
    searches = tracker.flex_searches(cfg, "tokyo")
    assert any(s.provider == "serpapi" for s in searches)

    run_id = tracker.run_flex(cfg, "tokyo")

    serp = list(db.quotes.find({"run_id": run_id, "provider": "serpapi"}))
    assert serp and all(q["min_price"] is None and "budget" in q["error"] for q in serp)


def test_run_flex_refuses_when_busy(cfg):
    assert tracker._lock.acquire(blocking=False)
    try:
        assert tracker.run_flex(cfg, "tokyo") is None
    finally:
        tracker._lock.release()


# --- alternatives ----------------------------------------------------------------------------------

def scenario_prices(s):
    """Open jaw is cheapest on the far dates 3/07 + 3/21; one day off the core dates (3/06 + 3/20) is cheaper too."""
    out = {"2027-03-05": 5000, "2027-03-06": 4800, "2027-03-07": 4000}
    home = {"2027-03-19": 4000, "2027-03-20": 4000, "2027-03-21": 3500}
    if s.kind == "longhaul_out":
        return out[s.legs[0].date]
    if s.kind == "longhaul_home":
        return home[s.legs[0].date]
    if s.kind == "longhaul_rt":
        return 9000
    return 100


def alt_for(cfg, price_for=scenario_prices, drop=None):
    fd = flex_dates(cfg)
    latest = {k: q for k, q in latest_for(cfg, price_for, fd).items() if not (drop and drop in k)}
    return alternatives(cfg, compute_plans(cfg, latest, dates=fd), "tokyo")


def test_alternatives_matrix_marks_best_core_and_cap():
    cfg = flex_cfg()
    alt = alt_for(cfg)
    m = alt["matrix"]
    assert m["outbound"] == FLEX["outbound"] and m["ends"] == FLEX["honeymoon_end"]
    assert m["core_outbound"] == ["2027-03-05"] and m["core_ends"] == ["2027-03-19"]
    cells = {(r["outbound"], e): c for r in m["rows"] for e, c in zip(m["ends"], r["cells"])}
    assert cells["2027-03-07", "2027-03-21"]["total"] == 4000 + 3500 + 100
    assert cells["2027-03-07", "2027-03-21"]["best"] is True and cells["2027-03-07", "2027-03-21"]["type"] == "open_jaw"
    assert [k for k, c in cells.items() if c["best"]] == [("2027-03-07", "2027-03-21")]
    assert [k for k, c in cells.items() if c["core"]] == [("2027-03-05", "2027-03-19")]
    assert all(c["home"] <= "2027-03-21" for c in cells.values())
    assert {r["outbound"] for r in m["rows"] if r["core"]} == {"2027-03-05"}


def test_alternatives_matrix_is_null_where_unpriced():
    alt = alt_for(flex_cfg(), drop="03-07")
    row = {r["outbound"]: r["cells"] for r in alt["matrix"]["rows"]}
    assert row["2027-03-07"] == [None, None, None]
    assert all(c is not None for c in row["2027-03-05"])
    assert alt["priced"] < alt["plans"]


def test_alternatives_top_five_with_deltas():
    alt = alt_for(flex_cfg())
    top = alt["top"]
    assert len(top) == 5
    assert top[0]["total"] == 7600 and top[0]["delta"] == 0
    assert [t["total"] for t in top] == sorted(t["total"] for t in top)
    assert all(t["delta"] == t["total"] - 7600 for t in top)
    assert all("hop_label" in t and "hop_note" in t for t in top)


def test_near_dates_flag_when_cheaper_within_a_day_of_core():
    nd = alt_for(flex_cfg())["near_dates"]
    assert nd["flag"] is True
    assert nd["core_best"]["total"] == 9100 and nd["core_best"]["core"] is True
    assert nd["near_best"]["total"] == 4800 + 4000 + 100       # 3/06 out + 3/20 home, not the cheaper 3/07 + 3/21
    assert nd["near_best"]["dates"]["outbound"] == "2027-03-06"
    assert nd["savings"] == 200


def test_near_dates_flag_off_when_core_is_cheapest_nearby():
    def prices(s):
        base = scenario_prices(s)
        return base - 1500 if s.kind in ("longhaul_out", "longhaul_home") and s.legs[0].date in (
            "2027-03-05", "2027-03-19") else base
    nd = alt_for(flex_cfg(), prices)["near_dates"]
    assert nd["flag"] is False and nd["savings"] == 0
    assert nd["near_best"] is not None and nd["core_best"]["total"] < nd["near_best"]["total"]


def test_near_dates_flag_off_without_core_prices():
    nd = alt_for(flex_cfg(), drop="03-05")["near_dates"]
    assert nd["flag"] is False and nd["core_best"] is None


def test_alternatives_rank_by_hop_penalty():
    cfg = flex_cfg(gateways=["DAD", "SGN"])

    def prices(s):   # SGN gateway is $50 cheaper in business: not enough to beat the $100 Vietnam hop penalty
        base = scenario_prices(s)
        return base - 50 if s.kind == "longhaul_out" and s.legs[0].dest == "SGN" else base
    top = alt_for(cfg, prices)["top"]
    assert top[0]["gateway"] == "DAD" and top[0]["hop_label"] == "direct"
    assert any(t["gateway"] == "SGN" and t["hop_label"] == "hop" and t["hop_note"] for t in alt_for(cfg, prices)["top"][1:])


# --- endpoints -------------------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    cfg = make_cfg(flex=FLEX, honeymoon=[TOKYO])
    monkeypatch.setattr(main, "cfg", cfg)
    monkeypatch.setattr(tracker, "state", {"running": False, "progress": 0, "total": 0, "current": None})
    return TestClient(main.app)


def test_alternatives_endpoint(client):
    price_all(main.cfg, scenario_prices, dates=flex_dates(main.cfg))
    body = client.get("/api/alternatives", params={"destination_id": "tokyo"}).json()
    assert body["top"][0]["total"] == 7600
    assert body["near_dates"]["flag"] is True
    assert body["matrix"]["ends"] == FLEX["honeymoon_end"]
    assert body["flex"] == {"enabled": True, "outbound": FLEX["outbound"], "honeymoon_end": FLEX["honeymoon_end"],
                            "latest_home_arrival": "2027-03-21", "top_candidates": 3, "running": False,
                            "last_run": None}


def test_alternatives_endpoint_empty_and_unknown(client):
    body = client.get("/api/alternatives", params={"destination_id": "tokyo"}).json()
    assert body["top"] == [] and body["near_dates"]["flag"] is False
    assert all(c is None for r in body["matrix"]["rows"] for c in r["cells"])
    assert client.get("/api/alternatives", params={"destination_id": "mars"}).status_code == 404
    assert client.get("/api/alternatives").status_code == 422
    assert client.get("/api/alternatives", params={"destination_id": "wedding-only"}).status_code == 200


def test_alternatives_reports_last_flex_run(client):
    db.runs.insert_one({"_id": "f1", "kind": "flex", "destination_id": "tokyo", "started_at": at(2),
                        "finished_at": at(2, 1), "searches": 10, "ok": 9, "errors": 1})
    last = client.get("/api/alternatives", params={"destination_id": "tokyo"}).json()["flex"]["last_run"]
    assert last == {"started_at": "2026-10-02T00:00:00+00:00", "finished_at": "2026-10-02T01:00:00+00:00",
                    "ok": 9, "errors": 1, "searches": 10}
    assert client.get("/api/overview").json()["status"]["last_run"] is None   # flex runs don't masquerade as refreshes


def test_flex_run_endpoint_starts_background_search(client, monkeypatch):
    price_all(main.cfg, lambda s: 1000)
    started = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            started.append((self.target, self.args))

    monkeypatch.setattr(main.threading, "Thread", FakeThread)
    assert client.post("/api/flex/run", params={"destination_id": "tokyo"}).json() == {"started": True}
    assert started == [(tracker.run_flex, (main.cfg, "tokyo"))]


def test_flex_run_endpoint_conflict_and_validation(client, monkeypatch):
    price_all(main.cfg, lambda s: 1000)
    monkeypatch.setattr(main.threading, "Thread", lambda **kw: pytest.fail("should not start"))
    assert client.post("/api/flex/run").status_code == 422
    assert client.post("/api/flex/run", params={"destination_id": "mars"}).status_code == 404
    tracker.state["running"] = True
    assert client.post("/api/flex/run", params={"destination_id": "tokyo"}).status_code == 409


def test_flex_run_endpoint_needs_config_and_priced_plans(client, monkeypatch):
    monkeypatch.setattr(main.threading, "Thread", lambda **kw: pytest.fail("should not start"))
    assert client.post("/api/flex/run", params={"destination_id": "tokyo"}).status_code == 422   # nothing priced
    monkeypatch.setattr(main, "cfg", make_cfg(honeymoon=[TOKYO]))
    assert client.post("/api/flex/run", params={"destination_id": "tokyo"}).status_code == 400   # no flex section


def test_scheduled_job_still_uses_run_all_only(monkeypatch):
    monkeypatch.setattr(main.scheduler, "add_job", lambda func, *a, **kw: calls.append(func))
    calls = []
    main.schedule_job(run_now=False)
    assert calls == [tracker.run_all]
