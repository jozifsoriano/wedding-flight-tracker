from app.plans import best_by, cheapest, compute_plans
from app.searches import build_searches
from tests.conftest import make_cfg, priced, quote


def sgn_cfg(**overrides):
    return make_cfg(gateways=["DAD", "SGN"], **overrides)


def priced_by_kind(cfg, default=100, **by_kind):
    """Quote every search at by_kind[kind] (else `default`); long-haul out to SGN uses by_kind['sgn_out']."""
    latest = {}
    for s in build_searches(cfg):
        price = by_kind.get(s.kind, default)
        if s.kind == "longhaul_out" and s.legs[0].dest == "SGN":
            price = by_kind["sgn_out"]
        latest[s.key] = quote(price)
    return latest


def open_jaw_by_gateway(plans):
    return {pl["gateway"]: pl for pl in plans if pl["type"] == "open_jaw"}


def best_open_jaw_gateway(plans):
    return best_by(plans, "destination_id", "type")[("tokyo", "open_jaw")]["gateway"]


def test_hop_fields_and_labels():
    cfg = sgn_cfg()
    oj = open_jaw_by_gateway(compute_plans(cfg, priced(build_searches(cfg))))
    assert oj["DAD"]["hop_count"] == 0 and oj["DAD"]["hop_label"] == "direct" and oj["DAD"]["hops"] == []
    assert oj["DAD"]["rank_score"] == oj["DAD"]["total"]
    hop = oj["SGN"]
    assert hop["hop_count"] == 1 and hop["hop_label"] == "hop"
    assert hop["hops"] == [{"type": "connector", "role": "SGN → DAD hop", "price": 100, "vietnam": True, "penalty": 100}]
    assert hop["total"] == 400 and hop["hop_penalty"] == 100 and hop["rank_score"] == 500


def test_hop_plan_wins_only_when_savings_exceed_penalty():
    cfg = sgn_cfg()   # Vietnam hop penalty is 100; the SGN plan also pays a 100 SGN->DAD hop fare
    def plans_for(sgn_out):
        return compute_plans(cfg, priced_by_kind(cfg, longhaul_out=5000, sgn_out=sgn_out))

    plans = plans_for(4750)   # 4750 + 100 hop fare = 150 cheaper than direct, more than the 100 penalty: hop wins
    oj = open_jaw_by_gateway(plans)
    assert oj["DAD"]["total"] - oj["SGN"]["total"] == 150
    assert best_open_jaw_gateway(plans) == "SGN"
    assert oj["SGN"]["hop_note"] == "hop saves $150"

    plans = plans_for(4850)   # only $50 cheaper in real money: loses to the no-hop plan but stays listed
    oj = open_jaw_by_gateway(plans)
    assert oj["SGN"]["total"] < oj["DAD"]["total"]
    assert best_open_jaw_gateway(plans) == "DAD"
    assert cheapest(plans)["gateway"] == "DAD"
    assert oj["SGN"]["hop_note"] == "hop saves $50"
    assert oj["SGN"]["total"] == oj["SGN"]["rank_score"] - 100   # total stays the real price

    plans = plans_for(4900)   # tie in real money: the no-hop plan wins
    assert best_open_jaw_gateway(plans) == "DAD"

    plans = plans_for(5100)   # 5100 + 100 hop vs 5000 direct
    assert open_jaw_by_gateway(plans)["SGN"]["hop_note"] == "hop costs $200 more"


def test_vietnam_hop_uses_vietnam_penalty_other_hops_use_max():
    cfg = make_cfg(
        hop_penalty={"max_usd": 300, "vietnam_usd": 40},
        honeymoon=[{"id": "pqc", "name": "Phu Quoc", "country": "Vietnam", "airports": ["PQC"], "fly_home_from": "SGN"},
                   {"id": "usm", "name": "Samui", "country": "Thailand", "airports": ["USM"], "fly_home_from": "BKK"}])
    plans = {pl["destination_id"]: pl
             for pl in compute_plans(cfg, priced(build_searches(cfg))) if pl["type"] == "open_jaw"}
    assert plans["pqc"]["hop_penalty"] == 40 and plans["pqc"]["hops"][0]["vietnam"] is True
    assert plans["pqc"]["hops"][0]["type"] == "home_hop"
    assert plans["pqc"]["rank_score"] == plans["pqc"]["total"] + 40
    assert plans["usm"]["hop_penalty"] == 300 and plans["usm"]["hops"][0]["vietnam"] is False


def test_hop_penalty_is_capped_at_max():
    cfg = make_cfg(gateways=["SGN"], hop_penalty={"max_usd": 150, "vietnam_usd": 100},
                   honeymoon=[{"id": "pqc", "name": "Phu Quoc", "country": "Vietnam", "airports": ["PQC"],
                               "fly_home_from": "SGN"}])
    pl = [p for p in compute_plans(cfg, priced(build_searches(cfg))) if p["type"] == "rt_side"][0]
    assert pl["hop_count"] == 2 and sum(h["penalty"] for h in pl["hops"]) == 200
    assert pl["hop_penalty"] == 150 and pl["rank_score"] == pl["total"] + 150


def test_via_hops_are_recorded_for_fly_out_via():
    cfg = make_cfg(honeymoon=[{"id": "mle", "name": "Maldives", "country": "Maldives",
                               "airports": ["MLE"], "fly_out_via": "SGN"}])
    rt_side = {p["type"]: p for p in compute_plans(cfg, priced(build_searches(cfg)))}["rt_side"]
    assert [h["type"] for h in rt_side["hops"]] == ["via", "via"]
    assert rt_side["hop_count"] == 2 and rt_side["hop_penalty"] == 200


def test_plans_are_sorted_by_rank_score_and_unpriced_last():
    cfg = sgn_cfg()
    plans = compute_plans(cfg, priced_by_kind(cfg, longhaul_out=5000, sgn_out=4850))
    scores = [pl["rank_score"] for pl in plans]
    assert scores == sorted(scores)
    assert compute_plans(cfg, {})[0]["rank_score"] is None


def test_best_by_uses_rank_score_but_keeps_real_totals():
    cheap_hop = {"destination_id": "a", "type": "x", "total": 900, "rank_score": 1000}
    direct = {"destination_id": "a", "type": "x", "total": 950, "rank_score": 950}
    assert best_by([cheap_hop, direct], "destination_id")[("a",)] is direct
    assert cheap_hop["total"] == 900
