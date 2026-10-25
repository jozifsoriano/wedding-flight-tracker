import pytest
from pydantic import ValidationError

from app.config import Config, load_config
from tests.conftest import ROOT


def test_bundled_config_yaml_loads():
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.home == "DFW"
    assert cfg.wedding_city == "DAD"
    assert cfg.gateways == ["DAD", "SGN"]
    assert cfg.cabins.long_haul == "business"
    ids = [d.id for d in cfg.honeymoon]
    assert len(ids) == len(set(ids)), "destination ids must be unique"
    for d in cfg.honeymoon:
        assert d.airports
    for destination_id, airport in (("seoul", "ICN"), ("saigon", "SGN")):
        dest = next(d for d in cfg.honeymoon if d.id == destination_id)
        assert dest.airports == [airport]
        assert dest.start_dates == ["2027-03-15"]
        assert dest.end_dates == ["2027-03-19"]
        assert dest.extended_stay is True


def test_minimal_config_gets_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("dates:\n  outbound: ['2027-03-05']\n  honeymoon_start: ['2027-03-15']\n  honeymoon_end: ['2027-03-19']\n")
    cfg = load_config(p)
    assert cfg.party.adults == 2
    assert cfg.currency == "USD"
    assert cfg.gateways == ["DAD"]
    assert cfg.cabins.regional == "economy"
    assert cfg.dates.connector_offset_days == 2
    assert cfg.dates.home_after_return_days == 1
    assert cfg.schedule.interval_hours == 12
    assert cfg.providers.default == "google"
    assert cfg.alerts.target_total == 0
    assert cfg.honeymoon == []


def test_dates_are_required(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("home: DFW\n")
    with pytest.raises(ValidationError):
        load_config(p)


def test_unknown_cabin_is_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate({
            "dates": {"outbound": [], "honeymoon_start": [], "honeymoon_end": []},
            "cabins": {"long_haul": "lie-flat"},
        })


def test_serpapi_key_comes_from_env(monkeypatch, cfg):
    assert cfg.serpapi_key == ""
    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    assert cfg.serpapi_key == "test-key"
