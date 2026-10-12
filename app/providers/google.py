"""Free provider: scrapes Google Flights through the fast-flights library.

Prices Google shows are the total for all passengers in the search.
Multi-city results are rendered client-side by Google, so they can't be
scraped this way — use the serpapi provider for those.
"""
from fast_flights import get_flights_from_filter

from ..searches import Search
from . import make_filter, parse_price


def search(s: Search, adults: int, currency: str) -> dict:
    if s.trip == "multi-city":
        raise ValueError("google scrape provider can't price multi-city; use serpapi")
    result = get_flights_from_filter(make_filter(s, adults), currency=currency, mode="common")
    offers = []
    for f in result.flights:
        price = parse_price(f.price)
        if not price:
            continue
        offers.append({
            "airline": f.name,
            "depart": f.departure,
            "arrive": f.arrival,
            "duration": f.duration,
            "stops": f.stops,
            "price": price,
        })
    # The scraper repeats some flights between the "best" and "other" lists.
    unique = {(o["airline"], o["depart"], o["price"]): o for o in offers}
    offers = sorted(unique.values(), key=lambda o: o["price"])
    return {
        "min_price": offers[0]["price"] if offers else None,
        "price_level": result.current_price or None,
        "offers": offers[:6],
    }
