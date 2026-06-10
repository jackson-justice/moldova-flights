"""SerpApi google_travel_explore integration.

One call per (outbound_date, return_date) pair returns all worldwide destinations
with prices for that window — no second call needed.

Actual response shape (confirmed from live test 2026-06-09):
  raw["destinations"] -> list of destination dicts, each with:
    destination_airport.code  -- IATA
    name                      -- city name
    country                   -- country name
    flight_price              -- total USD for all adults (we pass adults=2)
    flight_duration           -- outbound duration in minutes
    number_of_stops           -- stop count (appears to be outbound leg)
    airline                   -- airline name string
    link                      -- Google Flights booking URL
"""
import json
import os
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from src.config import (
    MAX_STOPS,
    ORIGIN_IATA,
    PRICE_CAP,
    SERPAPI_ADULTS,
    SERPAPI_CURRENCY,
    SERPAPI_LANGUAGE,
    WINDOW_TYPES,
)
from src.windows import get_window_dates

# Google Flights Explore does not reliably return results for near-term
# departures, so windows whose outbound date is this many days away (or fewer)
# are skipped without spending an API call.
NEAR_TERM_SKIP_DAYS = 3


def is_near_term(outbound_date: date, today: date = None,
                 days: int = NEAR_TERM_SKIP_DAYS) -> bool:
    """
    True if outbound_date is within `days` of today (inclusive) — too close in to
    fetch reliably. e.g. with today=Jun 9 and days=3, any departure on Jun 9-12
    is near-term and should be skipped.
    """
    if today is None:
        today = date.today()
    return (outbound_date - today).days <= days


def explore_result_status(raw: dict) -> tuple:
    """
    Classify a raw explore response so the caller can skip unusable windows.

    Returns (status, n_destinations):
      'error' — API returned an error payload (e.g. rate limited); n = 0
      'empty' — no error but zero destinations came back;          n = 0
      'ok'    — usable destinations present;                       n = count
    """
    if "error" in raw:
        return "error", 0
    destinations = raw.get("destinations") or []
    if not destinations:
        return "empty", 0
    return "ok", len(destinations)


# google_flights returns this error string when a query has no itineraries. It is
# transient (the same query often succeeds on retry), so it is classified as
# 'empty' (retryable) rather than 'error' (a genuine, non-transient failure).
_NO_RESULTS_MARKER = "hasn't returned any results"


def flights_result_status(raw: dict) -> tuple:
    """
    Classify a raw google_flights response so the caller can skip or retry.

    Returns (status, n_itineraries) where n = len(best_flights)+len(other_flights):
      'ok'    — itineraries present;                                   n = count
      'empty' — no itineraries, incl. the transient "hasn't returned   n = 0
                any results" payload — RETRYABLE;
      'error' — a genuine API error payload (rate limited, bad key);   n = 0
                NOT a transient empty.
    """
    n = len(raw.get("best_flights") or []) + len(raw.get("other_flights") or [])
    if n > 0:
        return "ok", n
    err = raw.get("error")
    if err and _NO_RESULTS_MARKER.lower() in str(err).lower():
        return "empty", 0
    if err:
        return "error", 0
    return "empty", 0


def fetch_with_retry(call, classify=flights_result_status, retries: int = 2,
                     sleep_s: float = 2, debug: bool = False, tag: str = ""):
    """
    Call `call()` and classify the response; retry transient empties.

    `retries` is the number of EXTRA attempts after a transient 'empty' (so total
    attempts = retries + 1). An 'error' or 'ok' result returns immediately —
    errors are not retried here because they are not the transient-empty case.

    Returns (raw, status, n_attempts).
    """
    raw, status, n = None, "empty", 0
    total = retries + 1
    while n < total:
        n += 1
        if n > 1:
            time.sleep(sleep_s)
        raw = call()
        status, _ = classify(raw)
        if status != "empty":
            break
        if debug:
            print(f"[RETRY] {tag}: empty (attempt {n}/{total})")
    return raw, status, n


def _api_key() -> str:
    key = os.getenv("SERPAPI_KEY")
    if not key:
        raise RuntimeError("SERPAPI_KEY not set in .env")
    return key


def fetch_explore(outbound_date: date, return_date: date) -> dict:
    """
    Call google_travel_explore for the given date pair.
    Returns the raw API response dict (no filtering applied yet).
    """
    from serpapi import GoogleSearch

    params = {
        "engine":        "google_travel_explore",
        "departure_id":  ORIGIN_IATA,
        "outbound_date": outbound_date.isoformat(),
        "return_date":   return_date.isoformat(),
        "currency":      SERPAPI_CURRENCY,
        "hl":            SERPAPI_LANGUAGE,
        "adults":        str(SERPAPI_ADULTS),
        "stops":         "2",   # 2 = up to 1 stop (SerpApi encoding)
        "type":          "1",   # 1 = roundtrip
        "api_key":       _api_key(),
    }

    search = GoogleSearch(params)
    return search.get_dict()


def discover_destinations(outbound_date: date) -> dict:
    """
    One-way Explore call (type=2, no return_date, no stops) used to DISCOVER which
    destinations are reachable from RMO on an outbound date, with a one-way price
    signal for ranking the shortlist.

    Roundtrip Explore returns empty for RMO; one-way is the reliable discovery
    path. Returns the raw API response — the caller reads raw["destinations"].
    """
    from serpapi import GoogleSearch

    params = {
        "engine":        "google_travel_explore",
        "departure_id":  ORIGIN_IATA,
        "outbound_date": outbound_date.isoformat(),
        "currency":      SERPAPI_CURRENCY,
        "hl":            SERPAPI_LANGUAGE,
        "adults":        str(SERPAPI_ADULTS),
        "type":          "2",   # 2 = one way (no return_date)
        "api_key":       _api_key(),
    }

    search = GoogleSearch(params)
    return search.get_dict()


def fetch_flights(outbound_date: date, return_date: date, arrival_id: str) -> dict:
    """
    Price one RMO -> arrival_id roundtrip via the google_flights engine.

    Deliberately omits the `stops` parameter: RMO is sparsely connected and the
    stops filter over-constrains the query (it wipes out otherwise-valid result
    sets and amplifies transient empties). Stop filtering is applied client-side
    in parse_flights_results via MAX_STOPS instead.
    """
    from serpapi import GoogleSearch

    params = {
        "engine":        "google_flights",
        "departure_id":  ORIGIN_IATA,
        "arrival_id":    arrival_id,
        "outbound_date": outbound_date.isoformat(),
        "return_date":   return_date.isoformat(),
        "currency":      SERPAPI_CURRENCY,
        "hl":            SERPAPI_LANGUAGE,
        "adults":        str(SERPAPI_ADULTS),
        "type":          "1",   # 1 = roundtrip
        "api_key":       _api_key(),
    }

    search = GoogleSearch(params)
    return search.get_dict()


def parse_explore_results(
    raw: dict,
    fri_date: date,
    window_label: str,
    fetched_at: str,
) -> list:
    """
    Parse a raw explore API response into flight row dicts ready for DB insertion.
    Applies PRICE_CAP and MAX_STOPS filters.

    fri_date: the Friday anchor for this weekend block
    window_label: one of 'fri_sun', 'thu_sun', 'fri_mon', 'thu_mon'
    fetched_at: ISO datetime string (UTC) of when this batch was fetched
    """
    wt = next((w for w in WINDOW_TYPES if w["label"] == window_label), None)
    if wt is None:
        raise ValueError(f"Unknown window_label: {window_label!r}")

    outbound_date, return_date = get_window_dates(fri_date, wt)

    destinations = raw.get("destinations", [])

    rows = []
    for dest in destinations:
        # ---- price ----
        price = dest.get("flight_price")
        if price is None:
            continue
        if price > PRICE_CAP:
            continue

        # ---- identity ----
        iata    = (dest.get("destination_airport") or {}).get("code", "")
        city    = dest.get("name", "")
        country = dest.get("country", "")
        if not iata:
            continue

        # ---- stops (explore returns a single stop count, not per-leg) ----
        stops = dest.get("number_of_stops", 0)
        if stops > MAX_STOPS:
            continue

        # ---- other fields ----
        duration = dest.get("flight_duration")   # outbound minutes
        airline  = dest.get("airline")
        link     = dest.get("link")

        rows.append({
            "fetched_at":            fetched_at,
            "window_fri_date":       fri_date.isoformat(),
            "window_type":           window_label,
            "outbound_date":         outbound_date.isoformat(),
            "return_date":           return_date.isoformat(),
            "destination_iata":      iata,
            "destination_city":      city,
            "destination_country":   country,
            "price_usd_2pax":        float(price),
            "outbound_stops":        stops,
            # explore doesn't give per-leg return stops; mirror outbound as best estimate
            "return_stops":          stops,
            "outbound_duration_min": duration,
            "return_duration_min":   None,
            "airline":               airline,
            "booking_link":          link,
        })

    return rows


def _itinerary_airline(itinerary: dict) -> str:
    """Distinct carriers across an itinerary's legs, in order, joined with ' / '."""
    airlines = []
    for leg in itinerary.get("flights") or []:
        a = leg.get("airline")
        if a and a not in airlines:
            airlines.append(a)
    return " / ".join(airlines) if airlines else None


def parse_flights_results(
    raw: dict,
    fri_date: date,
    window_label: str,
    destination_iata: str,
    destination_city: str,
    destination_country: str,
    fetched_at: str,
) -> list:
    """
    Parse a raw google_flights roundtrip response into flight row dicts.

    Returns a single-element list with the CHEAPEST itinerary that passes
    PRICE_CAP and MAX_STOPS (across best_flights + other_flights), or [] if none
    qualify. The destination identity comes from the discovery shortlist (the
    caller), since google_flights is queried per-route.

    Notes vs the Explore parser:
      * outbound_stops = number of layovers on the itinerary.
      * return_stops / return_duration_min are None — the first roundtrip call
        returns only the outbound leg detail; the total `price` is already the
        full roundtrip price.
      * price_source / departure_token are populated for the upcoming schema.
    """
    wt = next((w for w in WINDOW_TYPES if w["label"] == window_label), None)
    if wt is None:
        raise ValueError(f"Unknown window_label: {window_label!r}")

    outbound_date, return_date = get_window_dates(fri_date, wt)

    itineraries = (raw.get("best_flights") or []) + (raw.get("other_flights") or [])

    best = None
    for it in itineraries:
        price = it.get("price")
        if price is None or price > PRICE_CAP:
            continue
        stops = len(it.get("layovers") or [])
        if stops > MAX_STOPS:
            continue
        if best is None or price < best.get("price"):
            best = it

    if best is None:
        return []

    return [{
        "fetched_at":            fetched_at,
        "window_fri_date":       fri_date.isoformat(),
        "window_type":           window_label,
        "outbound_date":         outbound_date.isoformat(),
        "return_date":           return_date.isoformat(),
        "destination_iata":      destination_iata,
        "destination_city":      destination_city,
        "destination_country":   destination_country,
        "price_usd_2pax":        float(best["price"]),
        "outbound_stops":        len(best.get("layovers") or []),
        # google_flights' first roundtrip call carries only outbound leg detail
        "return_stops":          None,
        "outbound_duration_min": best.get("total_duration"),
        "return_duration_min":   None,
        "airline":               _itinerary_airline(best),
        "booking_link":          None,
        "price_source":          "google_flights",
        "departure_token":       best.get("departure_token"),
    }]


# ---------------------------------------------------------------------------
# Live test -- run with:  python -m src.fetcher
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    TEST_OUTBOUND = date(2026, 7, 11)
    TEST_RETURN   = date(2026, 7, 13)

    SEP = "=" * 60
    print(SEP)
    print(f"Live test: {ORIGIN_IATA} -> all destinations")
    print(f"  outbound : {TEST_OUTBOUND}  ({TEST_OUTBOUND.strftime('%A')})")
    print(f"  return   : {TEST_RETURN}   ({TEST_RETURN.strftime('%A')})")
    print(f"  adults   : {SERPAPI_ADULTS}")
    print(f"  currency : {SERPAPI_CURRENCY}")
    print(f"  stops    : 2  (= up to 1 stop)")
    print(f"  type     : 1  (roundtrip)")
    print(f"  max_price: {PRICE_CAP}")
    print(SEP)

    raw = fetch_explore(TEST_OUTBOUND, TEST_RETURN)

    # ---- status ----
    meta = raw.get("search_metadata", {})
    print(f"\nStatus : {meta.get('status', 'unknown')}")

    # ---- top-level keys (shows the response shape) ----
    top_keys = [k for k in raw if not k.startswith("search_")]
    print(f"Top-level keys: {top_keys}")

    # ---- destination list ----
    destinations = raw.get("destinations", [])
    print(f"Destinations returned: {len(destinations)}")

    if destinations:
        print("\nFirst result (raw):")
        print(json.dumps(destinations[0], indent=2))

        print(f"\n{'-'*64}")
        print(f"{'IATA':<6} {'City':<22} {'Country':<18} {'Stops':>5} {'USD':>8}  Airline")
        print(f"{'-'*64}")
        def asc(s: str, width: int) -> str:
            return s.encode("ascii", "replace").decode("ascii")[:width]

        for d in sorted(destinations, key=lambda x: x.get("flight_price", 9999)):
            iata    = (d.get("destination_airport") or {}).get("code", "???")
            city    = asc(d.get("name", "???"), 21)
            country = asc(d.get("country", ""), 17)
            stops   = d.get("number_of_stops", "?")
            price   = d.get("flight_price", "N/A")
            airline = asc(d.get("airline") or "", 20)
            deal    = " *" if isinstance(price, (int, float)) and price < 400 else ""
            print(f"{iata:<6} {city:<22} {country:<18} {str(stops):>5} {str(price):>8}{deal}  {airline}")

        print(f"{'-'*64}")
        print("  * = under $400 (Great deal threshold)")

    if "error" in raw:
        print(f"\nAPI error: {raw['error']}")

    raw_path = Path(__file__).parent.parent / "data" / "serpapi_raw_test.json"
    raw_path.write_text(json.dumps(raw, indent=2))
    print(f"\nFull raw response saved to: {raw_path}")
    print(SEP)
