# Moldova Flight Tracker — Planning Doc

## Project Overview
A Python-based flight price tracker and dashboard for weekend trips out of Chisinau, Moldova (RMO). The app maintains a rolling 6-week window of upcoming weekends, always departing from RMO, and logs prices to SQLite so patterns emerge over time. The goal is to find the cheapest roundtrip flights for 2 people and understand booking timing — when is the best time to buy for each destination.

---

## Trip Context
- **Travelers:** 2 people (always together, never solo)
- **Base:** Chisinau, Moldova (RMO)
- **Hard stop:** September 1st — no data collected for any weekend on or after this date
- **Budget target:** Under $400 total for 2 (roundtrip) — ideal
- **Budget ceiling:** Under $1,000 total for 2 (roundtrip) — hard filter, nothing above stored or shown
- **Flag threshold:** Under $500 total for 2 = "good deal" badge
- **Max stops:** 1 (no more than 1 layover each way)
- **Region:** Worldwide — no region filter applied at data collection. Filter by destination in the UI.

---

## Rolling Window Logic

The app always displays the next 6 upcoming **Friday–Sunday** blocks from today's date. Each week, the earliest weekend drops off and a new one is added at the end. The window advances one weekend at a time, always anchored to Friday.

**Example (as of Jun 9, 2026):**
| # | Fri | Sun |
|---|-----|-----|
| 1 | Jun 12 | Jun 14 |
| 2 | Jun 19 | Jun 21 |
| 3 | Jun 26 | Jun 28 |
| 4 | Jul 3  | Jul 5  |
| 5 | Jul 10 | Jul 12 |
| 6 | Jul 17 | Jul 19 |

Next week, Jun 12 drops off and Jul 24 is added, and so on.

**Hard rules:**
- Never collect or display data for any weekend on or after **September 1st**
- **Aug 18–22 (Hungary) is permanently skipped** — no API calls, no display, no data of any kind. This block is hardcoded to be excluded forever regardless of where it falls in the window
- Pre-trip weekends (before July 7) are included for data collection — this builds the price history curve so booking timing patterns are visible by the time the trip starts
- Always depart from RMO regardless of whether the trip has started yet

**Weekend expiry logic:**
- A weekend is considered "past" and drops off the rolling window once its Friday date is in the past (i.e. today > Friday date)
- If today IS the Friday, the weekend is still shown — same-day booking is still possible
- Once it's Saturday, that weekend is gone and the next one rolls in
- The per-window-type logic follows the same Friday anchor: Thu–Sun and Thu–Mon windows expire when their Friday has passed, even though they depart Thursday
- Skip fetching data for any window type whose outbound date has already passed

---

## Weekend Window Types

All four checked for every active weekend in the rolling window:

| Priority | Window | Outbound day | Return day |
|----------|--------|-------------|------------|
| Highest  | Fri–Sun | Friday | Sunday |
| High     | Thu–Sun | Thursday | Sunday |
| High     | Fri–Mon | Friday | Monday |
| Low      | Thu–Mon | Thursday | Monday |

The Fri–Sun block defines which "weekend" a set of searches belongs to (used as the identifier in the DB). The other window types are variants of that same weekend.

---

## Airport
| Code | Airport | Role |
|------|---------|------|
| RMO  | Chisinau International | Only departure/arrival airport |

No open-jaw, no secondary airports. RMO only.

---

## Locked Weekend
**Hungary — Aug 18–22:** Hardcoded exclusion. Never appears in the window, never gets API calls, never stored in DB. Already booked (flights + Airbnb).

---

## Tech Stack

| Layer | Choice | Reason |
|-------|--------|--------|
| Language | Python 3.11+ | Preferred, rich ecosystem |
| Flight data | SerpApi — `google_travel_explore` engine | 1 call returns all worldwide destinations for given dates, free tier sufficient |
| Database | SQLite via `sqlite3` | Zero setup, local, perfect for logging price history |
| Dashboard | Streamlit | Python-native UI, no JS needed |
| Data processing | pandas | Filtering, sorting, aggregation |
| HTTP | `serpapi` Python package | Official client |

No scheduler — data is collected manually via a refresh button in the dashboard.

---

## API Details — SerpApi Google Flights

- **Primary engine:** `google_travel_explore` — 1 call returns ALL destinations worldwide for a given departure date + return date
- **Auth:** API key from serpapi.com (free tier)
- **Free tier:** 250 searches/month — sufficient for this project
- **Python package:** `pip install google-search-results`
- **Key params:**
  - `engine`: `google_travel_explore`
  - `departure_id`: `RMO`
  - `arrival_area_id`: omitted — no region filter, collect worldwide
  - `outbound_date`: YYYY-MM-DD
  - `return_date`: YYYY-MM-DD
  - `currency`: `USD`
  - `hl`: `en`
  - `adults`: `2`
  - `stops`: `2` (1 stop or fewer)
  - `max_price`: `1000`
  - `type`: `1` (round trip)

**RMO verified working** — tested live in SerpApi playground on Jun 9, 2026. Returned Athens ($77), Lisbon ($269), Dublin ($194), London and more. Status: Success.

**No region filter — by design.** Leaving `arrival_area_id` blank returns all worldwide destinations in one call. Price filtering ($1,000 cap) is applied via `max_price` param and also in Python after the response. This means Istanbul, Cairo, Dubai, or anything else cheap out of RMO will show up naturally.

**1 call per window type — no departure_token needed.** Unlike `google_flights`, the Explore engine returns all destinations with prices in a single call. No second call required.

**Actual response shape (verified from live call):**
- Top-level key is `destinations`
- IATA is at `destination_airport.code`
- Price is `flight_price`
- Stops is `number_of_stops`
- Duration is `flight_duration` (outbound leg, minutes)
- Entries without `flight_price` are suggestion-only rows — filter these out

### Call budget
Manual refresh — user controls when calls are made. Each full refresh across all 4 window types × 6 weekends = **24 calls per refresh**. Free tier is 250 calls/month, giving ~10 full refreshes per month with headroom to spare.

---

## Data Model

### `flights` table (SQLite)
```sql
CREATE TABLE flights (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at            DATETIME NOT NULL,
    window_fri_date       DATE NOT NULL,        -- the Friday that identifies this weekend
    window_type           TEXT NOT NULL,         -- 'fri_sun', 'thu_sun', 'fri_mon', 'thu_mon'
    outbound_date         DATE NOT NULL,
    return_date           DATE NOT NULL,
    destination_iata      TEXT NOT NULL,
    destination_city      TEXT NOT NULL,
    destination_country   TEXT NOT NULL,
    price_usd_2pax        REAL NOT NULL,         -- total for 2 people roundtrip USD
    outbound_stops        INTEGER NOT NULL,
    return_stops          INTEGER NOT NULL,
    outbound_duration_min INTEGER,
    return_duration_min   INTEGER,
    airline               TEXT,
    booking_link          TEXT
);
```

### `fetch_log` table
```sql
CREATE TABLE fetch_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at      DATETIME NOT NULL,
    weekends_fetched INTEGER,
    calls_made      INTEGER,
    status          TEXT,                        -- 'success', 'error', 'rate_limited'
    notes           TEXT
);
```

---

## File Structure
```
moldova-flights/
├── PLANNING.md              ← this file
├── .env                     ← API key (never commit)
├── .gitignore
├── requirements.txt
├── data/
│   └── flights.db           ← SQLite database (auto-created on first run, never committed)
├── src/
│   ├── config.py            ← constants: locked weekends, budget thresholds, window types
│   ├── windows.py           ← rolling window logic: compute next 6 Fri-Sun blocks
│   ├── fetcher.py           ← SerpApi calls, rate limiting, error handling
│   ├── db.py                ← SQLite read/write helpers
│   └── dashboard.py         ← Streamlit UI
└── tests/
    └── test_windows.py      ← unit tests for rolling window logic (32/32 passing)
```

---

## Dashboard Features (Streamlit)

### Tabs
1. **Overview** — 6 weekend cards in a 3×2 grid, best price per weekend, color-coded by price tier, Wizz Air badge
2. **Weekend detail** — dropdown to pick a weekend, full sortable flight table with all destinations
3. **Price history** — line chart showing how prices for a selected destination + weekend have changed over time
4. **Booking timing** — scatter chart of days-before-departure vs price, aggregated across all weekends for a destination

### Sidebar filters (persistent across all tabs)
- Window type (Fri–Sun, Thu–Sun, Fri–Mon, Thu–Mon)
- Max price slider ($100–$1,000, default $1,000)
- Max stops (Any / Nonstop only / 1 stop max)
- Country multiselect — auto-populated from DB

### Header
- Title + last updated timestamp (reads from `fetch_log`, shows "Never" on first launch)
- **Manual refresh button** — top right. Triggers a full fetch for all active windows. No auto-refresh on launch. Only makes API calls when clicked.

### Price tier badges
| Badge | Range | Color |
|-------|-------|-------|
| Great deal | Under $400 | Green |
| Good option | $400–$500 | Amber |
| OK | $500–$999 | Gray |
| (hidden) | $1,000+ | Not stored or shown |

### Wizz Air flagging
Wizz Air results are flagged with a `W` badge in pink. User has a Wizz Air membership so actual price will be lower than displayed. Never exclude Wizz Air from results.

---

## Historical / Booking Timing Data Strategy
1. **Self-logging from day one:** Every fetch writes to SQLite with `fetched_at` timestamp. Pre-trip fetches (before Jul 7) build the price curve for the actual trip weekends
2. **Booking timing chart:** Plot days-before-departure (x-axis) vs price (y-axis) per destination — directly answers "when should I book?"
3. **Empty states:** Both history tabs show a friendly message when fewer than 2 data points exist rather than an empty chart

---

## Build Order

### Phase 1 — Core pipeline
- [x] Project setup, `.env`, `requirements.txt`
- [x] `config.py` — budget thresholds, locked dates, window type definitions
- [x] `windows.py` — rolling 6-week Friday-anchored window calculator, Sep 1 hard stop, Aug 18–22 exclusion
- [x] `db.py` — SQLite schema creation, insert/query helpers
- [x] `fetcher.py` — SerpApi integration, live test confirmed RMO working
- [x] Manual test: single fetch for one weekend, verified data in DB

### Phase 2 — Dashboard MVP (no scheduler — manual refresh only)
- [x] Overview tab: 6 weekend cards with price badges and Wizz Air flagging
- [x] Weekend detail tab: sortable flight table
- [x] Last updated timestamp (read-only)
- [x] Manual refresh button — no auto-refresh on launch

### Phase 3 — History & insights
- [x] Price history chart (line chart per route over time)
- [x] Booking timing chart (days-before vs price)

---

## Environment Variables (.env)
```
SERPAPI_KEY=your_key_here
```

---

## Key Decisions Log
- Prices always stored and displayed as **total for 2 people, roundtrip, USD**
- **Wizz Air (W6) flights are flagged** — user has a Wizz Air membership so actual price will be lower than displayed. Never exclude Wizz Air.
- USD only — user will mentally adjust for MDL exchange rate and Wizz Air membership discount
- Nothing above $1,000 is stored or shown
- Window is **rolling 6 Fridays forward** from today, not a fixed list
- Aug 18–22 is a permanent hardcoded exclusion
- Sep 1 is a hard data collection cutoff
- No open-jaw routing (RMO only)
- No scheduler — manual refresh button only, no auto-refresh on launch
- Data collection intentionally starts before the trip (pre-July 7) to build booking timing history
- `flights.db` is never committed to git — each machine maintains its own local database
- Personal PC is the primary data collection machine — work computer is for code changes only