"""Combines individual search prices into complete trip plans for the couple.

Every plan gets you DFW -> Da Nang for the wedding, to a honeymoon spot, and home.

  rt_side   Round trip DFW<->gateway (business) + Da Nang<->honeymoon (round trip
            or two one-ways, whichever is cheaper), then home from Da Nang.
  open_jaw  One-way DFW->gateway + Da Nang->honeymoon + one-way honeymoon->DFW
            (business). No backtracking to Vietnam, no Vietnam re-entry. Islands
            with no long-haul business fares hop to `fly_home_from` first.
  multicity Same route as open_jaw, but the two business legs are one
            multi-city ticket (SerpApi only) — often cheaper than two one-ways.
  wedding   Baseline: just DFW<->Da Nang, no honeymoon flights.

A SGN gateway adds the SGN->DAD hop (and DAD->SGN on the way home for rt_side).
"""
from itertools import product
from typing import Dict, List, Optional

from .config import Config
from .searches import Leg, Search, one_way, round_trip, shift

PLAN_TYPES = {
    "rt_side": "Round trip + side trip",
    "open_jaw": "Open jaw (fly home from honeymoon)",
    "multicity": "Multi-city business ticket",
    "wedding": "Wedding only (no honeymoon)",
}


def compute_plans(cfg: Config, latest: Dict[str, dict]) -> List[dict]:
    d = cfg.dates
    home, city = cfg.home, cfg.wedding_city
    lh, rg, p = cfg.cabins.long_haul, cfg.cabins.regional, cfg.providers.default
    plans: List[dict] = []

    def component(s: Search, role: str) -> dict:
        q = latest.get(s.key)
        return {
            "key": s.key,
            "role": role,
            "label": s.label,
            "cabin": s.cabin,
            "price": q["min_price"] if q else None,
            "price_level": q.get("price_level") if q else None,
            "fetched_at": q["fetched_at"].isoformat() if q else None,
            "url": q.get("booking_url") if q else None,
            "top_offer": (q.get("offers") or [None])[0] if q else None,
        }

    def add(ptype: str, dest, gateway: str, dates: dict, comps: List[dict]) -> None:
        missing = [c["label"] for c in comps if c["price"] is None]
        total = sum(c["price"] for c in comps) if not missing else None
        plans.append({
            "id": f"{ptype}:{dest.id if dest else 'none'}:{gateway}:" + ":".join(dates.values()),
            "type": ptype,
            "type_label": PLAN_TYPES[ptype],
            "destination_id": dest.id if dest else "wedding-only",
            "destination": f"{dest.name}, {dest.country}" if dest else "Wedding only",
            "airport": dates.get("airport"),
            "gateway": gateway,
            "dates": dates,
            "components": comps,
            "missing": missing,
            "total": total,
            "per_person": round(total / cfg.party.adults) if total else None,
        })

    def connector_in(g: str, out_day: str) -> List[dict]:
        if g == city:
            return []
        return [component(one_way("connector", rg, p, g, city, shift(out_day, d.connector_offset_days)),
                          f"{g} → {city} hop")]

    for out_day, g in product(d.outbound, cfg.gateways):
        for he in d.honeymoon_end:
            back = shift(he, d.home_after_return_days)
            rt = component(round_trip("longhaul_rt", lh, p, home, g, out_day, back),
                           f"{home} ⇄ {g} round trip")
            conn_back = [] if g == city else [
                component(one_way("connector", rg, p, city, g, back), f"{city} → {g} hop")]
            add("wedding", None, g, {"outbound": out_day, "home": back},
                [rt] + connector_in(g, out_day) + conn_back)

        for dest in cfg.honeymoon:
            starts = dest.start_dates or d.honeymoon_start
            ends = dest.end_dates or d.honeymoon_end
            for ap in dest.airports:
                start = dest.fly_out_via or city
                hub = dest.fly_home_from or ap
                for hs, he in product(starts, ends):
                    dates = {
                        "outbound": out_day, "honeymoon_start": hs, "honeymoon_end": he, "airport": ap,
                    }
                    via_out = [] if start == city else [
                        component(one_way("connector", rg, p, city, start, hs), f"{city} → {start} hop")]
                    to_honeymoon = component(one_way("honeymoon_ow", rg, p, start, ap, hs),
                                             f"{start} → {ap}")

                    if not dest.extended_stay:
                        back = shift(he, d.home_after_return_days)
                        rt = component(round_trip("longhaul_rt", lh, p, home, g, out_day, back),
                                       f"{home} ⇄ {g} round trip")
                        conn_back = [] if g == city else [
                            component(one_way("connector", rg, p, city, g, back), f"{city} → {g} hop")]
                        via_back = [] if start == city else [
                            component(one_way("connector", rg, p, start, city, he),
                                      f"{start} → {city} hop")]
                        side = _cheaper(
                            [component(round_trip("honeymoon_rt", rg, p, start, ap, hs, he),
                                       f"{start} ⇄ {ap} honeymoon")],
                            [to_honeymoon, component(one_way("honeymoon_back", rg, p, ap, start, he),
                                                     f"{ap} → {start}")],
                        )
                        add("rt_side", dest, g, {**dates, "home": back},
                            [rt] + connector_in(g, out_day) + via_out + side + via_back + conn_back)

                    hop = [] if hub == ap else [
                        component(one_way("home_hop", rg, p, ap, hub, he), f"{ap} → {hub} hop")]
                    add("open_jaw", dest, g, dates, [
                        component(one_way("longhaul_out", lh, p, home, g, out_day), f"{home} → {g}"),
                        *connector_in(g, out_day),
                        *via_out,
                        to_honeymoon,
                        *hop,
                        component(one_way("longhaul_home", lh, p, hub, home, he), f"{hub} → {home}"),
                    ])

                    if cfg.serpapi_key:
                        mc = Search("multicity", "multi-city", lh,
                                    (Leg(home, g, out_day), Leg(hub, home, he)), cfg.providers.multicity)
                        add("multicity", dest, g, dates, [
                            component(mc, f"{home} → {g} + {hub} → {home} (one ticket)"),
                            *connector_in(g, out_day),
                            *via_out,
                            to_honeymoon,
                            *hop,
                        ])

    plans.sort(key=lambda pl: (pl["total"] is None, pl["total"] or 0))
    return plans


def _cheaper(a: List[dict], b: List[dict]) -> List[dict]:
    """Pick the fully-priced option with the lower total (round trip vs. two one-ways)."""
    def total(cs):
        return sum(c["price"] for c in cs) if all(c["price"] is not None for c in cs) else None
    ta, tb = total(a), total(b)
    if ta is None:
        return b if tb is not None else a
    return b if tb is not None and tb < ta else a


def best_by(plans: List[dict], *fields: str) -> Dict[tuple, dict]:
    best: Dict[tuple, dict] = {}
    for pl in plans:
        if pl["total"] is None:
            continue
        k = tuple(pl[f] for f in fields)
        if k not in best or pl["total"] < best[k]["total"]:
            best[k] = pl
    return best


def cheapest(plans: List[dict]) -> Optional[dict]:
    return next((pl for pl in plans if pl["total"] is not None), None)
