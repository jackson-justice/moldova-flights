"""Phase 1 unit tests for the google_flights pricing path in src/fetcher.py.

These run entirely against saved fixtures in data/explore_probe/ and synthetic
payloads — no live API calls.
"""
import json
from datetime import date
from pathlib import Path

import pytest

from src.config import PRICE_CAP, MAX_STOPS, ORIGIN_IATA
from src.fetcher import (
    flights_result_status,
    fetch_with_retry,
    fetch_flights,
    parse_flights_results,
)

FIXTURES = Path(__file__).parent.parent / "data" / "explore_probe"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# flights_result_status
# ---------------------------------------------------------------------------

class TestFlightsResultStatus:

    def test_populated_roundtrip_is_ok(self):
        # gf_rt_ATH_jun19_MON: 3 best + 7 other = 10 itineraries
        status, n = flights_result_status(load("gf_rt_ATH_jun19_MON.json"))
        assert status == "ok"
        assert n == 10

    def test_no_results_payload_is_empty_retryable(self):
        # gf_rt_ATH_jul03_SUN: {"error": "...hasn't returned any results..."}
        status, n = flights_result_status(load("gf_rt_ATH_jul03_SUN.json"))
        assert status == "empty"      # transient -> retryable, NOT 'error'
        assert n == 0

    def test_genuine_error_is_error(self):
        status, n = flights_result_status({"error": "Your account has run out of searches."})
        assert status == "error"
        assert n == 0

    def test_empty_lists_no_error_is_empty(self):
        status, n = flights_result_status({"best_flights": [], "other_flights": []})
        assert status == "empty"
        assert n == 0

    def test_only_other_flights_counts(self):
        status, n = flights_result_status({"other_flights": [{"price": 1}, {"price": 2}]})
        assert status == "ok"
        assert n == 2


# ---------------------------------------------------------------------------
# fetch_with_retry
# ---------------------------------------------------------------------------

class TestFetchWithRetry:

    def test_retries_transient_empty_then_succeeds(self):
        empty = load("gf_rt_ATH_jul03_SUN.json")          # classifies 'empty'
        ok    = load("gf_rt_ATH_jun19_MON.json")          # classifies 'ok'
        seq = [empty, empty, ok]
        calls = {"n": 0}

        def call():
            r = seq[calls["n"]]
            calls["n"] += 1
            return r

        raw, status, n = fetch_with_retry(call, retries=2, sleep_s=0)
        assert status == "ok"
        assert n == 3            # empty, empty, ok
        assert calls["n"] == 3

    def test_stops_after_retries_exhausted(self):
        empty = load("gf_rt_ATH_jul03_SUN.json")
        calls = {"n": 0}

        def call():
            calls["n"] += 1
            return empty

        raw, status, n = fetch_with_retry(call, retries=2, sleep_s=0)
        assert status == "empty"
        assert n == 3            # retries=2 -> 3 total attempts
        assert calls["n"] == 3

    def test_error_returns_immediately_no_retry(self):
        calls = {"n": 0}

        def call():
            calls["n"] += 1
            return {"error": "rate limited"}

        raw, status, n = fetch_with_retry(call, retries=2, sleep_s=0)
        assert status == "error"
        assert n == 1            # errors are not retried
        assert calls["n"] == 1

    def test_immediate_ok_single_attempt(self):
        ok = load("gf_rt_ATH_jun19_MON.json")
        raw, status, n = fetch_with_retry(lambda: ok, retries=2, sleep_s=0)
        assert status == "ok"
        assert n == 1


# ---------------------------------------------------------------------------
# parse_flights_results
# ---------------------------------------------------------------------------

class TestParseFlightsResults:

    def test_cheapest_qualifying_from_fixture(self):
        raw = load("gf_rt_ATH_jun19_SUN_nostopsparam.json")
        rows = parse_flights_results(
            raw, date(2026, 6, 19), "fri_sun",
            "ATH", "Athens", "Greece", "2026-06-19T12:00:00",
        )
        assert len(rows) == 1
        r = rows[0]
        # Cheapest qualifying itinerary in this fixture: $594, nonstop, SkyUp MT
        assert r["price_usd_2pax"] == 594.0
        assert r["outbound_stops"] == 0
        assert r["outbound_duration_min"] == 135
        assert r["airline"] == "SkyUp MT"

    def test_storage_fields_and_window_dates(self):
        raw = load("gf_rt_ATH_jun19_SUN_nostopsparam.json")
        rows = parse_flights_results(
            raw, date(2026, 6, 19), "fri_sun",
            "ATH", "Athens", "Greece", "2026-06-19T12:00:00",
        )
        r = rows[0]
        assert r["destination_iata"] == "ATH"
        assert r["destination_city"] == "Athens"
        assert r["destination_country"] == "Greece"
        assert r["window_fri_date"] == "2026-06-19"
        assert r["window_type"] == "fri_sun"
        # fri_sun: outbound = Friday, return = Friday + 2 (Sunday)
        assert r["outbound_date"] == "2026-06-19"
        assert r["return_date"] == "2026-06-21"
        assert r["fetched_at"] == "2026-06-19T12:00:00"
        # v1 single-call pricing: no return-leg detail
        assert r["return_stops"] is None
        assert r["return_duration_min"] is None
        assert r["price_source"] == "google_flights"
        assert "departure_token" in r

    def test_empty_response_returns_no_rows(self):
        rows = parse_flights_results(
            load("gf_rt_ATH_jul03_SUN.json"), date(2026, 7, 3), "fri_sun",
            "ATH", "Athens", "Greece", "2026-07-03T12:00:00",
        )
        assert rows == []

    def test_price_cap_and_max_stops_filtered_client_side(self):
        # Synthetic payload exercising both filters deterministically.
        raw = {
            "best_flights": [
                {"price": PRICE_CAP + 200, "total_duration": 100, "layovers": [],
                 "flights": [{"airline": "OverCap"}]},                       # over cap
                {"price": 500, "total_duration": 300,
                 "layovers": [{"id": "VIE"}, {"id": "MUC"}],
                 "flights": [{"airline": "A"}, {"airline": "B"}, {"airline": "C"}]},  # 2 stops
            ],
            "other_flights": [
                {"price": 480, "total_duration": 150, "layovers": [{"id": "VIE"}],
                 "flights": [{"airline": "One"}, {"airline": "One"}]},        # qualifies (1 stop)
                {"price": 460, "total_duration": 140, "layovers": [],
                 "flights": [{"airline": "Cheap"}]},                          # qualifies, cheapest
            ],
        }
        rows = parse_flights_results(
            raw, date(2026, 6, 19), "fri_sun",
            "XXX", "Test", "Nowhere", "2026-06-19T12:00:00",
        )
        assert len(rows) == 1
        assert rows[0]["price_usd_2pax"] == 460.0      # cheapest qualifying, not 500/over-cap
        assert rows[0]["outbound_stops"] == 0
        assert rows[0]["airline"] == "Cheap"

    def test_all_over_cap_or_too_many_stops_returns_empty(self):
        raw = {"best_flights": [
            {"price": PRICE_CAP + 1, "total_duration": 100, "layovers": [],
             "flights": [{"airline": "X"}]},
            {"price": 300, "total_duration": 100,
             "layovers": [{"id": "A"}, {"id": "B"}],          # MAX_STOPS=1 -> excluded
             "flights": [{"airline": "Y"}]},
        ]}
        rows = parse_flights_results(
            raw, date(2026, 6, 19), "fri_sun",
            "XXX", "Test", "Nowhere", "2026-06-19T12:00:00",
        )
        assert rows == []

    def test_multi_carrier_airline_joined(self):
        raw = {"best_flights": [
            {"price": 400, "total_duration": 200, "layovers": [{"id": "VIE"}],
             "flights": [{"airline": "Austrian"}, {"airline": "Lufthansa"}]},
        ]}
        rows = parse_flights_results(
            raw, date(2026, 6, 19), "fri_sun",
            "XXX", "Test", "Nowhere", "2026-06-19T12:00:00",
        )
        assert rows[0]["airline"] == "Austrian / Lufthansa"

    def test_unknown_window_label_raises(self):
        with pytest.raises(ValueError):
            parse_flights_results(
                {"best_flights": []}, date(2026, 6, 19), "not_a_window",
                "ATH", "Athens", "Greece", "2026-06-19T12:00:00",
            )


# ---------------------------------------------------------------------------
# fetch_flights — parameter construction (no live call)
# ---------------------------------------------------------------------------

class TestFetchFlightsParams:

    def test_builds_google_flights_params_without_stops(self, monkeypatch):
        captured = {}

        class FakeSearch:
            def __init__(self, params):
                captured.update(params)

            def get_dict(self):
                return {"best_flights": [], "other_flights": []}

        import serpapi
        monkeypatch.setattr(serpapi, "GoogleSearch", FakeSearch)
        monkeypatch.setattr("src.fetcher._api_key", lambda: "TESTKEY")

        fetch_flights(date(2026, 6, 19), date(2026, 6, 21), "ATH")

        assert captured["engine"] == "google_flights"
        assert captured["departure_id"] == ORIGIN_IATA      # RMO
        assert captured["arrival_id"] == "ATH"
        assert captured["outbound_date"] == "2026-06-19"
        assert captured["return_date"] == "2026-06-21"
        assert captured["type"] == "1"
        assert "stops" not in captured                       # decision D2
        assert captured["api_key"] == "TESTKEY"
