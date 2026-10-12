"""Paid provider: SerpApi's Google Flights API (https://serpapi.com/google-flights-api).

Supports true multi-city tickets. Each call counts against your SerpApi plan.
"""
import json

import httpx

from ..searches import Search

TRAVEL_CLASS = {"economy": 1, "premium-economy": 2, "business": 3, "first": 4}
TRIP_TYPE = {"round-trip": 1, "one-way": 2, "multi-city": 3}


def _fmt_minutes(m) -> str:
    return f"{m // 60} hr {m % 60} min" if isinstance(m, int) else ""


def search(s: Search, adults: int, currency: str, api_key: str) -> dict:
    params = {
        "engine": "google_flights",
        "api_key": api_key,
        "currency": currency,
        "hl": "en",
        "gl": "us",
        "adults": adults,
        "travel_class": TRAVEL_CLASS[s.cabin],
        "type": TRIP_TYPE[s.trip],
    }
    first = s.legs[0]
    if s.trip == "multi-city":
        params["multi_city_json"] = json.dumps(
            [{"departure_id": l.origin, "arrival_id": l.dest, "date": l.date} for l in s.legs]
        )
    else:
        params.update(departure_id=first.origin, arrival_id=first.dest, outbound_date=first.date)
        if s.trip == "round-trip":
            params["return_date"] = s.legs[1].date

    r = httpx.get("https://serpapi.com/search.json", params=params, timeout=120)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(data["error"])

    offers = []
    for item in data.get("best_flights", []) + data.get("other_flights", []):
        segs = item.get("flights") or []
        if not item.get("price") or not segs:
            continue
        offers.append({
            "airline": ", ".join(dict.fromkeys(seg.get("airline", "") for seg in segs)),
            "depart": segs[0].get("departure_airport", {}).get("time", ""),
            "arrive": segs[-1].get("arrival_airport", {}).get("time", ""),
            "duration": _fmt_minutes(item.get("total_duration")),
            "stops": len(segs) - 1,
            "price": int(item["price"]),
        })
    offers.sort(key=lambda o: o["price"])
    insights = data.get("price_insights") or {}
    return {
        "min_price": offers[0]["price"] if offers else insights.get("lowest_price"),
        "price_level": insights.get("price_level"),
        "offers": offers[:6],
    }
