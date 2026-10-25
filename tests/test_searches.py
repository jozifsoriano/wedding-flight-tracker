from app.searches import Leg, Search, build_searches, one_way, round_trip, shift
from tests.conftest import make_cfg


def test_shift_crosses_month_boundary():
    assert shift("2027-02-27", 2) == "2027-03-01"
    assert shift("2027-03-05", -5) == "2027-02-28"


def test_search_key_and_label():
    ow = one_way("longhaul_out", "business", "google", "DFW", "DAD", "2027-03-05")
    assert ow.key == "one-way|business|DFW-DAD@2027-03-05"
    assert ow.label == "DFW → DAD 2027-03-05"

    rt = round_trip("honeymoon_rt", "economy", "google", "DAD", "HND", "2027-03-15", "2027-03-19")
    assert rt.legs == (Leg("DAD", "HND", "2027-03-15"), Leg("HND", "DAD", "2027-03-19"))
    assert rt.key == "round-trip|economy|DAD-HND@2027-03-15,HND-DAD@2027-03-19"
    assert rt.label == "DAD ⇄ HND (2027-03-15 / 2027-03-19)"

    mc = Search("multicity", "multi-city", "business",
                (Leg("DFW", "DAD", "2027-03-05"), Leg("HND", "DFW", "2027-03-19")), "serpapi")
    assert mc.label == "DFW → DAD 2027-03-05 + HND → DFW 2027-03-19"


def test_to_doc():
    doc = one_way("connector", "economy", "google", "SGN", "DAD", "2027-03-07").to_doc()
    assert doc == {
        "key": "one-way|economy|SGN-DAD@2027-03-07",
        "kind": "connector",
        "trip": "one-way",
        "cabin": "economy",
        "provider": "google",
        "label": "SGN → DAD 2027-03-07",
        "legs": [{"origin": "SGN", "dest": "DAD", "date": "2027-03-07"}],
    }


def test_build_searches_direct_gateway():
    keys = {s.key for s in build_searches(make_cfg())}
    assert keys == {
        "one-way|business|DFW-DAD@2027-03-05",
        "round-trip|business|DFW-DAD@2027-03-05,DAD-DFW@2027-03-20",  # home_after_return_days = 1
        "one-way|business|HND-DFW@2027-03-19",
        "one-way|economy|DAD-HND@2027-03-15",
        "round-trip|economy|DAD-HND@2027-03-15,HND-DAD@2027-03-19",
        "one-way|economy|HND-DAD@2027-03-19",
    }


def test_build_searches_sgn_gateway_adds_hops_on_offset_dates():
    cfg = make_cfg(gateways=["SGN"])
    keys = {s.key for s in build_searches(cfg)}
    assert "one-way|economy|SGN-DAD@2027-03-07" in keys   # outbound + connector_offset_days
    assert "one-way|economy|DAD-SGN@2027-03-20" in keys   # honeymoon_end + home_after_return_days
    assert "one-way|business|DFW-SGN@2027-03-05" in keys


def test_build_searches_fly_home_from_and_fly_out_via():
    cfg = make_cfg(honeymoon=[
        {"id": "pqc", "name": "Phu Quoc", "country": "Vietnam", "airports": ["PQC"], "fly_home_from": "SGN"},
        {"id": "mle", "name": "Maldives", "country": "Maldives", "airports": ["MLE"], "fly_out_via": "SGN"},
    ])
    by_key = {s.key: s for s in build_searches(cfg)}
    # Island with no long-haul business: economy hop to the hub, business home from the hub.
    assert by_key["one-way|economy|PQC-SGN@2027-03-19"].kind == "home_hop"
    assert by_key["one-way|business|SGN-DFW@2027-03-19"].kind == "longhaul_home"
    assert "one-way|business|PQC-DFW@2027-03-19" not in by_key
    # Honeymoon flights priced from the via hub, plus hops to/from the wedding city.
    assert by_key["one-way|economy|SGN-MLE@2027-03-15"].kind == "honeymoon_ow"
    assert "one-way|economy|DAD-MLE@2027-03-15" not in by_key
    assert by_key["one-way|economy|DAD-SGN@2027-03-15"].kind == "connector"
    assert by_key["one-way|economy|SGN-DAD@2027-03-19"].kind == "connector"


def test_build_searches_has_no_duplicate_keys():
    # Two destinations sharing a hub produce the same longhaul_home search; it must be priced once.
    cfg = make_cfg(gateways=["DAD", "SGN"], honeymoon=[
        {"id": "a", "name": "A", "country": "X", "airports": ["MPH"], "fly_home_from": "MNL"},
        {"id": "b", "name": "B", "country": "X", "airports": ["PPS"], "fly_home_from": "MNL"},
    ])
    keys = [s.key for s in build_searches(cfg)]
    assert len(keys) == len(set(keys))
    assert keys.count("one-way|business|MNL-DFW@2027-03-19") == 1


def test_multicity_searches_only_with_serpapi_key(monkeypatch):
    cfg = make_cfg()
    assert not [s for s in build_searches(cfg) if s.trip == "multi-city"]

    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    mc = [s for s in build_searches(cfg) if s.trip == "multi-city"]
    assert [s.key for s in mc] == ["multi-city|business|DFW-DAD@2027-03-05,HND-DFW@2027-03-19"]
    assert mc[0].provider == "serpapi"


def test_extended_stay_searches_only_outbound_hop_and_homebound_ticket(monkeypatch):
    cfg = make_cfg(honeymoon=[{
        "id": "seoul", "name": "Seoul", "country": "South Korea", "airports": ["ICN"],
        "start_dates": ["2027-03-15"], "end_dates": ["2027-03-19"], "extended_stay": True,
    }])
    keys = {s.key for s in build_searches(cfg)}
    assert "one-way|economy|DAD-ICN@2027-03-15" in keys
    assert "one-way|business|ICN-DFW@2027-03-19" in keys
    assert "round-trip|economy|DAD-ICN@2027-03-15,ICN-DAD@2027-03-19" not in keys
    assert "one-way|economy|ICN-DAD@2027-03-19" not in keys

    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    keys = {s.key for s in build_searches(cfg)}
    assert "multi-city|business|DFW-DAD@2027-03-05,ICN-DFW@2027-03-19" in keys


def test_every_date_combination_is_searched():
    cfg = make_cfg(dates={"outbound": ["2027-03-05", "2027-03-06"],
                          "honeymoon_start": ["2027-03-15", "2027-03-16"], "honeymoon_end": ["2027-03-19"]})
    keys = {s.key for s in build_searches(cfg)}
    for out in ("2027-03-05", "2027-03-06"):
        assert f"one-way|business|DFW-DAD@{out}" in keys
    for hs in ("2027-03-15", "2027-03-16"):
        assert f"round-trip|economy|DAD-HND@{hs},HND-DAD@2027-03-19" in keys
