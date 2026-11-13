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

Hops (gateway connectors, the home hop to a fly_home_from hub, fly_out_via hops) are tracked per plan.
`total` is always the real price; `rank_score` adds a hop penalty (config hop_penalty) so a plan with hops
only outranks a comparable no-hop plan when it is cheaper by more than the penalty.
"""
from datetime import date
from itertools import product
from typing import Dict, List, Optional

from .config import Config, Dates
from .searches import Leg, Search, home_ok, one_way, round_trip, shift

PLAN_TYPES = {
    "rt_side": "Round trip + side trip",
    "open_jaw": "Open jaw (fly home from honeymoon)",
    "multicity": "Multi-city business ticket",
    "wedding": "Wedding only (no honeymoon)",
}


VIETNAM_AIRPORTS = {"SGN", "DAD", "HAN", "PQC", "CXR", "HUI", "VCA", "DLI", "UIH", "VII", "HPH", "VDO",
                    "THD", "VCL", "TBB", "PXU", "BMV", "DIN", "VKG", "CAH", "VDH"}

# Plans are only "comparable" for hop-vs-no-hop within a family (open jaw and multi-city are the same route).
FAMILIES = {"rt_side": "round_trip", "open_jaw": "one_way", "multicity": "one_way", "wedding": "wedding"}


def _hop_penalty(cfg: Config, hops: List[dict]) -> float:
    return min(sum(h["penalty"] for h in hops), cfg.hop_penalty.max_usd)


def plan_end_date(cfg: Config, plan: dict) -> str:
    """The honeymoon_end this plan was built for (wedding-only plans derive it from the home date)."""
    return plan["dates"].get("honeymoon_end") or shift(plan["dates"]["home"], -cfg.dates.home_after_return_days)


def compute_plans(cfg: Config, latest: Dict[str, dict], dates: Optional[Dates] = None) -> List[dict]:
    """Every plan priced from `latest`. `dates` overrides cfg.dates (flex grid); see build_searches."""
    d = dates or cfg.dates
    home, city = cfg.home, cfg.wedding_city
    lh, rg, p = cfg.cabins.long_haul, cfg.cabins.regional, cfg.providers.default
    plans: List[dict] = []

    def component(s: Search, role: str, hop: Optional[str] = None) -> dict:
        q = latest.get(s.key)
        return {
            "key": s.key,
            "role": role,
            "hop": hop,   # None, or connector | home_hop | via
            "origin": s.legs[0].origin,
            "dest": s.legs[0].dest,
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
        hops = [{
            "type": c["hop"], "role": c["role"], "price": c["price"],
            "vietnam": c["origin"] in VIETNAM_AIRPORTS and c["dest"] in VIETNAM_AIRPORTS,
            "penalty": cfg.hop_penalty.vietnam_usd if c["origin"] in VIETNAM_AIRPORTS and c["dest"] in VIETNAM_AIRPORTS
            else cfg.hop_penalty.max_usd,
        } for c in comps if c["hop"]]
        penalty = _hop_penalty(cfg, hops)
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
            "hops": hops,
            "hop_count": len(hops),
            "hop_label": "hop" if hops else "direct",
            "hop_penalty": penalty,
            "rank_score": total + penalty if total is not None else None,
            "hop_delta": None,   # filled in below: what the hops save vs. the comparable no-hop plan
            "hop_note": None,
        })

    def connector_in(g: str, out_day: str) -> List[dict]:
        if g == city:
            return []
        return [component(one_way("connector", rg, p, g, city, shift(out_day, d.connector_offset_days)),
                          f"{g} → {city} hop", "connector")]

    for out_day, g in product(d.outbound, cfg.gateways):
        for he in d.honeymoon_end:
            back = shift(he, d.home_after_return_days)
            if not home_ok(cfg, back):
                continue
            rt = component(round_trip("longhaul_rt", lh, p, home, g, out_day, back),
                           f"{home} ⇄ {g} round trip")
            conn_back = [] if g == city else [
                component(one_way("connector", rg, p, city, g, back), f"{city} → {g} hop", "connector")]
            add("wedding", None, g, {"outbound": out_day, "home": back},
                [rt] + connector_in(g, out_day) + conn_back)

        for dest in cfg.honeymoon:
            starts = dest.start_dates or d.honeymoon_start
            ends = d.honeymoon_end if dates else dest.end_dates or d.honeymoon_end
            for ap in dest.airports:
                start = dest.fly_out_via or city
                hub = dest.fly_home_from or ap
                for hs, he in product(starts, ends):
                    dates = {
                        "outbound": out_day, "honeymoon_start": hs, "honeymoon_end": he, "airport": ap,
                    }
                    via_out = [] if start == city else [
                        component(one_way("connector", rg, p, city, start, hs), f"{city} → {start} hop", "via")]
                    to_honeymoon = component(one_way("honeymoon_ow", rg, p, start, ap, hs),
                                             f"{start} → {ap}")

                    if not dest.extended_stay and home_ok(cfg, shift(he, d.home_after_return_days)):
                        back = shift(he, d.home_after_return_days)
                        rt = component(round_trip("longhaul_rt", lh, p, home, g, out_day, back),
                                       f"{home} ⇄ {g} round trip")
                        conn_back = [] if g == city else [
                            component(one_way("connector", rg, p, city, g, back), f"{city} → {g} hop",
                                      "connector")]
                        via_back = [] if start == city else [
                            component(one_way("connector", rg, p, start, city, he),
                                      f"{start} → {city} hop", "via")]
                        side = _cheaper(
                            [component(round_trip("honeymoon_rt", rg, p, start, ap, hs, he),
                                       f"{start} ⇄ {ap} honeymoon")],
                            [to_honeymoon, component(one_way("honeymoon_back", rg, p, ap, start, he),
                                                     f"{ap} → {start}")],
                        )
                        add("rt_side", dest, g, {**dates, "home": back},
                            [rt] + connector_in(g, out_day) + via_out + side + via_back + conn_back)

                    if not home_ok(cfg, he):
                        continue
                    hop = [] if hub == ap else [
                        component(one_way("home_hop", rg, p, ap, hub, he), f"{ap} → {hub} hop", "home_hop")]
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

    _note_hop_savings(plans)
    plans.sort(key=lambda pl: (pl["rank_score"] is None, pl["rank_score"] or 0, pl["hop_count"], pl["total"] or 0))
    return plans


def _note_hop_savings(plans: List[dict]) -> None:
    """Tag each priced hop plan with what its hops save/cost vs. the cheapest comparable no-hop plan
    (same destination, plan family, airport and dates)."""
    def group(pl):
        return (pl["destination_id"], FAMILIES[pl["type"]], tuple(sorted(pl["dates"].items())))

    direct: Dict[tuple, int] = {}
    for pl in plans:
        if pl["total"] is not None and pl["hop_count"] == 0:
            k = group(pl)
            direct[k] = min(direct.get(k, pl["total"]), pl["total"])
    for pl in plans:
        if pl["total"] is None or pl["hop_count"] == 0 or group(pl) not in direct:
            continue
        saved = direct[group(pl)] - pl["total"]
        pl["hop_delta"] = saved
        pl["hop_note"] = f"hop saves ${saved:,}" if saved > 0 else f"hop costs ${-saved:,} more"


def _cheaper(a: List[dict], b: List[dict]) -> List[dict]:
    """Pick the fully-priced option with the lower total (round trip vs. two one-ways)."""
    def total(cs):
        return sum(c["price"] for c in cs) if all(c["price"] is not None for c in cs) else None
    ta, tb = total(a), total(b)
    if ta is None:
        return b if tb is not None else a
    return b if tb is not None and tb < ta else a


def _rank(pl: dict):
    return pl.get("rank_score", pl["total"])


def best_by(plans: List[dict], *fields: str) -> Dict[tuple, dict]:
    """Best priced plan per group by rank_score (total + hop penalty); displayed prices stay real totals."""
    best: Dict[tuple, dict] = {}
    for pl in plans:
        if pl["total"] is None:
            continue
        k = tuple(pl[f] for f in fields)
        if k not in best or _rank(pl) < _rank(best[k]):
            best[k] = pl
    return best


def cheapest(plans: List[dict]) -> Optional[dict]:
    """First priced plan; plans from compute_plans are sorted by rank_score, so this is the best-ranked one."""
    return next((pl for pl in plans if pl["total"] is not None), None)


# --- flexible dates ------------------------------------------------------------------------------

def flex_candidates(cfg: Config, plans: List[dict], destination_id: str) -> List[dict]:
    """The cheapest few plan shapes (type + gateway + airport) for a destination, from plans priced on the
    core dates and already sorted by rank_score. Only these get the flex-date grid."""
    out, seen = [], set()
    for pl in plans:
        shape = (pl["type"], pl["gateway"], pl["airport"])
        if pl["destination_id"] != destination_id or pl["total"] is None or shape in seen:
            continue
        seen.add(shape)
        out.append(pl)
        if len(out) >= cfg.flex.top_candidates:
            break
    return out


def _slim(pl: dict, core: bool, best_total: Optional[int] = None) -> dict:
    return {
        "id": pl["id"], "type": pl["type"], "type_label": pl["type_label"], "gateway": pl["gateway"],
        "airport": pl["airport"], "dates": pl["dates"], "total": pl["total"], "per_person": pl["per_person"],
        "rank_score": pl["rank_score"], "hop_count": pl["hop_count"], "hop_label": pl["hop_label"],
        "hop_note": pl["hop_note"], "core": core,
        **({"delta": pl["total"] - best_total} if best_total is not None else {}),
    }


def alternatives(cfg: Config, plans: List[dict], destination_id: str) -> dict:
    """Date matrix, top-5 alternatives and the near-dates flag for one destination.
    `plans` come from compute_plans(cfg, latest, dates=flex_dates(cfg)), so unpriced dates are just null."""
    dest = next((x for x in cfg.honeymoon if x.id == destination_id), None)
    core_out = set(cfg.dates.outbound)
    core_end = set(dest.end_dates if dest and dest.end_dates else cfg.dates.honeymoon_end)
    mine = [pl for pl in plans if pl["destination_id"] == destination_id]
    priced = [pl for pl in mine if pl["total"] is not None]   # already sorted by rank_score

    def is_core(pl):
        return pl["dates"]["outbound"] in core_out and plan_end_date(cfg, pl) in core_end

    def within_a_day(day, core):
        return any(abs((date.fromisoformat(day) - date.fromisoformat(c)).days) <= 1 for c in core)

    def near_core(pl):
        return within_a_day(pl["dates"]["outbound"], core_out) and within_a_day(plan_end_date(cfg, pl), core_end)

    outbound = sorted({pl["dates"]["outbound"] for pl in mine})
    ends = sorted({plan_end_date(cfg, pl) for pl in mine})
    cell_for: Dict[tuple, dict] = {}
    for pl in priced:
        cell_for.setdefault((pl["dates"]["outbound"], plan_end_date(cfg, pl)), pl)
    best = priced[0] if priced else None
    rows = []
    for out_day in outbound:
        cells = []
        for end in ends:
            pl = cell_for.get((out_day, end))
            cells.append(pl and {
                "total": pl["total"], "type": pl["type"], "type_label": pl["type_label"],
                "gateway": pl["gateway"], "airport": pl["airport"], "home": pl["dates"].get("home") or end,
                "hop_count": pl["hop_count"], "plan_id": pl["id"], "core": is_core(pl),
                "best": pl is best,
            })
        rows.append({"outbound": out_day, "core": out_day in core_out, "cells": cells})

    core_best = next((pl for pl in priced if is_core(pl)), None)
    near_best = next((pl for pl in priced if not is_core(pl) and near_core(pl)), None)
    flag = bool(core_best and near_best and near_best["rank_score"] < core_best["rank_score"]
                and near_best["total"] < core_best["total"])
    return {
        "destination_id": destination_id,
        "matrix": {"outbound": outbound, "ends": ends, "core_outbound": sorted(core_out),
                   "core_ends": sorted(core_end), "rows": rows},
        "top": [_slim(pl, is_core(pl), best["total"]) for pl in priced[:5]],
        "near_dates": {
            "flag": flag,
            "core_best": core_best and _slim(core_best, True),
            "near_best": near_best and _slim(near_best, False),
            "savings": core_best["total"] - near_best["total"] if flag else 0,
        },
        "priced": len(priced), "plans": len(mine),
    }
