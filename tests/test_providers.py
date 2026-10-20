import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.providers import google, google_flights_url, parse_price, serpapi
from app.searches import Leg, Search, one_way, round_trip

OW = one_way("longhaul_out", "business", "google", "DFW", "DAD", "2027-03-05")
RT = round_trip("honeymoon_rt", "economy", "serpapi", "DAD", "HND", "2027-03-15", "2027-03-19")
MC = Search("multicity", "multi-city", "business",
            (Leg("DFW", "DAD", "2027-03-05"), Leg("HND", "DFW", "2027-03-19")), "serpapi")


@pytest.mark.parametrize("text, expected", [
    ("$1,234", 1234),
    ("US$987", 987),
    ("$12,345 ", 12345),
    (850, 850),
    (850.7, 850),
    ("", None),
    (None, None),
    ("Price unavailable", None),
])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


def test_google_flights_url_is_deterministic_deep_link():
    url = google_flights_url(OW, 2, "USD")
    parsed = urlparse(url)
    assert parsed.netloc == "www.google.com"
    assert parsed.path == "/travel/flights"
    qs = parse_qs(parsed.query)
    assert qs["curr"] == ["USD"] and qs["hl"] == ["en"] and qs["tfs"][0]
    assert url == google_flights_url(OW, 2, "USD")
    assert url != google_flights_url(OW, 1, "USD")


# --- google (fast-flights) -------------------------------------------------

def flight(name, price, departure="8:00 AM", **kw):
    return SimpleNamespace(name=name, price=price, departure=departure, arrival="5:00 PM",
                           duration="20 hr", stops=kw.get("stops", 1))


def test_google_search_normalises_and_dedupes(monkeypatch):
    calls = []

    def fake_get(flt, currency, mode):
        calls.append((currency, mode))
        return SimpleNamespace(current_price="low", flights=[
            flight("JAL", "$9,500"),
            flight("AA", "$8,000"),
            flight("JAL", "$9,500"),               # repeated between "best" and "other" lists
            flight("Vietjet", "Price unavailable"),
            *[flight(f"Air{i}", f"${10000 + i}") for i in range(6)],
        ])

    monkeypatch.setattr(google, "get_flights_from_filter", fake_get)
    out = google.search(OW, 2, "USD")
    assert calls == [("USD", "common")]
    assert out["min_price"] == 8000
    assert out["price_level"] == "low"
    assert [o["price"] for o in out["offers"]] == [8000, 9500, 10000, 10001, 10002, 10003]   # sorted, capped at 6
    assert out["offers"][0] == {"airline": "AA", "depart": "8:00 AM", "arrive": "5:00 PM",
                                "duration": "20 hr", "stops": 1, "price": 8000}


def test_google_search_no_prices(monkeypatch):
    monkeypatch.setattr(google, "get_flights_from_filter",
                        lambda *a, **k: SimpleNamespace(current_price="", flights=[flight("X", "")]))
    assert google.search(OW, 2, "USD") == {"min_price": None, "price_level": None, "offers": []}


def test_google_refuses_multicity(monkeypatch):
    monkeypatch.setattr(google, "get_flights_from_filter", lambda *a, **k: pytest.fail("should not scrape"))
    with pytest.raises(ValueError):
        google.search(MC, 2, "USD")


# --- serpapi -----------------------------------------------------------------

def fake_serpapi(monkeypatch, payload, status=200):
    seen = {}

    def fake_get(url, params, timeout):
        seen.update(url=url, params=params)
        return httpx.Response(status, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(serpapi.httpx, "get", fake_get)
    return seen


def serp_item(price, airlines, duration=None):
    return {"price": price, "total_duration": duration, "flights": [
        {"airline": a, "departure_airport": {"time": f"dep-{i}"}, "arrival_airport": {"time": f"arr-{i}"}}
        for i, a in enumerate(airlines)
    ]}


def test_serpapi_round_trip_params(monkeypatch):
    seen = fake_serpapi(monkeypatch, {})
    serpapi.search(RT, 2, "USD", "test-key")
    p = seen["params"]
    assert seen["url"] == "https://serpapi.com/search.json"
    assert p["api_key"] == "test-key" and p["adults"] == 2 and p["currency"] == "USD"
    assert p["travel_class"] == 1 and p["type"] == 1
    assert (p["departure_id"], p["arrival_id"]) == ("DAD", "HND")
    assert (p["outbound_date"], p["return_date"]) == ("2027-03-15", "2027-03-19")
    assert "multi_city_json" not in p


def test_serpapi_one_way_params(monkeypatch):
    seen = fake_serpapi(monkeypatch, {})
    serpapi.search(OW, 2, "USD", "k")
    assert seen["params"]["type"] == 2 and seen["params"]["travel_class"] == 3
    assert "return_date" not in seen["params"]


def test_serpapi_multicity_params(monkeypatch):
    seen = fake_serpapi(monkeypatch, {})
    serpapi.search(MC, 2, "USD", "k")
    p = seen["params"]
    assert p["type"] == 3
    assert json.loads(p["multi_city_json"]) == [
        {"departure_id": "DFW", "arrival_id": "DAD", "date": "2027-03-05"},
        {"departure_id": "HND", "arrival_id": "DFW", "date": "2027-03-19"},
    ]
    assert "departure_id" not in p


def test_serpapi_normalises_offers(monkeypatch):
    fake_serpapi(monkeypatch, {
        "best_flights": [serp_item(9100, ["JAL", "JAL", "AA"], duration=1290)],
        "other_flights": [serp_item(8700, ["Qatar"]), serp_item(None, ["Skip"]), {"price": 5, "flights": []}],
        "price_insights": {"price_level": "high", "lowest_price": 8000},
    })
    out = serpapi.search(MC, 2, "USD", "k")
    assert out["min_price"] == 8700
    assert out["price_level"] == "high"
    assert out["offers"] == [
        {"airline": "Qatar", "depart": "dep-0", "arrive": "arr-0", "duration": "", "stops": 0, "price": 8700},
        {"airline": "JAL, AA", "depart": "dep-0", "arrive": "arr-2", "duration": "21 hr 30 min",
         "stops": 2, "price": 9100},
    ]


def test_serpapi_falls_back_to_insights_lowest_price(monkeypatch):
    fake_serpapi(monkeypatch, {"price_insights": {"lowest_price": 7777}})
    assert serpapi.search(OW, 2, "USD", "k") == {"min_price": 7777, "price_level": None, "offers": []}


def test_serpapi_error_payload_raises(monkeypatch):
    fake_serpapi(monkeypatch, {"error": "Invalid API key"})
    with pytest.raises(RuntimeError, match="Invalid API key"):
        serpapi.search(OW, 2, "USD", "bad")


def test_serpapi_http_error_raises(monkeypatch):
    fake_serpapi(monkeypatch, {}, status=500)
    with pytest.raises(httpx.HTTPStatusError):
        serpapi.search(OW, 2, "USD", "k")
