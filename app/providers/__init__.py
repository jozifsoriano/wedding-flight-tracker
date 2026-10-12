import re
from typing import Optional

from fast_flights import FlightData, Passengers, create_filter

from ..searches import Search


def parse_price(text) -> Optional[int]:
    if isinstance(text, (int, float)):
        return int(text)
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def make_filter(search: Search, adults: int):
    return create_filter(
        flight_data=[FlightData(date=l.date, from_airport=l.origin, to_airport=l.dest) for l in search.legs],
        trip=search.trip,
        passengers=Passengers(adults=adults),
        seat=search.cabin,
    )


def google_flights_url(search: Search, adults: int, currency: str) -> str:
    """Deep link that opens the same search on google.com/travel/flights."""
    tfs = make_filter(search, adults).as_b64().decode()
    return f"https://www.google.com/travel/flights?tfs={tfs}&curr={currency}&hl=en"
