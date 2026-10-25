"""Turns the config into the concrete list of flight searches to price."""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Tuple

from .config import Config


@dataclass(frozen=True)
class Leg:
    origin: str
    dest: str
    date: str

    def __str__(self) -> str:
        return f"{self.origin}-{self.dest}@{self.date}"


@dataclass(frozen=True)
class Search:
    kind: str    # longhaul_out | longhaul_rt | longhaul_home | connector | home_hop | honeymoon_ow | honeymoon_back | honeymoon_rt | multicity
    trip: str    # one-way | round-trip | multi-city
    cabin: str
    legs: Tuple[Leg, ...]
    provider: str

    @property
    def key(self) -> str:
        return f"{self.trip}|{self.cabin}|" + ",".join(str(l) for l in self.legs)

    @property
    def label(self) -> str:
        if self.trip == "round-trip":
            a, b = self.legs
            return f"{a.origin} ⇄ {a.dest} ({a.date} / {b.date})"
        return " + ".join(f"{l.origin} → {l.dest} {l.date}" for l in self.legs)

    def to_doc(self) -> dict:
        return {
            "key": self.key,
            "kind": self.kind,
            "trip": self.trip,
            "cabin": self.cabin,
            "provider": self.provider,
            "label": self.label,
            "legs": [l.__dict__ for l in self.legs],
        }


def shift(d: str, days: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=days)).isoformat()


def one_way(kind, cabin, provider, o, d, day) -> Search:
    return Search(kind, "one-way", cabin, (Leg(o, d, day),), provider)


def round_trip(kind, cabin, provider, o, d, out_day, back_day) -> Search:
    return Search(kind, "round-trip", cabin, (Leg(o, d, out_day), Leg(d, o, back_day)), provider)


def build_searches(cfg: Config) -> List[Search]:
    d = cfg.dates
    home, city = cfg.home, cfg.wedding_city
    lh, rg = cfg.cabins.long_haul, cfg.cabins.regional
    p = cfg.providers.default
    out: List[Search] = []

    for out_day in d.outbound:
        for g in cfg.gateways:
            out.append(one_way("longhaul_out", lh, p, home, g, out_day))
            if g != city:
                out.append(one_way("connector", rg, p, g, city, shift(out_day, d.connector_offset_days)))
            round_trip_ends = set(d.honeymoon_end)
            round_trip_ends.update(
                he for dest in cfg.honeymoon if not dest.extended_stay
                for he in (dest.end_dates or d.honeymoon_end)
            )
            for he in sorted(round_trip_ends):
                back = shift(he, d.home_after_return_days)
                out.append(round_trip("longhaul_rt", lh, p, home, g, out_day, back))
                if g != city:
                    out.append(one_way("connector", rg, p, city, g, back))

    for dest in cfg.honeymoon:
        start_dates = dest.start_dates or d.honeymoon_start
        end_dates = dest.end_dates or d.honeymoon_end
        for ap in dest.airports:
            start = dest.fly_out_via or city
            hub = dest.fly_home_from or ap
            for hs in start_dates:
                out.append(one_way("honeymoon_ow", rg, p, start, ap, hs))
                if start != city:
                    out.append(one_way("connector", rg, p, city, start, hs))
                if not dest.extended_stay:
                    for he in end_dates:
                        out.append(round_trip("honeymoon_rt", rg, p, start, ap, hs, he))
                        out.append(one_way("honeymoon_back", rg, p, ap, start, he))
                        if start != city:
                            out.append(one_way("connector", rg, p, start, city, he))
            for he in end_dates:
                out.append(one_way("longhaul_home", lh, p, hub, home, he))
                if hub != ap:
                    out.append(one_way("home_hop", rg, p, ap, hub, he))
                if cfg.serpapi_key:
                    for out_day in d.outbound:
                        for g in cfg.gateways:
                            out.append(Search(
                                "multicity", "multi-city", lh,
                                (Leg(home, g, out_day), Leg(hub, home, he)),
                                cfg.providers.multicity,
                            ))

    seen, unique = set(), []
    for s in out:
        if s.key not in seen:
            seen.add(s.key)
            unique.append(s)
    return unique
