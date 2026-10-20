# Wedding Flight Tracker

Self-hosted tracker for **2 business-class tickets DFW → Da Nang (DAD)** for an early-March wedding, plus a mini honeymoon. It keeps pricing every route on a schedule, stores the history in MongoDB, and shows the cheapest complete trip for each honeymoon spot in a web dashboard.

## What it compares

For each honeymoon destination, each gateway (`DAD` direct, or `SGN` + a cheap hop to Da Nang) and each date combination, it builds full trip plans:

| Plan | Flights |
|---|---|
| **Round trip + side trip** | DFW⇄gateway business round trip, DAD⇄honeymoon economy (round trip or two one-ways, whichever is cheaper), home from Da Nang |
| **Open jaw** | DFW→gateway business, DAD→honeymoon economy, honeymoon→DFW business. You don't go back to Vietnam, so a single-entry e-visa is enough |
| **Multi-city** *(SerpApi only)* | Same route as open jaw, but both business legs on one ticket. Often cheaper than two one-ways |
| **Wedding only** | Baseline with no honeymoon, so you can see what the honeymoon adds |

Default destinations: Tokyo, Osaka/Kyoto, Phu Quoc, Phuket, Koh Samui, Boracay, Palawan and the Maldives. The **Honeymoon ideas** tab covers March weather, how to get there from DAD and entry notes for each. Add or remove destinations in `config.yaml`.

All prices are **totals for both travelers** (that's what Google Flights returns when you search for 2 adults). The dashboard also shows the per-person price.

## Run it on the homelab

```bash
cp .env.example .env        # set MONGO_URI to your Mongo server
# edit config.yaml: your real dates, destinations, alerts
docker compose up -d --build
```

Then open `http://<homelab-ip>:8085`. The first refresh starts 5 seconds after boot and takes about 6–8 minutes, because the scraper waits between requests so Google doesn't block it. After that it refreshes every `schedule.interval_hours` (default 12).

No Mongo yet? Use the bundled one:

```bash
docker compose --profile with-mongo up -d --build   # and set MONGO_URI=mongodb://mongo:27017
```

Data goes in the `wedding_flights` database, in the collections `quotes`, `plan_history` and `runs`.

### Deployment

GitHub Actions builds and publishes the image to GHCR. Every push to `main` publishes `ghcr.io/jozifsoriano/wedding-flight-tracker:latest`, and every `v*` tag (e.g. `v1.2.0`) publishes a versioned image (`:1.2.0`). To run a published image instead of building locally, set `TAG` and pull:

```bash
TAG=latest docker compose pull flight-tracker && TAG=latest docker compose up -d
```

## Configuration (`config.yaml`)

- **`dates`**: `outbound` (leave DFW), `honeymoon_start`, `honeymoon_end`. Every combination is searched, so keep each list to 1–3 dates.
- **`gateways`**: `[DAD, SGN]` compares flying into Da Nang against flying into Saigon and hopping up.
- **`cabins`**: `long_haul: business`, `regional: economy`.
- **`honeymoon[].fly_home_from`**: island airports such as PQC, MPH, PPS and USM have no business fares to DFW. For those, open-jaw plans add an economy hop to a hub (SGN/MNL/BKK) and fly business home from there.
- **`honeymoon[].fly_out_via`**: Google can't price Da Nang→Malé as one ticket, so the Maldives is priced via SGN.
- **`alerts`**: set `ntfy_url` and/or `discord_webhook` to get a push when a destination hits a new low (`min_drop_pct`), or when any full trip drops under `target_total`.

After editing, click **Searches → Reload config.yaml** in the UI. Schedule changes take effect from the next run.

## Data sources

- **`google`** (default, free): scrapes Google Flights with the [`fast-flights`](https://github.com/AWeirdDev/flights) library, directly from your homelab IP. It can't price multi-city tickets. If Google changes its page markup the scraper can break until the library is updated (`pip` pin in `requirements.txt`).
- **`serpapi`** (optional, paid): put `SERPAPI_KEY` in `.env` to turn on true multi-city business tickets (one set of searches per gateway × honeymoon hub). `serpapi_monthly_budget` caps usage. The free tier (~250/month) is enough for multi-city searches at a 12h interval with the default dates.

## Notes

- Google Flights prices change constantly and include taxes. Always confirm on the airline's site before you book. Each price in the UI links to that exact search on Google Flights.
- Award or points bookings (e.g. AAdvantage/JAL business to Tokyo) aren't tracked, but they're worth checking for the long-haul legs.
- Entry rules in the destination notes are general guidance as of 2026. Check them before booking. In particular, if you leave Vietnam for the honeymoon and come back, you need a **multiple-entry** Vietnam e-visa.

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
MONGO_URI=mongodb://localhost:27017 CONFIG_PATH=./config.yaml .venv/bin/uvicorn app.main:app --reload --port 8085
```

## Running tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests are hermetic: MongoDB is replaced with `mongomock`, and Google Flights (fast-flights), SerpApi and the ntfy/Discord alerts are all faked, so you don't need a network connection, a database or any API keys.
