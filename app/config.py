import os
import re
from datetime import date
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

Cabin = Literal["economy", "premium-economy", "business", "first"]

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/config/config.yaml"))


class Party(BaseModel):
    adults: int = 2


class Cabins(BaseModel):
    long_haul: Cabin = "business"
    regional: Cabin = "economy"


class Dates(BaseModel):
    outbound: List[str]
    honeymoon_start: List[str]
    honeymoon_end: List[str]
    connector_offset_days: int = 2
    home_after_return_days: int = 1


class Schedule(BaseModel):
    interval_hours: float = 12
    request_delay_seconds: float = 8
    run_on_startup: bool = True


class Providers(BaseModel):
    default: Literal["google", "serpapi"] = "google"
    multicity: Literal["serpapi"] = "serpapi"
    serpapi_monthly_budget: int = 240


class Alerts(BaseModel):
    ntfy_url: str = ""
    discord_webhook: str = ""
    min_drop_pct: float = 3
    target_total: float = 0


class HopPenalty(BaseModel):
    max_usd: float = 300       # most a plan's hops can cost in ranking, total for the party
    vietnam_usd: float = 100   # penalty for a hop entirely inside Vietnam (SGN <-> DAD, PQC <-> SGN)


class Flex(BaseModel):
    outbound: List[str] = []
    honeymoon_end: List[str] = []
    latest_home_arrival: Optional[str] = None   # hard cap: no plan lands back home after this date
    top_candidates: int = 3                     # plan shapes per destination priced by the flex search

    @property
    def enabled(self) -> bool:
        return bool(self.outbound or self.honeymoon_end)


class Destination(BaseModel):
    id: str
    name: str
    country: str
    airports: List[str]
    start_dates: Optional[List[str]] = None
    end_dates: Optional[List[str]] = None
    extended_stay: bool = False
    fly_home_from: Optional[str] = None  # hub for the business flight home in open-jaw plans
    fly_out_via: Optional[str] = None    # price honeymoon flights from this hub (plus a hop from the wedding city)
    from_dad: str = ""
    march: str = ""
    vibe: str = ""
    entry: str = ""
    tip: str = ""


Shape = Literal["round-trip", "one-way", "multi-city"]
MAX_MULTI_CITY_LEGS = 5


class TripLeg(BaseModel):
    origin: str       # IATA airport (3 letters) or city code (e.g. TYOA)
    destination: str
    date: str         # YYYY-MM-DD

    @field_validator("origin", "destination")
    @classmethod
    def place_code(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3,4}", value):
            raise ValueError("airport/city codes are three or four letters")
        return value

    @field_validator("date")
    @classmethod
    def iso_date(cls, value: str) -> str:
        return date.fromisoformat(value.strip()).isoformat()


class TrackedSearch(BaseModel):
    """A flight itinerary priced through SlickTrip (via the Claude bridge). Prices are totals for all travelers."""
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,59}$")
    name: str = Field(min_length=1, max_length=80)
    shape: Shape
    cabin: Cabin = "business"
    travelers: int = Field(default=2, ge=1, le=9)
    legs: List[TripLeg]
    note: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def legs_match_shape(self):
        legs = self.legs
        if self.shape == "one-way" and len(legs) != 1:
            raise ValueError("a one-way search has exactly one leg")
        if self.shape == "round-trip":
            if len(legs) != 2 or (legs[0].origin, legs[0].destination) != (legs[1].destination, legs[1].origin):
                raise ValueError("a round trip has an outbound leg and the reverse return leg")
        if self.shape == "multi-city" and not 2 <= len(legs) <= MAX_MULTI_CITY_LEGS:
            raise ValueError(f"a multi-city search has 2-{MAX_MULTI_CITY_LEGS} legs")
        if any(a.date > b.date for a, b in zip(legs, legs[1:])):
            raise ValueError("legs must be in date order")
        return self


class Config(BaseModel):
    party: Party = Field(default_factory=Party)
    currency: str = "USD"
    home: str = "DFW"
    wedding_city: str = "DAD"
    gateways: List[str] = ["DAD"]
    cabins: Cabins = Field(default_factory=Cabins)
    dates: Dates
    schedule: Schedule = Field(default_factory=Schedule)
    providers: Providers = Field(default_factory=Providers)
    alerts: Alerts = Field(default_factory=Alerts)
    hop_penalty: HopPenalty = Field(default_factory=HopPenalty)
    flex: Flex = Field(default_factory=Flex)
    honeymoon: List[Destination] = []
    tracked_searches: List[TrackedSearch] = []   # priced via SlickTrip (config.yaml ones are read-only)

    @model_validator(mode="after")
    def unique_search_ids(self):
        ids = [t.id for t in self.tracked_searches]
        if len(ids) != len(set(ids)):
            raise ValueError("tracked_searches ids must be unique")
        return self

    @property
    def serpapi_key(self) -> str:
        return os.environ.get("SERPAPI_KEY", "")


def gui_destinations_path(config_path: Path = CONFIG_PATH) -> Path:
    default_path = config_path.parent / "user-data" / "user_destinations.yaml"
    return Path(os.environ.get("GUI_DESTINATIONS_PATH", default_path))


def read_store(path: Path) -> tuple[list[dict], bool]:
    """App-managed destinations: (destinations, migrated). The file is a YAML list (legacy) or
    {migrated: true, destinations: [...]}; once migrated, the honeymoon list in config.yaml is ignored."""
    if not path.exists():
        return [], False
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    if isinstance(data, dict):
        return list(data.get("destinations") or []), bool(data.get("migrated"))
    if isinstance(data, list):
        return data, False
    raise ValueError("destinations file must contain a YAML list or a mapping")


def gui_searches_path(config_path: Path = CONFIG_PATH) -> Path:
    default_path = gui_destinations_path(config_path).with_name("user_searches.yaml")
    return Path(os.environ.get("GUI_SEARCHES_PATH", default_path))


def read_search_store(path: Path) -> list[dict]:
    """App-managed SlickTrip searches (YAML list); missing file = none."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    if not isinstance(data, list):
        raise ValueError("searches file must contain a YAML list")
    return data


def load_config(path: Path = CONFIG_PATH) -> Config:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    data["tracked_searches"] = [*(data.get("tracked_searches") or []), *read_search_store(gui_searches_path(path))]
    stored, migrated = read_store(gui_destinations_path(path))
    data["honeymoon"] = stored if migrated else [*(data.get("honeymoon") or []), *stored]
    return Config.model_validate(data)
