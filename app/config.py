import os
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field

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
    honeymoon: List[Destination] = []

    @property
    def serpapi_key(self) -> str:
        return os.environ.get("SERPAPI_KEY", "")


def gui_destinations_path(config_path: Path = CONFIG_PATH) -> Path:
    default_path = config_path.parent / "user-data" / "user_destinations.yaml"
    return Path(os.environ.get("GUI_DESTINATIONS_PATH", default_path))


def load_config(path: Path = CONFIG_PATH) -> Config:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    gui_path = gui_destinations_path(path)
    if gui_path.exists():
        with gui_path.open() as f:
            gui_destinations = yaml.safe_load(f) or []
        data["honeymoon"] = [*(data.get("honeymoon") or []), *gui_destinations]
    return Config.model_validate(data)
