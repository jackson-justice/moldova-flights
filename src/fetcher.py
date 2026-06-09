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
