# Migration Plan: Explore-only → Hybrid (Explore discovery + google_flights pricing)

**Status:** proposed — no app code changed yet.
**Author context:** follows the API probing in `data/explore_probe/` (see [src/explore_probe.py](src/explore_probe.py)).

---

## 1. Why we're migrating

The app is built on a single SerpApi engine, `google_travel_explore` (`type=1`,
roundtrip), called once per window to return *all* destinations. Live probing of
RMO (Chișinău) showed this does not work for our use case:

| Test | Engine / params | Result |
|---|---|---|
| Roundtrip Explore, all 6 weekends | `google_travel_explore` `type=1` | **0 destinations for every weekend** |
| One-way Explore | `google_travel_explore` `type=2` (no return) | 65 destinations, but only **3 of 6** outbound dates |
| Roundtrip Explore, no return date | `google_travel_explore` `type=1` | **Hard API error** — `return_date` is required |
| Per-route roundtrip | `google_flights` `type=1` | **Clean roundtrip itineraries with prices** ✓ |

Two findings drive the new design:

1. **`google_travel_explore` cannot deliver roundtrip pricing for RMO.** Its
   roundtrip mode returns empty; only its one-way mode returns anything, and only
   for some dates. It is a *discovery* engine ("show me anywhere from X"), not a
   reliable pricing engine for a sparse airport.
2. **Empties are transient / non-deterministic, not a parameter rule.** For RMO→ATH
   on `google_flights`, `stops=Any` returned **empty** while `stops≤1` returned **3
   itineraries** on the same weekend — logically impossible for a stable backend
   (Any ⊇ ≤1-stop). The Sunday-vs-Monday and stops-on/off patterns were all noise
   on a flaky upstream. **The fix is retry, not a magic parameter.**

When `google_flights` returns, the payload is exactly what we need:

```
price            : 496   (USD, total for 2 pax)   ← true roundtrip price
total_duration   : 535 min
flights (legs)   : RMO→VIE (Austrian OS718), VIE→ATH (Austrian OS805)
layovers         : [VIE, 310 min]
price_insights.lowest_price : 496
```

---

## 2. Target architecture

A two-phase hybrid. Each engine does the job it's actually good at.

```
                ┌─────────────────────────────────────────────────────────┐
                │  PHASE 1 — DISCOVERY (cheap, 1 call per Friday)           │
                │  google_travel_explore  type=2 (one-way, no return)      │
                │  → list of reachable destinations + one-way price        │
                └─────────────────────────────────────────────────────────┘
                                      │  rank by one-way price, take top-N
                                      ▼
                ┌─────────────────────────────────────────────────────────┐
                │  SHORTLIST  (curated ∪ discovered), capped at S dests     │
                └─────────────────────────────────────────────────────────┘
                                      │  for each window × destination
                                      ▼
                ┌─────────────────────────────────────────────────────────┐
                │  PHASE 2 — PRICING (per route, with retry-on-empty)      │
                │  google_flights  type=1 roundtrip, NO stops param        │
                │  → roundtrip itineraries; filter ≤MAX_STOPS in code      │
                └─────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                            insert_flights() → SQLite
```

### Key design decisions

- **D1 — Two engines, two roles.** Explore one-way = *discovery* (which cities are
  reachable + a price signal for ranking). google_flights = *pricing* (the actual
  roundtrip number we store and display).
- **D2 — Drop the `stops` param from all API calls.** The app already enforces
  `MAX_STOPS` (=1) inside `parse_*`. Sending `stops` to the API only over-filters
  and amplifies the transient empties (it caused the RMO→ATH contradiction). Fetch
  the full result set; filter stops client-side.
- **D3 — Retry-on-empty.** Treat an empty/`"hasn't returned any results"` response
  as transient: retry the same query up to `R` times (with the existing 2 s spacing)
  before concluding the route is genuinely empty for that window.
- **D4 — One call per roundtrip price.** SerpApi's `google_flights` roundtrip first
  call returns *outbound* itineraries, each carrying the **total roundtrip price**
  (and `price_insights.lowest_price`). That single call is enough for the cheapest
  roundtrip price + outbound legs. Exact *return-leg* details would need a second
  `departure_token` call — **out of scope** for v1 (we don't display return legs).
- **D5 — Keep the hard-won robustness from the current `do_refresh`.** No `st.*`
  calls inside the fetch loop (so sidebar clicks can't cancel a refresh), near-term
  skip (`NEAR_TERM_SKIP_DAYS`), 2 s throttle, debug logging, always-log-the-fetch.

---

## 3. Call-count math

This is the biggest consequence of the migration and must be sized deliberately.

**Today:** 1 call/window × 24 windows (6 Fridays × 4 window types) = **24 calls/refresh.**

**Hybrid:** `discovery + (effective_windows × shortlist_size × retry_factor)`.

Definitions:
- `effective_windows` — 24 minus the nearest Friday's 4 windows dropped by the
  near-term skip ≈ **20**.
- `S` — shortlist size (destinations priced per window).
- `discovery` — 1 one-way Explore call per Friday, reused across that Friday's 4
  window types = **6 calls**.
- `retry_factor` — expected attempts per pricing call given a ~25 % transient-empty
  rate and up to 2 retries: `1 + 0.25 + 0.25² ≈ 1.31`.

Sensitivity:

| Scenario | `S` | eff. windows | base pricing | ×retry (~1.31) | + discovery | **total/refresh** |
|---|---:|---:|---:|---:|---:|---:|
| Lean | 15 | 20 | 300 | ~393 | +6 | **~400** |
| **Recommended** | **20** | **20** | **400** | **~525** | **+6** | **~530** |
| Wide | 30 | 20 | 600 | ~786 | +6 | **~790** |
| 2 window types only (`fri_sun`,`thu_mon`) | 20 | 10 | 200 | ~262 | +6 | **~270** |

**Wall-clock** at the current 2 s throttle (serial): ~530 calls × 2 s ≈ **~18 min**
per full refresh. Mitigations: run discovery + pricing concurrently within the
SerpApi plan's concurrency limit (cuts wall time, not call count); reduce `S`;
reduce priced window types; only price destinations whose one-way price suggests a
roundtrip under `PRICE_CAP`.

**Quota** (recommended `S=20`, ~530/refresh): weekly ≈ **~2.1k/mo**, daily ≈ **~16k/mo**.
Confirm the SerpApi plan tier before choosing cadence. **Cost-control levers, in order
of impact:** (a) cap `S`; (b) price only top-N cheapest from discovery; (c) drop
low-priority window types; (d) incremental refresh (skip routes priced in the last
N hours).

> **Decision needed:** target `S`, which window types to price, and refresh cadence.
> The recommended starting point is `S=20`, all 4 window types, **weekly** refresh.

---

## 4. Changes by file

### 4.1 `src/config.py` (supporting — constants live here)

Add:

```python
# Pricing engine
GOOGLE_FLIGHTS_ENGINE = "google_flights"
EXPLORE_ENGINE        = "google_travel_explore"

# Hybrid tuning
DESTINATION_SHORTLIST_SIZE = 20      # S — roundtrip-priced destinations per window
RETRY_ON_EMPTY             = 2       # extra attempts after a transient empty
PRICE_WINDOW_TYPES         = [w["label"] for w in WINDOW_TYPES]  # or a subset

# Curated seed destinations (IATA) always priced regardless of discovery.
# Discovery augments this up to DESTINATION_SHORTLIST_SIZE.
CURATED_DESTINATIONS = ["ATH", "LIS", "DUB", "LTN", "BVA", "FCO", "BCN", "VIE"]
```

`stops` is **removed** from API params (decision D2) — `MAX_STOPS` stays and is
applied client-side.

### 4.2 `src/fetcher.py`

**Repurpose / add:**

| Function | Change |
|---|---|
| `fetch_explore(outbound, return_date)` | **Repurpose → `discover_destinations(outbound_date)`**: `type=2`, no `return_date`, **no `stops`**. Returns raw. (Keep `fetch_explore` as a thin alias during transition if convenient.) |
| `parse_explore_results(...)` | Keep, but it now parses **one-way discovery** output into *candidate* dicts `{iata, city, country, oneway_price, stops, airline}`. Used for ranking/shortlisting, **not** for the `flights` table. |
| **NEW** `fetch_flights(outbound_date, return_date, arrival_id)` | `google_flights` engine, `type=1`, `departure_id=RMO`, `arrival_id=<iata>`, `adults=2`, **no `stops`**. Returns raw. |
| **NEW** `parse_flights_results(raw, fri_date, window_label, iata, city, country, fetched_at)` | Map `best_flights`/`other_flights` → `flights` row dicts (see §5). Apply `PRICE_CAP` and `MAX_STOPS` here. Returns the **cheapest qualifying** row (or `[]`). |
| **NEW** `flights_result_status(raw)` | Generalize `explore_result_status`: `('error'\|'empty'\|'ok', n)` where `n = len(best_flights)+len(other_flights)`; `empty` when both lists are empty / "hasn't returned any results". |
| **NEW** `fetch_with_retry(call, classify, attempts, sleep_s, debug, tag)` | Calls `call()`, classifies; on `empty` retries up to `attempts` (with `sleep_s` spacing); on `error` returns immediately (errors are not transient the way empties are). Returns `(raw, status, n_attempts)`. |
| `explore_result_status(raw)` | Keep for discovery (`destinations` empty vs error vs ok). |
| `is_near_term`, `NEAR_TERM_SKIP_DAYS` | Unchanged. |

**Stops mapping** (google_flights → our schema): `outbound_stops = len(itinerary["layovers"])`
(equivalently `len(itinerary["flights"]) - 1`). Reject if `> MAX_STOPS`.

### 4.3 `src/db.py`

**Schema changes to the `flights` table:**

| Column | Change | Reason |
|---|---|---|
| `return_stops` | `INTEGER NOT NULL` → **nullable** | v1 single-call pricing has no return-leg detail (D4). |
| `return_duration_min` | already nullable | no change. |
| `booking_link` | already nullable | google_flights gives no per-itinerary URL; store a constructed Google Flights search URL **or** `NULL`. |
| **NEW** `price_source TEXT` | add | `'google_flights'` vs legacy `'explore'`; lets old/new rows coexist. |
| **NEW** `departure_token TEXT` (optional) | add | enables a future return-leg detail call. |
| **NEW** `oneway_price_usd REAL` (optional) | add | discovery price signal, for analytics. |

**`fetch_log`** — optional new columns `discovery_calls`, `pricing_calls`, `retries`
(INTEGER). Otherwise fold counts into the existing `notes` text.

**Migration mechanics:** `init_db()` uses `CREATE TABLE IF NOT EXISTS`, so it won't
alter an existing table, and SQLite can't drop a `NOT NULL` constraint in place. The
DB is **disposable and reproducible** (not git-tracked; raw JSON gitignored), so the
recommended path is:

1. Bump a `SCHEMA_VERSION` constant.
2. On mismatch, **rebuild** `flights` (create `flights_new` with the new schema,
   copy compatible columns, drop, rename) — or, simplest for a personal app,
   **delete `data/flights.db` and re-fetch.** Document whichever is chosen.

Query helpers (`get_flights_for_window`, `get_cheapest_per_weekend`,
`get_price_history`) need **no logic change** — same columns consumed.

### 4.4 `src/dashboard.py`

`do_refresh(debug)` becomes two-phase. It **keeps** the no-`st.*`-in-loop property,
near-term skip, 2 s throttle, debug `SKIP`/`OK` logging, and always-log-the-fetch.

```text
do_refresh(debug):
    today, fetched_at, windows = ... (as today)
    # PHASE 1 — discovery (1 call per Friday, reused across its window types)
    shortlist_by_friday = {}
    for fri in distinct_fridays(windows) if not near_term(fri):
        raw = fetch_with_retry(lambda: discover_destinations(fri_outbound), ...)
        cands = parse_explore_results(raw, ...)               # one-way candidates
        ranked = sort(cands, key=oneway_price)
        shortlist = dedupe(CURATED_DESTINATIONS + [c.iata for c in ranked])[:S]
        shortlist_by_friday[fri] = shortlist
        # count discovery_calls / errors / empties; debug-log

    # PHASE 2 — pricing (per window × destination, with retry)
    all_rows = []
    for w in windows:
        if near_term(w.outbound): skip; continue
        if w.label not in PRICE_WINDOW_TYPES: skip; continue
        for iata in shortlist_by_friday[w.fri_date]:
            throttle()
            raw, status, n = fetch_with_retry(
                lambda: fetch_flights(w.outbound, w.return, iata),
                classify=flights_result_status, attempts=RETRY_ON_EMPTY, ...)
            if status == 'error': errors++; note; continue
            if status == 'empty': empties++; continue
            rows = parse_flights_results(raw, w.fri_date, w.label, iata, ...)
            all_rows += rows
            retries += (n - 1)

    persist(all_rows); log_fetch({..., calls_made, status, notes})
    return {rows, discovery_calls, pricing_calls, errors, empty, skipped, retries, warnings}
```

- `_build_refresh_msg(result)` — extend to surface discovery vs pricing counts and
  retries, e.g. *"Fetched 180 roundtrip prices · 6 discovery + 412 pricing calls
  (38 retries, 9 empty, 2 errors)."*
- **Imports** — add `discover_destinations`, `fetch_flights`, `parse_flights_results`,
  `flights_result_status`, `fetch_with_retry` from `src.fetcher`; add
  `DESTINATION_SHORTLIST_SIZE`, `RETRY_ON_EMPTY`, `CURATED_DESTINATIONS`,
  `PRICE_WINDOW_TYPES` from `src.config`.
- **UX** — keep the single `st.spinner`. With ~530 calls / ~18 min, the spinner
  message should set expectations (e.g. *"Fetching roundtrip prices — this can take
  several minutes"*). Optionally add an ETA derived from `pending_calls × 2 s`.

The **partial-data Overview surfacing** added earlier
(`windows_with_data_for_friday`) and all chart tabs are **unaffected** — they read
the same `flights` columns.

---

## 5. Field mapping reference

### google_flights itinerary → `flights` row

| `flights` column | Source |
|---|---|
| `price_usd_2pax` | `itinerary["price"]` (total RT for `adults=2`); cheapest qualifying itinerary, or `price_insights["lowest_price"]` |
| `outbound_stops` | `len(itinerary["layovers"])` |
| `outbound_duration_min` | `itinerary["total_duration"]` |
| `airline` | from `itinerary["flights"][*]["airline"]` (single carrier → that name; multi → first marketing carrier or joined) |
| `destination_iata` / `_city` / `_country` | carried from the shortlist entry (Explore discovery) |
| `outbound_date` / `return_date` / `window_*` | from the window `w` |
| `return_stops` / `return_duration_min` | `NULL` in v1 (D4) |
| `booking_link` | constructed Google Flights URL, or `NULL` |
| `price_source` | `"google_flights"` |
| `departure_token` (optional) | `itinerary["departure_token"]` |

### Explore one-way `destinations[]` → shortlist candidate

| Candidate field | Source |
|---|---|
| `iata` | `destination_airport["code"]` |
| `city` | `name` |
| `country` | `country` |
| `oneway_price` | `flight_price` (ranking signal only) |
| `stops` / `airline` / `duration` | `number_of_stops` / `airline` / `flight_duration` |

---

## 6. Rollout (incremental, low-risk)

1. **Add alongside, don't replace.** Land `fetch_flights` + `parse_flights_results`
   + `fetch_with_retry` and unit-test them against the saved fixtures in
   `data/explore_probe/` (no live calls). Keep the existing Explore path intact.
2. **Schema bump + DB rebuild** behind `SCHEMA_VERSION`.
3. **Swap `do_refresh` to two-phase** behind a flag (e.g. `USE_GOOGLE_FLIGHTS`),
   defaulting to the new path once validated; keep the old path as fallback for one
   release.
4. **Tune live** with small `S` (e.g. 5) and one Friday to validate end-to-end and
   real empty-rate, then raise `S` to target.
5. Remove the legacy Explore-roundtrip path and the flag.

---

## 7. Risks & mitigations

| Risk | Mitigation |
|---|---|
| **Quota / cost blowup** (~22× calls) | Cap `S`; price top-N from discovery; fewer window types; weekly cadence; incremental refresh. |
| **~18 min serial runtime** | Concurrency within plan limits; ETA in spinner; background/scheduled refresh rather than interactive. |
| **Transient empties** | `fetch_with_retry` (D3); log empty-rate to tune `RETRY_ON_EMPTY`. |
| **Discovery itself returns empty for a Friday** (seen for 3/6 one-way dates) | Fall back to `CURATED_DESTINATIONS` so pricing still runs for that weekend. |
| **No return-leg detail in v1** | Acceptable — not displayed. `departure_token` stored to enable later. |
| **No booking URL per itinerary** | Construct a Google Flights search URL from route+dates, or leave `NULL`. |
| **Schema migration on a live DB** | DB is reproducible; rebuild or delete-and-refetch, documented. |

---

## 8. Testing

- **Parser unit tests** against saved JSON in `data/explore_probe/`
  (`gf_*.json`, `testA_*`): assert `parse_flights_results` extracts price/stops/
  duration/airline and applies `PRICE_CAP`/`MAX_STOPS`; assert `flights_result_status`
  classifies the empty (`gf_jul03*` error payload) vs populated samples.
- **Retry test**: stub a call that returns empty twice then data; assert
  `fetch_with_retry` makes 3 attempts and returns the data.
- **`do_refresh` mock test** (as already done): patch `discover_destinations` /
  `fetch_flights` / `time.sleep` / DB writers; assert two-phase counts, near-term
  skips, error/empty skips, and run-to-completion.
- **Live smoke**: one Friday, `S=5`, `debug=True`; eyeball `SKIP`/`OK`/retry logs.

---

## 9. Open decisions

1. **`S`, priced window types, cadence** — recommend `S=20`, all 4 types, weekly.
2. **Curated vs discovered balance** — seed list contents; whether discovery may
   exceed the curated set or only fill remaining slots.
3. **`booking_link`** — construct a Google Flights URL or store `NULL`.
4. **Migration style** — in-place table rebuild vs delete-and-refetch.
5. **Concurrency** — keep serial 2 s throttle, or parallelize to cut the ~18 min?
