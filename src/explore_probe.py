"""Diagnostic probe for google_travel_explore on RMO windows.

Tests two hypotheses about why most RMO windows return empty:
  A) type=2 (one way, no return_date) — does outbound-only return results for all 6 weekends?
  B) type=1 with NO return_date      — does flexible-return give results for all 6 weekends?

Saves every raw response under data/explore_probe/ and prints a per-weekend summary
plus a full sample payload for each test.

Run:  python -m src.explore_probe
"""
import json
import time
from datetime import date, timedelta
from pathlib import Path

from src.config import (
    ORIGIN_IATA, SERPAPI_CURRENCY, SERPAPI_LANGUAGE, SERPAPI_ADULTS,
)
from src.fetcher import _api_key, is_near_term
from src.windows import get_rolling_fridays

OUT_DIR = Path(__file__).parent.parent / "data" / "explore_probe"
SEP = "=" * 72
DELAY_S = 2  # throttle between calls to avoid rate limiting


def explore(outbound_date: date, return_date: date = None, trip_type: str = "1") -> dict:
    """Call google_travel_explore. Omits return_date when None."""
    from serpapi import GoogleSearch
    params = {
        "engine":        "google_travel_explore",
        "departure_id":  ORIGIN_IATA,
        "outbound_date": outbound_date.isoformat(),
        "currency":      SERPAPI_CURRENCY,
        "hl":            SERPAPI_LANGUAGE,
        "adults":        str(SERPAPI_ADULTS),
        "stops":         "2",
        "type":          trip_type,
        "api_key":       _api_key(),
    }
    if return_date is not None:
        params["return_date"] = return_date.isoformat()
    return GoogleSearch(params).get_dict()


def summarize(raw: dict) -> dict:
    meta = raw.get("search_metadata", {}) or {}
    dests = raw.get("destinations") or []
    return {
        "status":  meta.get("status", "unknown"),
        "n_dest":  len(dests),
        "error":   raw.get("error"),
        "top_keys": [k for k in raw if not k.startswith("search_")],
    }


def run_test(label: str, fridays: list, trip_type: str, with_return: bool) -> None:
    print(SEP)
    print(f"TEST {label}")
    print(f"  type={trip_type}  return_date={'<friday+2 / Sun>' if with_return else 'OMITTED'}")
    print(SEP)

    today = date.today()
    raws = []
    for i, fri in enumerate(fridays, 1):
        outbound = fri                         # Friday departure (out_offset=0)
        ret = fri + timedelta(days=2) if with_return else None  # Sun return (has been failing)

        if i > 1:
            time.sleep(DELAY_S)
        try:
            raw = explore(outbound, ret, trip_type=trip_type)
        except Exception as exc:
            print(f"  {i}. {fri}  ->  EXCEPTION: {exc}")
            raws.append((fri, {"error": f"exception: {exc}"}))
            continue

        s = summarize(raw)
        nt = " (near-term)" if is_near_term(outbound, today) else ""
        ret_s = ret.isoformat() if ret else "—"
        err = f"  ERROR: {s['error']}" if s["error"] else ""
        print(f"  {i}. out {outbound}  ret {ret_s}{nt}  ->  "
              f"status={s['status']}  destinations={s['n_dest']}{err}")

        fn = OUT_DIR / f"test{label}_{i}_{fri.isoformat()}.json"
        fn.write_text(json.dumps(raw, indent=2))
        raws.append((fri, raw))

    # Full sample: first non-empty response, else first response
    sample = next((r for _, r in raws if (r.get("destinations") or [])), None)
    if sample is None and raws:
        sample = raws[0][1]
    print(f"\n  -- FULL RAW SAMPLE (test {label}) --")
    print(json.dumps(sample, indent=2)[:6000])
    print(f"  -- end sample (full files in {OUT_DIR}) --\n")


if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fridays = get_rolling_fridays()
    print(f"Origin: {ORIGIN_IATA}   Today: {date.today()}   Weekends: "
          f"{[f.isoformat() for f in fridays]}\n")

    run_test("A", fridays, trip_type="2", with_return=False)   # one way
    run_test("B", fridays, trip_type="1", with_return=False)   # roundtrip, flexible return

    print(SEP)
    print("Done. Compare destinations>0 across weekends for each test.")
    print(SEP)
