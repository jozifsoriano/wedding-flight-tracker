from app.config import load_config
from app.plans import _cheaper, best_by, cheapest, compute_plans
from app.searches import build_searches
from tests.conftest import ROOT, make_cfg, priced, quote

LONGHAUL_RT = "round-trip|business|DFW-DAD@2027-03-05,DAD-DFW@2027-03-20"
LONGHAUL_OUT = "one-way|business|DFW-DAD@2027-03-05"
LONGHAUL_HOME = "one-way|business|HND-DFW@2027-03-19"
HM_OW = "one-way|economy|DAD-HND@2027-03-15"
HM_BACK = "one-way|economy|HND-DAD@2027-03-19"
HM_RT = "round-trip|economy|DAD-HND@2027-03-15,HND-DAD@2027-03-19"


def by_type(plans):
    return {pl["type"]: pl for pl in plans}


def prices(**by_key):
    return {k: quote(v) for k, v in by_key.items()}


def test_plan_totals_for_direct_gateway(cfg):
    latest = prices(**{LONGHAUL_RT: 9000, LONGHAUL_OUT: 5000, LONGHAUL_HOME: 4500,
                       HM_OW: 300, HM_BACK: 350, HM_RT: 600})
    plans = by_type(compute_plans(cfg, latest))

    assert set(plans) == {"wedding", "rt_side", "open_jaw"}   # no multicity without a SerpApi key
    assert plans["wedding"]["total"] == 9000
    assert plans["wedding"]["destination_id"] == "wedding-only"
    assert plans["rt_side"]["total"] == 9000 + 600              # round trip beats 300 + 350
    assert plans["open_jaw"]["total"] == 5000 + 300 + 4500
    assert plans["open_jaw"]["per_person"] == 4900
    assert plans["open_jaw"]["destination"] == "Tokyo, Japan"
    assert plans["open_jaw"]["airport"] == "HND"
    assert plans["open_jaw"]["missing"] == []


def test_rt_side_uses_two_one_ways_when_cheaper(cfg):
    latest = prices(**{LONGHAUL_RT: 9000, HM_OW: 200, HM_BACK: 250, HM_RT: 600})
    rt_side = by_type(compute_plans(cfg, latest))["rt_side"]
    assert rt_side["total"] == 9000 + 450
    assert [c["key"] for c in rt_side["components"]] == [LONGHAUL_RT, HM_OW, HM_BACK]


def test_missing_price_leaves_plan_unpriced_and_sorted_last(cfg):
    latest = prices(**{LONGHAUL_RT: 9000, HM_RT: 600, LONGHAUL_OUT: 5000, HM_OW: 300})  # no LONGHAUL_HOME
    plans = compute_plans(cfg, latest)
    open_jaw = by_type(plans)["open_jaw"]
    assert open_jaw["total"] is None
    assert open_jaw["per_person"] is None
    assert open_jaw["missing"] == ["HND → DFW 2027-03-19"]   # search labels of the unpriced legs
    assert plans[-1] is open_jaw
    assert [pl["total"] for pl in plans[:-1]] == [9000, 9600]   # priced plans sorted cheapest first


def test_component_carries_quote_details(cfg):
    latest = prices(**{LONGHAUL_RT: 9000})
    comp = by_type(compute_plans(cfg, latest))["wedding"]["components"][0]
    assert comp["price"] == 9000
    assert comp["price_level"] == "typical"
    assert comp["url"] == "https://example.test/flights"
    assert comp["top_offer"] == {"airline": "X", "price": 9000}
    assert comp["fetched_at"] == "2026-10-01T00:00:00+00:00"
    assert comp["cabin"] == "business"


def test_sgn_gateway_adds_hops_to_every_plan():
    cfg = make_cfg(gateways=["SGN"])
    plans = by_type(compute_plans(cfg, priced(build_searches(cfg))))
    roles = lambda pl: [c["role"] for c in pl["components"]]  # noqa: E731
    assert roles(plans["wedding"]) == ["DFW ⇄ SGN round trip", "SGN → DAD hop", "DAD → SGN hop"]
    assert "SGN → DAD hop" in roles(plans["open_jaw"])
    assert "DAD → SGN hop" not in roles(plans["open_jaw"])   # open jaw flies home from the honeymoon


def test_fly_home_from_adds_hop_in_open_jaw():
    cfg = make_cfg(honeymoon=[{"id": "pqc", "name": "Phu Quoc", "country": "Vietnam",
                               "airports": ["PQC"], "fly_home_from": "SGN"}])
    open_jaw = by_type(compute_plans(cfg, priced(build_searches(cfg))))["open_jaw"]
    assert [c["role"] for c in open_jaw["components"]] == ["DFW → DAD", "DAD → PQC", "PQC → SGN hop", "SGN → DFW"]
    assert open_jaw["total"] == 400


def test_fly_out_via_prices_honeymoon_from_hub():
    cfg = make_cfg(honeymoon=[{"id": "mle", "name": "Maldives", "country": "Maldives",
                               "airports": ["MLE"], "fly_out_via": "SGN"}])
    rt_side = by_type(compute_plans(cfg, priced(build_searches(cfg))))["rt_side"]
    # Every search costs 100, so the honeymoon round trip (100) beats two one-ways (200).
    assert [c["role"] for c in rt_side["components"]] == [
        "DFW ⇄ DAD round trip", "DAD → SGN hop", "SGN ⇄ MLE honeymoon", "SGN → DAD hop",
    ]


def test_multicity_plan_with_serpapi_key(monkeypatch, cfg):
    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    plans = by_type(compute_plans(cfg, priced(build_searches(cfg))))
    mc = plans["multicity"]
    assert mc["components"][0]["key"] == "multi-city|business|DFW-DAD@2027-03-05,HND-DFW@2027-03-19"
    assert mc["total"] == 200   # one multi-city ticket + DAD → HND


def test_extended_stay_compares_multicity_with_separate_longhaul_tickets(monkeypatch):
    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    cfg = make_cfg(honeymoon=[{
        "id": "seoul", "name": "Seoul", "country": "South Korea", "airports": ["ICN"],
        "start_dates": ["2027-03-15"], "end_dates": ["2027-03-19"], "extended_stay": True,
    }])
    plans = compute_plans(cfg, priced(build_searches(cfg)))
    seoul = [p for p in plans if p["destination_id"] == "seoul"]
    by_type = {p["type"]: p for p in seoul}
    assert set(by_type) == {"open_jaw", "multicity"}
    assert by_type["open_jaw"]["total"] == 300
    assert by_type["multicity"]["total"] == 200
    assert [c["role"] for c in by_type["multicity"]["components"]] == [
        "DFW → DAD + ICN → DFW (one ticket)", "DAD → ICN",
    ]


def test_every_plan_component_is_a_searched_route(monkeypatch):
    """compute_plans must only reference keys that build_searches actually prices."""
    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    cfg = load_config(ROOT / "config.yaml")
    searched = {s.key for s in build_searches(cfg)}
    plans = compute_plans(cfg, priced(build_searches(cfg)))
    assert plans
    for pl in plans:
        assert pl["missing"] == [], pl["id"]
        for c in pl["components"]:
            assert c["key"] in searched


def test_plan_ids_are_unique():
    cfg = load_config(ROOT / "config.yaml")
    ids = [pl["id"] for pl in compute_plans(cfg, {})]
    assert len(ids) == len(set(ids))


def c(price):
    return {"price": price}


def test_cheaper():
    assert _cheaper([c(600)], [c(300), c(250)]) == [c(300), c(250)]
    assert _cheaper([c(500)], [c(300), c(250)]) == [c(500)]
    assert _cheaper([c(550)], [c(300), c(250)]) == [c(550)]           # tie keeps the round trip
    assert _cheaper([c(None)], [c(300), c(250)]) == [c(300), c(250)]  # unpriced round trip
    assert _cheaper([c(600)], [c(300), c(None)]) == [c(600)]          # one one-way unpriced
    assert _cheaper([c(None)], [c(None), c(250)]) == [c(None)]        # neither priced: keep the first


def plan(dest, ptype, total):
    return {"destination_id": dest, "type": ptype, "total": total}


def test_best_by_picks_cheapest_per_group_and_skips_unpriced():
    plans = [plan("tokyo", "rt_side", 9000), plan("tokyo", "open_jaw", 8000), plan("tokyo", "open_jaw", None),
             plan("osaka", "rt_side", None), plan("phuket", "open_jaw", 7000), plan("phuket", "open_jaw", 7500)]
    assert best_by(plans, "destination_id") == {
        ("tokyo",): plans[1], ("phuket",): plans[4],
    }
    by_dt = best_by(plans, "destination_id", "type")
    assert set(by_dt) == {("tokyo", "rt_side"), ("tokyo", "open_jaw"), ("phuket", "open_jaw")}
    assert by_dt[("phuket", "open_jaw")]["total"] == 7000


def test_cheapest_returns_first_priced_plan():
    assert cheapest([plan("a", "x", 5), plan("b", "x", None)])["destination_id"] == "a"
    assert cheapest([plan("b", "x", None)]) is None
    assert cheapest([]) is None
