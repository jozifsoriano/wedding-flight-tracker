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

Default destinations: Tokyo, Osaka/Kyoto, Phu Quoc, Phuket, Koh Samui, Boracay, Palawan, the Maldives, Seoul and Saigon. Seoul and Saigon are tracked as extended stays leaving Da Nang on March 15 and flying home March 19. The **Honeymoon ideas** tab covers destination notes. Add destinations from the **Add a city** tab; GUI additions are persisted in `user-data/user_destinations.yaml` and use their own dates.

For extended stays, the **All plans** tab compares separate DFW→wedding-city and destination→DFW tickets against those long-haul legs on one multi-city ticket. Both totals include the separately priced wedding-city→destination hop. Multi-city pricing requires `SERPAPI_KEY`; separate-ticket estimates are still tracked without it.

All prices are **totals for both travelers** (that's what Google Flights returns when you search for 2 adults). The dashboard also shows the per-person price.

### Avoiding hops

Every plan records its `hops` (the gateway hop, e.g. SGN→DAD; the hop from a honeymoon island to a `fly_home_from` hub; `fly_out_via` hops), a `hop_count`, and a `hop_label` of `direct` or `hop`. `total` is always the real price. To keep the trip simple, each hop plan also gets a `rank_score = total + penalty`, and plans are sorted and the "best" trip is picked by `rank_score`. A hop within Vietnam (SGN↔DAD, PQC↔SGN) adds `hop_penalty.vietnam_usd`, any other hop adds `hop_penalty.max_usd`; a plan's total penalty is capped at `max_usd`. So a hop plan only outranks the comparable no-hop plan when it is cheaper by **more** than the penalty (ties go to no-hop). Hop plans stay listed with a "hop saves $X" / "hop costs $X more" tag against the same itinerary and dates without the hop (for example, one-way business straight to DAD vs. via SGN). The dashboard has a **No-hop only** toggle (client-side, `hop_count == 0`).

### Flexible dates

The scheduled job only searches the core `dates`, so its search count doesn't change. The wider grid is on demand: **Alternatives & flexible dates → Search flexible dates** on a destination's card in the dashboard, or `POST /api/flex/run?destination_id=<id>`. It takes the destination's `flex.top_candidates` cheapest plan shapes (type + gateway + airport) from the latest core-date prices and prices them over `flex.outbound` × `flex.honeymoon_end` (keeping that plan's honeymoon start). It runs in a background thread like `/api/run` (one run at a time, `409` if busy; `422` if nothing is priced yet or the grid was priced within the last `schedule.interval_hours`), waits `request_delay_seconds` between Google requests, counts against `providers.serpapi_monthly_budget`, and stores quotes like any other run. It does not touch plan history or alerts. Searches already priced within the refresh interval are skipped.

`flex.latest_home_arrival` is enforced everywhere. Round-trip plans fly back to Da Nang on `honeymoon_end` and home `home_after_return_days` later, so with the shipped config a 3/21 `honeymoon_end` only exists for open-jaw and multi-city plans (which fly home that day). Arrival is compared by calendar date; an overnight flight that lands the next day is not accounted for.

`GET /api/alternatives?destination_id=<id>` (use `wedding-only` for the baseline) returns, from the latest quotes over the core and flex dates: a date `matrix` (outbound × honeymoon end, cheapest total and plan type per cell, `null` if unpriced), the `top` 5 plans with `delta` vs. the best, a `near_dates` flag (a cheaper plan exists within ±1 day of your core dates, with the savings), and the last flex run. Both endpoints sit behind the login like `/api/run`.

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

Data goes in the `wedding_flights` database, in the collections `quotes`, `plan_history`, `runs`, `slicktrip_prices` and `actions` (changes Claude made and removals awaiting approval).

### Deployment

GitHub Actions builds and publishes the image to GHCR. Every push to `main` publishes `ghcr.io/jozifsoriano/wedding-flight-tracker:latest`, and every `v*` tag (e.g. `v1.2.0`) publishes a versioned image (`:1.2.0`). To run a published image instead of building locally, set `TAG` and pull:

```bash
TAG=latest docker compose pull flight-tracker && TAG=latest docker compose up -d
```

## Configuration (`config.yaml`)

- **`dates`**: `outbound` (leave DFW), `honeymoon_start`, `honeymoon_end`. Every combination is searched, so keep each list to 1–3 dates. Destination-specific `start_dates` and `end_dates` override the honeymoon dates for that city.
- **`gateways`**: `[DAD, SGN]` compares flying into Da Nang against flying into Saigon and hopping up.
- **`cabins`**: `long_haul: business`, `regional: economy`.
- **`honeymoon[].fly_home_from`**: island airports such as PQC, MPH, PPS and USM have no business fares to DFW. For those, open-jaw plans add an economy hop to a hub (SGN/MNL/BKK) and fly business home from there.
- **`honeymoon[].fly_out_via`**: Google can't price Da Nang→Malé as one ticket, so the Maldives is priced via SGN.
- **`flex`**: flexible dates, priced **on demand only** (see [Flexible dates](#flexible-dates)). `outbound` and `honeymoon_end` list the extra dates to try; `latest_home_arrival` is a hard cap on the date any plan lands back in DFW; `top_candidates` (default 3) is how many of a destination's cheapest plan shapes get the full grid. Leave `outbound`/`honeymoon_end` empty to turn it off.
- **`hop_penalty`**: `max_usd` (default 300) and `vietnam_usd` (default 100), totals for the whole party. See [Avoiding hops](#avoiding-hops).
- **`alerts`**: set `ntfy_url` and/or `discord_webhook` to get a push when a destination hits a new low (`min_drop_pct`), or when any full trip drops under `target_total`.

After editing `config.yaml`, click **Searches → Reload config.yaml** in the UI. GUI-added destinations are saved separately and reloaded immediately. Schedule changes take effect from the next run.

## Data sources

- **`google`** (default, free): scrapes Google Flights with the [`fast-flights`](https://github.com/AWeirdDev/flights) library, directly from your homelab IP. It can't price multi-city tickets. If Google changes its page markup the scraper can break until the library is updated (`pip` pin in `requirements.txt`).
- **`serpapi`** (optional, paid): put `SERPAPI_KEY` in `.env` to turn on true multi-city business tickets (one set of searches per gateway × honeymoon hub). `serpapi_monthly_budget` caps usage. The free tier (~250/month) is enough for multi-city searches at a 12h interval with the default dates.

## Login

Set `APP_PASSWORD_HASH` (and `SESSION_SECRET`) in `.env` to put the whole app behind a password. Generate them with
`docker compose run --rm -it flight-tracker python -m app.auth`, paste the two lines into `.env` in single quotes, and recreate the container.
Sessions are a signed HttpOnly, SameSite=Strict cookie (7 days; `Secure` when served over HTTPS); five wrong passwords from one address lock
it out for five minutes. `/healthz` (Docker healthcheck) and `/mcp` (its own per-question token) are the only paths open without a session.
Without `APP_PASSWORD_HASH` the dashboard stays open as before, but **Ask Claude and Claude's tools stay off**.

## Managing the built-in destinations

Destinations in `config.yaml` are read-only until you click **Move built-in destinations into the app** on the *Add a city* tab. That copies
them, with every field, into `user-data/user_destinations.yaml`, which becomes the source of truth (`config.yaml`'s destination list is then
ignored and the file is never modified). After that you and Claude can edit or remove any of them. To undo it, delete the `migrated: true`
line from that file.

## SlickTrip prices

[SlickTrip](https://mcp.slicktrip.com/mcp) is a second price source next to Google Flights, for the exact itineraries in `tracked_searches` in `config.yaml`: round trip, one-way, or multi-city (open-jaw, or 3 legs). Each search has its own `cabin` (default `business`) and `travelers` (default `2`), and codes may be airports or city codes (`TYOA` = Tokyo). The shipped list is the trip plan: DFW⇄DAD and DFW⇄SGN round trips, one-ways each way, DAD→BKK/Tokyo (business and economy), BKK/Tokyo→DFW on 3/20, and two 3-leg multi-city tickets.

SlickTrip needs an OAuth login that lives on the bridge machine, so **the app never calls SlickTrip and holds no SlickTrip credential**. **Refresh SlickTrip prices** (SlickTrip prices tab, `POST /api/slicktrip/refresh`, or Claude's `refresh_prices` tool with `source: slicktrip`) asks Claude through the bridge, one question per search, to run `search_flights` (with `legs` for multi-city) and answer with one strict JSON object. It is on demand only, not part of the 12-hour schedule, because each search is a Claude question (about a minute each, one at a time) and uses your Claude plan. Those questions get SlickTrip only, never the app's tools.

The answer is treated as untrusted text and parsed defensively (`app/slicktrip.py`): it must be a JSON object for the right search, with the same cabin and traveler count, a finite USD `total_usd` between 0 and 250,000, and a total (not per-person) scope. Anything else is stored as `invalid` with the reason and shown in the tab, never as a price. Only `https://*.slicktrip.com` links are kept; a price that moves more than 50% is kept but flagged. Instructions inside an answer are never followed.

Each result is a document in `slicktrip_prices` with timestamp, `source: slicktrip`, `cabin`, `travelers`, `shape`, `legs`, `total_usd` and the SlickTrip `url`, so history accumulates. **Every SlickTrip price is the total for all travelers on that search (both of you), in USD, not per person.** The tab shows the change vs. the previous price (▼ down / ▲ up), a "lowest seen" badge, and the lowest price so far. `GET /api/slicktrip` returns the same data.

Searches in `config.yaml` are read-only. Searches added in the app (or by Claude's `add_destination` tool with `shape` + `legs`) are stored in `user_searches.yaml` next to `user_destinations.yaml` (override with `GUI_SEARCHES_PATH`), up to 20.

## Ask Claude (Claude Code + SlickTrip MCP)

The **Ask Claude** tab sends questions, plus your cheapest tracked trips, to a small bridge on the homelab VM that has Claude Code
(see [`bridge/README.md`](bridge/README.md)). That VM holds the SlickTrip OAuth login (`https://mcp.slicktrip.com/mcp`), so no SlickTrip
credential lives in this app. Set `BRIDGE_URL` and `BRIDGE_TOKEN` in `.env` for chat. Follow-up questions keep their context through the bridge's `session_id` until you click **New chat**. A busy bridge (it handles one question at a time) is retried with backoff; other failures show a specific message (bad token, client not allowed, Claude error, timeout after about 3 minutes). Prompts and tokens are never logged.

Set `APP_BASE_URL` (how the bridge reaches this app) and Claude can also **manage the tracker** through the app's own MCP endpoint
(`POST /mcp`, streamable HTTP with JSON replies; tools: `list_destinations`, `get_best_trips`, `add_destination`, `update_destination`, `request_remove_destination`,
`refresh_prices`):

- Adds and edits apply immediately and are logged under **Changes by Claude** in the tab.
- **Removals never run on Claude's say-so.** `request_remove_destination` only queues a request; it stays in the tab until you click **Approve** or **Deny**.
- Claude can only change destinations and flight searches the app manages (added in the app, or destinations moved in from `config.yaml` as above), up to 30 destinations and 20 searches. `config.yaml` flight searches are always read-only.
- `/mcp` only accepts a one-question token the app mints when you ask and revokes when the answer returns (it also expires after 5 minutes). Tokens are compared in constant time, the token is checked before the body is read, and bodies over 64 KB are refused. The tools and the Ask tab require the login above.

## Environment variables

Set these in `.env` (see `.env.example`; never commit real values). Defaults are what the code uses when the variable is unset.

| Variable | Used by | Purpose |
|---|---|---|
| `MONGO_URI` | app | MongoDB connection string. Default `mongodb://localhost:27017` |
| `MONGO_DB` | app | Database name. Default `wedding_flights` |
| `SERPAPI_KEY` | app | Optional. Enables SerpApi multi-city searches |
| `APP_PASSWORD_HASH`, `SESSION_SECRET` | app | Optional password login (see [Login](#login)). Without the hash the app is open and the Claude features are off |
| `BRIDGE_URL`, `BRIDGE_TOKEN` | app | Enable the Ask Claude tab and SlickTrip prices (requires login). Older names `CLAUDE_BRIDGE_URL`, `CLAUDE_BRIDGE_TOKEN` still work |
| `APP_BASE_URL` | app | URL the bridge uses to reach this app; enables Claude's `/mcp` tools. Older name `APP_URL` still works |
| `GUI_SEARCHES_PATH` | app | Where app-added SlickTrip searches are stored. Default `user_searches.yaml` next to the destinations file |
| `CONFIG_PATH` | app | Path to `config.yaml`. Default `/config/config.yaml` (set in the Dockerfile) |
| `GUI_DESTINATIONS_PATH` | app | Where GUI-managed destinations are stored. `docker-compose.yml` sets `/data/user_destinations.yaml` |
| `TAG` | compose | Image tag to build/pull. Default `dev` |

The bridge process has its own variables (its `BRIDGE_TOKEN` is the same secret as the app's `BRIDGE_TOKEN`; plus `BRIDGE_ALLOWED_APP_URL`, …); see [`bridge/README.md`](bridge/README.md).
`docker-compose.yml` also sets `TZ: America/Chicago`.

## Architecture

```
config.yaml ─┐                                    ┌─ Google Flights (fast-flights)
user-data/   ├─► searches.py ─► tracker.py ──────►┤
 user_destinations.yaml ─┘      (run_all)          └─ SerpApi (multi-city, optional)
                                   │ quotes
                                   ▼
                               MongoDB ◄── plans.py (cheapest complete trips) ──► alerts.py (ntfy / Discord)
                                   ▲
 browser ──► FastAPI (main.py) ────┘      /mcp ◄── Claude bridge ◄── Ask Claude tab
             + static dashboard (app/static)
```

| Module | Role |
|---|---|
| `app/main.py` | FastAPI app: dashboard API, login middleware, APScheduler job (interval from `schedule.interval_hours`), `/healthz` |
| `app/config.py` | Pydantic models for `config.yaml`; merges in GUI-managed destinations |
| `app/searches.py` | Expands config dates × gateways × destinations into the list of concrete `Search`es (de-duplicated by key) |
| `app/tracker.py` | `run_all`: prices each search (one run at a time, guarded by a lock), stores a quote per search, records plan history, fires alerts |
| `app/providers/` | `google.py` (fast-flights scrape), `serpapi.py` (multi-city), Google Flights deep-link builder |
| `app/plans.py` | Combines the latest successful quotes into plan types and picks the cheapest per destination |
| `app/alerts.py` | ntfy / Discord notifications |
| `app/slicktrip.py` | SlickTrip prices: prompt, defensive JSON parsing, price history, drop/lowest summary, refresh runner |
| `app/auth.py`, `app/assistant.py`, `app/mcp.py`, `app/actions.py` | Password login, bridge client (retry, errors, tool tokens), the `/mcp` tool endpoint, and the log/approval queue for Claude's changes |
| `bridge/` | Separate service (not in the app image) that runs `claude -p` for Ask Claude |

`/api/overview` plans carry `hops`, `hop_count`, `hop_label`, `hop_penalty`, `rank_score` and `hop_note`. `tracker.run_flex` (on demand) shares the pricing loop and lock with `run_all`.

A run: the scheduler (or **Run** in the UI, `POST /api/run`) calls `run_all`. Each search is priced and inserted into `quotes` (failures are stored with an `error` and the run continues). Google searches wait `schedule.request_delay_seconds` between requests, and a failed one is retried once after 30 s. Then plans are rebuilt from the latest successful quote per search, each destination's best total per plan type is appended to `plan_history`, and alerts fire for new lows. The dashboard reads `quotes`/`plan_history` and recomputes plans on request.

## Runbook

- **Health:** `GET /healthz` (also the Docker healthcheck). Logs: `docker compose logs -f flight-tracker`.
- **Prices are empty or stale:** check the log for `search failed` lines and the `error` field on recent `quotes`/`runs` documents. A wave of Google failures usually means the scraper broke on a markup change: update the `fast-flights` pin in `requirements.txt` and rebuild. "No flights found" for a route/date/cabin is normal for some island airports (see `fly_home_from`).
- **A run seems stuck:** only one run happens at a time; `POST /api/run` returns nothing new while one is in progress. Restarting the container clears it.
- **SerpApi budget:** usage is counted from `quotes` documents with `provider: serpapi` this calendar month against `providers.serpapi_monthly_budget`; extra searches fail with "SerpApi monthly budget used up".
- **Config edits:** `config.yaml` is mounted from the host; use **Searches → Reload config.yaml** or restart. Invalid YAML or fields fail Pydantic validation.
- **Locked out / login:** regenerate `APP_PASSWORD_HASH` and `SESSION_SECRET` with `python -m app.auth`, update `.env` and recreate the container. Changing `SESSION_SECRET` signs everyone out.
- **Ask Claude not working:** the tab needs login plus `BRIDGE_URL` and `BRIDGE_TOKEN`; check `curl <bridge>/health` and `bridge/README.md`.
- **Backups:** state lives in the Mongo database and `user-data/user_destinations.yaml` (plus your `config.yaml`). TODO: no backup or retention policy is defined in the repo.

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
