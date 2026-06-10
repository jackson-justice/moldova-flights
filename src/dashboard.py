"""Streamlit dashboard for Moldova Flight Tracker.

Run from the project root:
    streamlit run src/dashboard.py
"""
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# Guarantee project root is importable regardless of launch method
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st
import pandas as pd

from src.config import PRICE_CAP, PRICE_GREAT, PRICE_GOOD, WINDOW_TYPES
from src.db import (
    get_conn, init_db,
    insert_flights, log_fetch,
    get_flights_for_window, get_price_history,
)
from src.windows import get_rolling_fridays, get_all_windows, get_window_dates
from src.fetcher import (
    discover_destinations, explore_result_status,
    fetch_flights, parse_flights_results, flights_result_status, fetch_with_retry,
    is_near_term, NEAR_TERM_SKIP_DAYS,
)

# ── Page config (must be the very first Streamlit call) ──────────────────────
st.set_page_config(
    page_title="Moldova Flight Tracker",
    page_icon=":airplane:",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Bootstrap DB ─────────────────────────────────────────────────────────────
init_db()

# ── Constants ────────────────────────────────────────────────────────────────
WINDOW_DISPLAY = {
    "fri_sun": "Fri - Sun",
    "thu_sun": "Thu - Sun",
    "fri_mon": "Fri - Mon",
    "thu_mon": "Thu - Mon (weekly)",
}

TIER_LABEL = {"great": "Great Deal", "good": "Good Option", "ok": "OK"}

# ── Hybrid refresh tuning (Phase 3) ──────────────────────────────────────────
PRICE_WINDOW_TYPE          = "fri_sun"   # only Fri–Sun is roundtrip-priced
DESTINATION_SHORTLIST_SIZE = 4           # S — top-N cheapest priced per weekend
EXCLUDE_IATA               = {"OTP"}     # Bucharest — never in the shortlist
RETRY_ON_EMPTY             = 2           # extra google_flights attempts on a transient empty

# ── Pure helpers ─────────────────────────────────────────────────────────────

def price_tier(price: float) -> str:
    if price < PRICE_GREAT:
        return "great"
    if price < PRICE_GOOD:
        return "good"
    return "ok"


def fmt_day(d: date) -> str:
    """'Jun 9' — no leading zero, works on all platforms."""
    return d.strftime("%b ") + str(d.day)


def date_range_label(fri: date, wl: str) -> str:
    wt  = next(w for w in WINDOW_TYPES if w["label"] == wl)
    out = fri - timedelta(days=wt["out_offset"])
    ret = fri + timedelta(days=wt["ret_offset"])
    return f"{fmt_day(out)} - {fmt_day(ret)}"


def fmt_duration(mins) -> str:
    if mins is None:
        return "-"
    h, m = divmod(int(mins), 60)
    return f"{h}h {m:02d}m"


def is_wizz(airline: str | None) -> bool:
    return bool(airline) and "wizz" in airline.lower()


def get_last_updated_str() -> str:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT MAX(fetched_at) FROM fetch_log WHERE status IN ('success','partial')"
        ).fetchone()
        ts = row[0] if row else None
        if not ts:
            return "Never"
        return datetime.fromisoformat(ts).strftime("%b %d, %Y %H:%M UTC")
    finally:
        conn.close()


def get_all_destinations() -> list:
    """All (iata, city, country) in the DB with valid prices, ordered by city."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT destination_iata AS iata,
                   destination_city AS city,
                   destination_country AS country
            FROM flights
            GROUP BY destination_iata
            ORDER BY destination_city
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_weekends_for_dest(iata: str, window_type: str) -> list:
    """window_fri_date strings (YYYY-MM-DD) that have data for iata + window_type."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT DISTINCT window_fri_date FROM flights
            WHERE destination_iata = ? AND window_type = ?
            ORDER BY window_fri_date
        """, (iata, window_type)).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def cheapest_for_friday(
    fri: date,
    wl: str,
    max_price: int,
    max_stops: int | None,
    countries: list,
) -> dict | None:
    """Cheapest flight for one Friday that passes all sidebar filters, or None."""
    conn = get_conn()
    rows = get_flights_for_window(fri.isoformat(), wl, conn=conn)
    conn.close()
    filtered = [
        r for r in rows
        if r["price_usd_2pax"] <= max_price
        and (max_stops is None or r["outbound_stops"] <= max_stops)
        and (not countries or r["destination_country"] in countries)
    ]
    return min(filtered, key=lambda r: r["price_usd_2pax"]) if filtered else None


def windows_with_data_for_friday(fri_iso: str) -> list:
    """
    Window-type labels that have at least one row for this Friday, in any window.

    Used so the Overview can distinguish 'no data at all' from 'data exists, just
    not for the window type you're currently viewing' (e.g. Jun 26, which was only
    fetched for thu_mon) — otherwise that data is invisible and looks like a bug.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT window_type FROM flights WHERE window_fri_date = ?",
            (fri_iso,),
        ).fetchall()
        present = {r[0] for r in rows}
        # Return in canonical WINDOW_TYPES order for stable display
        return [w["label"] for w in WINDOW_TYPES if w["label"] in present]
    finally:
        conn.close()


# ── CSS ──────────────────────────────────────────────────────────────────────
# Dark-mode palette. The app is pinned to the dark theme (.streamlit/config.toml),
# so cards are dark elevated surfaces with light text and accents tuned for
# contrast on the deep canvas. Every card element still sets an explicit color so
# the look is stable regardless of the user's global Streamlit theme preference.
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

:root {
  --ink-900:#f1f4f9; --ink-700:#c3cad8; --ink-500:#8b94a7; --ink-400:#5f6b80;
  --line:rgba(255,255,255,.09);
  --card:#161d2b; --card-soft:#10151f;
  --shadow:0 1px 2px rgba(0,0,0,.40), 0 6px 20px rgba(0,0,0,.34);
  --shadow-hover:0 4px 10px rgba(0,0,0,.45), 0 18px 42px rgba(0,0,0,.50);
  --radius:14px;
  --great-accent:#34d399; --great-ink:#6ee7b7; --great-tint:rgba(52,211,153,.13);
  --good-accent:#fbbf24;  --good-ink:#fcd34d;  --good-tint:rgba(251,191,36,.12);
  --ok-accent:#7c8aa3;
}

/* Base typography + layout */
html, body, [class*="css"] {
  font-family:'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
}
.block-container { padding-top:3.6rem; max-width:1180px; }

/* Header hero — explicit font stack + weight so the title renders consistently
   even when the webfont hasn't loaded yet. Generous line-height + padding so tall
   ascenders (the 'l' in Flight, the 'k' in Tracker) are never clipped. */
.hero-title { font-family:'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI',
                          Roboto, Helvetica, Arial, sans-serif;
              font-size:2.05rem; font-weight:700; letter-spacing:-.01em;
              color:var(--ink-900); margin:0; padding-top:.15em; line-height:1.28;
              -webkit-font-smoothing:antialiased; }
.hero-sub   { font-size:.92rem; color:var(--ink-500); margin-top:.4rem; }
.hero-sub b { color:var(--ink-700); font-weight:600; }
.hero-dot   { display:inline-block; width:5px; height:5px; border-radius:50%;
              background:var(--ink-400); margin:0 .6rem; vertical-align:middle; }

/* Weekend cards */
.pc { position:relative; background:var(--card); border:1px solid var(--line);
      border-radius:var(--radius); padding:17px 18px 15px; margin-bottom:14px;
      box-shadow:var(--shadow); overflow:hidden;
      transition:transform .15s ease, box-shadow .15s ease, border-color .15s ease; }
.pc:hover { transform:translateY(-2px); box-shadow:var(--shadow-hover);
            border-color:rgba(255,255,255,.16); }
.pc::before { content:""; position:absolute; left:0; top:0; bottom:0; width:4px;
              background:var(--ok-accent); }
.pc.t-great { background:linear-gradient(180deg,var(--great-tint),var(--card) 60%); }
.pc.t-good  { background:linear-gradient(180deg,var(--good-tint),var(--card) 60%); }
.pc.t-great::before { background:var(--great-accent); }
.pc.t-good::before  { background:var(--good-accent); }
.pc.t-ok::before    { background:var(--ok-accent); }
.pc.t-empty, .pc.t-partial { background:var(--card-soft); border-style:dashed; box-shadow:none; }
.pc.t-empty::before, .pc.t-partial::before { background:var(--line); }

.pc-head { display:flex; align-items:center; justify-content:space-between;
           gap:8px; margin-bottom:9px; min-height:20px; }
.c-date  { font-size:.72rem; font-weight:600; letter-spacing:.04em;
           text-transform:uppercase; color:var(--ink-500); }
.c-price { font-size:2rem; font-weight:800; letter-spacing:-.02em; line-height:1;
           color:var(--ink-900); margin:2px 0 9px; }
.pc.t-great .c-price { color:var(--great-ink); }
.pc.t-good  .c-price { color:var(--good-ink); }
.c-price.is-empty { color:var(--ink-400); font-weight:700; }
.c-dest  { font-size:1rem; font-weight:600; color:var(--ink-900); margin:0 0 4px; }
.c-dest .muted { color:var(--ink-500); font-weight:500; }
.c-sub   { font-size:.82rem; color:var(--ink-500); margin:0; line-height:1.4; }

/* Badges */
.badge { display:inline-flex; align-items:center; padding:3px 9px; border-radius:999px;
         font-size:.64rem; font-weight:700; letter-spacing:.05em;
         text-transform:uppercase; white-space:nowrap; line-height:1; }
.b-great { background:var(--great-accent); color:#05271c; }
.b-good  { background:var(--good-accent); color:#3a2905; }
.b-ok    { background:rgba(255,255,255,.10); color:var(--ink-700); }
.b-info  { background:rgba(99,102,241,.20); color:#c7d2fe; }
.b-wizz  { background:rgba(244,114,182,.16); color:#f9a8d4; margin-left:6px; }

/* Legend */
.legend-chip { display:inline-flex; align-items:center; gap:8px; font-size:.8rem;
               color:var(--ink-700); font-weight:500; }
.legend-dot  { width:11px; height:11px; border-radius:4px; display:inline-block; flex:0 0 auto; }

/* Section heading */
.sec-title { font-size:1.05rem; font-weight:700; color:var(--ink-900); margin:.1rem 0 .2rem; }
.sec-sub   { font-size:.85rem; color:var(--ink-500); margin:0 0 .6rem; }

/* Buttons */
.stButton button { border-radius:10px; font-weight:600; }
.stButton button[kind="primary"] { box-shadow:var(--shadow); }

/* Tabs */
.stTabs [data-baseweb="tab-list"] { gap:2px; border-bottom:1px solid var(--line); }
.stTabs [data-baseweb="tab"] { height:44px; padding:0 18px; font-weight:600;
                               color:var(--ink-500); background:transparent;
                               border-radius:9px 9px 0 0; }
.stTabs [data-baseweb="tab"]:hover { color:var(--ink-900); background:rgba(255,255,255,.04); }
.stTabs [aria-selected="true"] { color:var(--ink-900) !important; }
.stTabs [data-baseweb="tab-highlight"] { background:var(--great-accent); height:2.5px; }

/* Data grid */
[data-testid="stDataFrame"] { border:1px solid var(--line); border-radius:12px;
                              overflow:hidden; box-shadow:var(--shadow); }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
if "refresh_msg" not in st.session_state:
    st.session_state.refresh_msg = None
if "refresh_warnings" not in st.session_state:
    st.session_state.refresh_warnings = []

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Filters")

    wl = st.selectbox(
        "Window type",
        options=["fri_sun", "thu_sun", "fri_mon", "thu_mon"],
        format_func=lambda x: WINDOW_DISPLAY[x],
        help="Defines the outbound and return days of the trip.",
    )

    max_price = st.slider(
        "Max price (2 pax, USD)",
        min_value=100, max_value=PRICE_CAP, value=PRICE_CAP, step=50,
    )

    stops_choice = st.radio("Max stops", ["Any", "Nonstop only", "1 stop max"])
    max_stops = {"Any": None, "Nonstop only": 0, "1 stop max": 1}[stops_choice]

    st.divider()

    conn_s = get_conn()
    _country_rows = conn_s.execute(
        "SELECT DISTINCT destination_country FROM flights "
        "WHERE destination_country != '' ORDER BY destination_country"
    ).fetchall()
    conn_s.close()
    all_countries   = [r[0] for r in _country_rows]
    country_filter  = st.multiselect("Country", all_countries)

    st.divider()
    st.caption("Prices = **total for 2 people, roundtrip, USD.**")
    st.caption("**W** badge = Wizz Air — membership discount not reflected in price shown.")

    st.divider()
    debug_mode = st.checkbox(
        "Debug mode (verbose fetch logging)",
        value=False,
        help="Print each window being fetched and how many results it returned "
             "to the terminal running Streamlit.",
    )


# ── Refresh ───────────────────────────────────────────────────────────────────
def _shortlist_from_discovery(raw: dict) -> list:
    """
    Build the pricing shortlist from a one-way discovery response: dedupe by IATA
    (keeping the cheapest occurrence), exclude OTP, rank by one-way price, take the
    cheapest DESTINATION_SHORTLIST_SIZE.
    """
    seen = {}
    for d in raw.get("destinations") or []:
        iata  = (d.get("destination_airport") or {}).get("code")
        price = d.get("flight_price")
        if not iata or price is None or iata in EXCLUDE_IATA:
            continue
        if iata not in seen or price < seen[iata]["oneway_price"]:
            seen[iata] = {
                "iata":         iata,
                "city":         d.get("name", ""),
                "country":      d.get("country", ""),
                "oneway_price": float(price),
            }
    ranked = sorted(seen.values(), key=lambda x: x["oneway_price"])
    return ranked[:DESTINATION_SHORTLIST_SIZE]


def do_refresh(debug: bool = False) -> dict:
    """
    Two-phase hybrid refresh, persisting the results.

      Phase 1 (discovery): one one-way Explore call per Fri–Sun weekend → shortlist
                           the top-N cheapest destinations (dedup, OTP excluded).
      Phase 2 (pricing):   one google_flights roundtrip call per shortlist
                           destination (with retry-on-empty) → cheapest itinerary.

    IMPORTANT: makes NO Streamlit element calls inside the fetch loop. A sidebar
    interaction (e.g. toggling Debug) queues a rerun that Streamlit raises at the
    next st.* call, which would abort a half-finished refresh and discard every
    row. Keeping the loop free of st.* calls lets it always run to completion;
    progress UX is the single spinner around the call site.
    """
    today      = date.today()
    fetched_at = datetime.utcnow().isoformat()
    windows    = [w for w in get_all_windows(today)
                  if w["label"] == PRICE_WINDOW_TYPE and w["outbound_date"] >= today]

    empty_result = {"rows": 0, "discovery_calls": 0, "pricing_calls": 0,
                    "errors": 0, "empty": 0, "skipped": 0, "retries": 0,
                    "warnings": []}
    if not windows:
        return empty_result

    all_rows = []
    warnings = []            # human-facing notes, rendered AFTER the loop
    discovery_calls = pricing_calls = 0
    n_errors = n_empty = n_skipped = n_retries = 0

    def _throttle():
        # 2s between API calls (discovery or pricing), but not before the first.
        if discovery_calls + pricing_calls > 0:
            time.sleep(2)

    for w in windows:
        fri  = w["fri_date"]
        wtag = f"{w['label']} {fri} ({w['outbound_date']} -> {w['return_date']})"

        # Skip near-term weekends — discovery/pricing unreliable within a few days.
        if is_near_term(w["outbound_date"], today):
            n_skipped += 1
            if debug:
                print(f"[REFRESH] SKIP {wtag}: outbound within "
                      f"{NEAR_TERM_SKIP_DAYS} days of today")
            continue

        # ---- Phase 1: discovery (one-way Explore) ----
        _throttle()
        try:
            draw = discover_destinations(w["outbound_date"])
        except Exception as exc:
            discovery_calls += 1
            n_errors += 1
            warnings.append(f"Discovery error ({fri}): {exc}")
            if debug:
                print(f"[REFRESH] SKIP discovery {wtag}: EXCEPTION -> {exc}")
            continue
        discovery_calls += 1

        dstatus, n_dest = explore_result_status(draw)
        if dstatus == "error":
            n_errors += 1
            warnings.append(f"Discovery error ({fri}): {draw['error']}")
            if debug:
                print(f"[REFRESH] SKIP discovery {wtag}: ERROR -> {draw['error']}")
            continue
        if dstatus == "empty":
            n_empty += 1
            if debug:
                print(f"[REFRESH] SKIP discovery {wtag}: 0 destinations returned")
            continue

        shortlist = _shortlist_from_discovery(draw)
        if debug:
            print(f"[REFRESH] OK   discovery {wtag}: {n_dest} destinations -> "
                  f"shortlist {[d['iata'] for d in shortlist]}")

        # ---- Phase 2: pricing (google_flights per shortlist destination) ----
        for d in shortlist:
            _throttle()
            ptag = f"{w['label']} {fri} {d['iata']}"
            try:
                raw, status, attempts = fetch_with_retry(
                    lambda d=d: fetch_flights(w["outbound_date"], w["return_date"], d["iata"]),
                    classify=flights_result_status,
                    retries=RETRY_ON_EMPTY, sleep_s=2, debug=debug, tag=ptag,
                )
            except Exception as exc:
                pricing_calls += 1
                n_errors += 1
                warnings.append(f"Pricing error ({fri} {d['iata']}): {exc}")
                if debug:
                    print(f"[REFRESH] SKIP pricing {ptag}: EXCEPTION -> {exc}")
                continue

            pricing_calls += attempts
            n_retries     += attempts - 1

            if status == "error":
                n_errors += 1
                warnings.append(f"Pricing error ({fri} {d['iata']}): {raw.get('error')}")
                if debug:
                    print(f"[REFRESH] SKIP pricing {ptag}: ERROR -> {raw.get('error')}")
                continue
            if status == "empty":
                n_empty += 1
                if debug:
                    print(f"[REFRESH] SKIP pricing {ptag}: empty after {attempts} attempts")
                continue

            rows = parse_flights_results(
                raw, fri, w["label"], d["iata"], d["city"], d["country"], fetched_at)
            if debug:
                price = rows[0]["price_usd_2pax"] if rows else None
                print(f"[REFRESH] OK   pricing {ptag}: {len(rows)} row(s)"
                      + (f" @ ${price:,.0f}" if rows else " (no qualifying itinerary)"))
            all_rows.extend(rows)

    # Persist — reached unconditionally because nothing above can be interrupted.
    conn = get_conn()
    if all_rows:
        insert_flights(all_rows, conn=conn)
    note_bits = []
    if n_errors:  note_bits.append(f"{n_errors} errors")
    if n_empty:   note_bits.append(f"{n_empty} empty")
    if n_retries: note_bits.append(f"{n_retries} retries")
    if n_skipped: note_bits.append(f"{n_skipped} skipped (near-term)")
    log_fetch(
        {
            "fetched_at":       fetched_at,
            "weekends_fetched": len({r["window_fri_date"] for r in all_rows}),
            "calls_made":       discovery_calls + pricing_calls,
            "status":           "success" if not n_errors else "partial",
            "notes":            ", ".join(note_bits) or None,
        },
        conn=conn,
    )
    conn.close()

    if debug:
        print(f"[REFRESH] done: {len(all_rows)} rows, "
              f"{discovery_calls} discovery + {pricing_calls} pricing calls, "
              f"{n_errors} errors, {n_empty} empty, {n_retries} retries, "
              f"{n_skipped} skipped")

    return {"rows": len(all_rows),
            "discovery_calls": discovery_calls, "pricing_calls": pricing_calls,
            "errors": n_errors, "empty": n_empty, "skipped": n_skipped,
            "retries": n_retries, "warnings": warnings}


def _build_refresh_msg(r: dict) -> str:
    """Compose the success/info banner shown after a refresh completes."""
    total = r["discovery_calls"] + r["pricing_calls"]
    if r["rows"]:
        msg   = (f"Fetched {r['rows']} roundtrip prices · "
                 f"{r['discovery_calls']} discovery + {r['pricing_calls']} pricing calls.")
        extra = []
        if r["retries"]: extra.append(f"{r['retries']} retries")
        if r["empty"]:   extra.append(f"{r['empty']} empty")
        if r["errors"]:  extra.append(f"{r['errors']} errors")
        if r["skipped"]: extra.append(f"{r['skipped']} skipped (near-term)")
        if extra:
            msg += "  (" + ", ".join(extra) + ")"
        return msg
    if total == 0:
        if r["skipped"]:
            return (f"No prices fetched — all {r['skipped']} weekends skipped "
                    "(departures too near-term).")
        return "No upcoming weekends to fetch — season may be over."
    return ("No roundtrip prices returned — discovery or pricing came back empty. "
            "Check your SERPAPI_KEY / quota.")


# ── Header ────────────────────────────────────────────────────────────────────
h_col, r_col = st.columns([5, 1])
with h_col:
    st.markdown(
        '<div class="hero-title">Moldova Flight Tracker</div>'
        '<div class="hero-sub">Departing <b>RMO</b> &middot; Chi&#537;in&#259;u'
        '<span class="hero-dot"></span>'
        f'Last updated <b>{get_last_updated_str()}</b></div>',
        unsafe_allow_html=True,
    )
with r_col:
    st.write("")  # push button down to align with title baseline
    refresh_btn = st.button(
        "Refresh prices", type="primary", use_container_width=True
    )

st.divider()

# ── Handle refresh click ───────────────────────────────────────────────────────
if refresh_btn:
    with st.spinner("Fetching roundtrip prices — this may take a few minutes"):
        result = do_refresh(debug=debug_mode)
        # Store results here, inside the spinner, BEFORE any further st.* call:
        # plain session_state assignment is not an interruption point, so a rerun
        # queued by a sidebar click can't drop the just-completed refresh.
        st.session_state.refresh_msg      = _build_refresh_msg(result)
        st.session_state.refresh_warnings = result["warnings"]
    st.rerun()

if st.session_state.refresh_msg:
    st.success(st.session_state.refresh_msg)
    st.session_state.refresh_msg = None
for _warn in st.session_state.refresh_warnings:
    st.warning(_warn)
st.session_state.refresh_warnings = []


# ── Active Fridays (used by both tabs) ────────────────────────────────────────
active_fridays = get_rolling_fridays()


# ── Common data for history/timing tabs (loaded once, used in both) ───────────
all_destinations = get_all_destinations()

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_overview, tab_detail, tab_history, tab_timing = st.tabs(
    ["Overview", "Weekend Detail", "Price History", "Booking Timing"]
)


# ─────────────────────────────────────────────────────────────────────────────
# Tab 1 — Overview
# ─────────────────────────────────────────────────────────────────────────────
with tab_overview:
    if not active_fridays:
        st.info("Season is over (past Sep 1). No weekends to display.")
    else:
        cols = st.columns(3, gap="medium")

        for idx, fri in enumerate(active_fridays):
            best      = cheapest_for_friday(fri, wl, max_price, max_stops, country_filter)
            date_lbl  = date_range_label(fri, wl)

            if best is None:
                # Distinguish 'no data at all' from 'data exists for other windows'.
                other = [w for w in windows_with_data_for_friday(fri.isoformat())
                         if w != wl]
                if other:
                    avail = ", ".join(WINDOW_DISPLAY[w] for w in other)
                    html = (
                        f'<div class="pc t-partial">'
                        f'<div class="pc-head"><span class="c-date">{date_lbl}</span>'
                        f'<span class="badge b-info">Other windows</span></div>'
                        f'<div class="c-price is-empty">&mdash;</div>'
                        f'<div class="c-sub">No {WINDOW_DISPLAY[wl]} data. '
                        f'Available for <strong>{avail}</strong> &mdash; '
                        f'switch window type in the sidebar.</div>'
                        f'</div>'
                    )
                else:
                    html = (
                        f'<div class="pc t-empty">'
                        f'<div class="pc-head"><span class="c-date">{date_lbl}</span></div>'
                        f'<div class="c-price is-empty">&mdash;</div>'
                        f'<div class="c-sub">No data yet &mdash; click Refresh.</div>'
                        f'</div>'
                    )
            else:
                tier    = price_tier(best["price_usd_2pax"])
                price   = best["price_usd_2pax"]
                city    = best["destination_city"]
                country = best["destination_country"]
                airline = best.get("airline") or ""
                stops_n = best.get("outbound_stops", 0)
                stops_s = "Nonstop" if stops_n == 0 else f"{stops_n} stop"
                t_badge = f'<span class="badge b-{tier}">{TIER_LABEL[tier]}</span>'
                w_badge = '<span class="badge b-wizz">W</span>' if is_wizz(airline) else ""
                airline_html = f'{airline}{w_badge}' if airline else w_badge
                meta    = " &middot; ".join(p for p in (airline_html, stops_s) if p)
                html = (
                    f'<div class="pc t-{tier}">'
                    f'<div class="pc-head"><span class="c-date">{date_lbl}</span>'
                    f'{t_badge}</div>'
                    f'<div class="c-price">${price:,.0f}</div>'
                    f'<div class="c-dest">{city}<span class="muted">, {country}</span></div>'
                    f'<div class="c-sub">{meta}</div>'
                    f'</div>'
                )

            with cols[idx % 3]:
                st.markdown(html, unsafe_allow_html=True)

        # Legend
        st.divider()
        lcols = st.columns(4, gap="small")
        lcols[0].markdown(
            '<div class="legend-chip"><span class="legend-dot" style="background:#34d399">'
            '</span>Great Deal &middot; under $400</div>', unsafe_allow_html=True
        )
        lcols[1].markdown(
            '<div class="legend-chip"><span class="legend-dot" style="background:#fbbf24">'
            '</span>Good Option &middot; $400 &ndash; $499</div>', unsafe_allow_html=True
        )
        lcols[2].markdown(
            '<div class="legend-chip"><span class="legend-dot" style="background:#7c8aa3">'
            '</span>OK &middot; $500 &ndash; $999</div>', unsafe_allow_html=True
        )
        lcols[3].markdown(
            '<div class="legend-chip"><span class="legend-dot" style="background:#f472b6">'
            '</span>W &middot; Wizz Air member discount</div>', unsafe_allow_html=True
        )


# ─────────────────────────────────────────────────────────────────────────────
# Tab 2 — Weekend Detail
# ─────────────────────────────────────────────────────────────────────────────
with tab_detail:
    if not active_fridays:
        st.info("No active weekends in the rolling window.")
    else:
        fri_map = {
            fri.isoformat(): date_range_label(fri, wl) for fri in active_fridays
        }
        sel_fri = st.selectbox(
            "Select weekend",
            options=list(fri_map.keys()),
            format_func=lambda k: fri_map[k],
        )

        if sel_fri:
            conn   = get_conn()
            detail = get_flights_for_window(sel_fri, wl, conn=conn)
            conn.close()

            # Apply sidebar filters
            detail = [
                r for r in detail
                if r["price_usd_2pax"] <= max_price
                and (max_stops is None or r["outbound_stops"] <= max_stops)
                and (not country_filter or r["destination_country"] in country_filter)
            ]

            if not detail:
                st.info(
                    "No flights match current filters for this weekend. "
                    "Try widening your filters or clicking Refresh."
                )
            else:
                df = pd.DataFrame(detail)

                # Build display dataframe
                display = pd.DataFrame({
                    "City":         df["destination_city"],
                    "Country":      df["destination_country"],
                    "IATA":         df["destination_iata"],
                    "Price":        df["price_usd_2pax"],
                    "Tier":         df["price_usd_2pax"].apply(
                                        lambda p: TIER_LABEL[price_tier(p)]
                                    ),
                    "Stops":        df["outbound_stops"].apply(
                                        lambda s: "Nonstop" if s == 0 else f"{s} stop"
                                    ),
                    "Duration":     df["outbound_duration_min"].apply(fmt_duration),
                    "Airline":      df["airline"].fillna("—"),
                    "Wizz":         df["airline"].apply(
                                        lambda a: "✦" if is_wizz(a) else ""
                                    ),
                })

                st.caption(
                    f"**{len(display)}** destinations  &middot;  "
                    f"{WINDOW_DISPLAY[wl]}  &middot;  sorted cheapest first"
                )

                st.dataframe(
                    display,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "City":     st.column_config.TextColumn("City",     width="medium"),
                        "Country":  st.column_config.TextColumn("Country",  width="medium"),
                        "IATA":     st.column_config.TextColumn("IATA",     width="small"),
                        "Price":    st.column_config.NumberColumn(
                                        "Price (2 pax)", format="$%d", width="small"
                                    ),
                        "Tier":     st.column_config.TextColumn("Tier",     width="small"),
                        "Stops":    st.column_config.TextColumn("Stops",    width="small"),
                        "Duration": st.column_config.TextColumn("Duration", width="small"),
                        "Airline":  st.column_config.TextColumn("Airline",  width="medium"),
                        "Wizz":     st.column_config.TextColumn("W",        width="small"),
                    },
                )
                st.caption(
                    "W = Wizz Air flight. "
                    "Wizz Air membership discount is not reflected in displayed prices. "
                    "All prices: 2 people, roundtrip, USD."
                )


# ─────────────────────────────────────────────────────────────────────────────
# Tab 3 — Price History
# ─────────────────────────────────────────────────────────────────────────────
with tab_history:
    if not all_destinations:
        st.info(
            "No price history yet. "
            "Click **Refresh prices** to fetch data, then check back after a few more refreshes."
        )
    else:
        dc, wc = st.columns(2)

        with dc:
            h_iata = st.selectbox(
                "Destination",
                options=[d["iata"] for d in all_destinations],
                format_func=lambda iata: next(
                    f"{d['city']}, {d['country']} ({iata})"
                    for d in all_destinations if d["iata"] == iata
                ),
                key="h_dest",
            )

        # Weekend options depend on the selected destination + current window type
        dest_weekends = get_weekends_for_dest(h_iata, wl)
        weekend_opts  = ["__all__"] + dest_weekends

        with wc:
            h_weekend = st.selectbox(
                "Weekend",
                options=weekend_opts,
                format_func=lambda w: "All weekends" if w == "__all__" else
                    date_range_label(date.fromisoformat(w), wl),
                key="h_weekend",
            )

        fri_filter = None if h_weekend == "__all__" else h_weekend
        conn = get_conn()
        history = get_price_history(h_iata, fri_filter, conn=conn)
        conn.close()

        # Filter to the sidebar window type so data is consistent
        history = [r for r in history if r["window_type"] == wl]

        dest_label = next(
            f"{d['city']}, {d['country']} ({h_iata})"
            for d in all_destinations if d["iata"] == h_iata
        )

        if not history:
            st.info(
                f"No {WINDOW_DISPLAY[wl]} data for {dest_label} yet. "
                "Try a different window type or destination, or click Refresh."
            )
        else:
            df_h = pd.DataFrame(history)
            df_h["fetched_at"] = pd.to_datetime(df_h["fetched_at"])

            if df_h["fetched_at"].nunique() < 2:
                st.info(
                    "Not enough data yet to draw a trend — "
                    "check back after a few more refreshes."
                )
            else:
                if fri_filter is None:
                    # One line per weekend
                    df_h["Weekend"] = df_h["window_fri_date"].apply(
                        lambda w: date_range_label(date.fromisoformat(w), wl)
                    )
                    # Min price per (timestamp, weekend) — handles multiple rows per fetch
                    grp = (
                        df_h.groupby(["fetched_at", "Weekend"])["price_usd_2pax"]
                        .min()
                        .reset_index()
                    )
                    pivot = grp.pivot_table(
                        index="fetched_at",
                        columns="Weekend",
                        values="price_usd_2pax",
                    ).sort_index()
                    st.line_chart(
                        pivot,
                        x_label="Fetch date",
                        y_label="Price (USD, 2 pax)",
                    )
                else:
                    # Single line — min price per fetch timestamp
                    df_single = (
                        df_h.groupby("fetched_at")["price_usd_2pax"]
                        .min()
                        .rename(dest_label)
                        .sort_index()
                        .to_frame()
                    )
                    st.line_chart(
                        df_single,
                        x_label="Fetch date",
                        y_label="Price (USD, 2 pax)",
                    )

                st.caption(
                    f"Price tiers: "
                    f"Great Deal < ${PRICE_GREAT:,}  |  "
                    f"Good Option ${PRICE_GREAT:,}–${PRICE_GOOD - 1:,}  |  "
                    f"OK ${PRICE_GOOD:,}–${PRICE_CAP - 1:,}  |  "
                    f"All prices: 2 people, roundtrip, USD."
                )


# ─────────────────────────────────────────────────────────────────────────────
# Tab 4 — Booking Timing
# ─────────────────────────────────────────────────────────────────────────────
with tab_timing:
    st.caption(
        "How does price change based on how far in advance you check? "
        "Each dot = one price observation. "
        "Higher x = checked further ahead of departure."
    )

    if not all_destinations:
        st.info(
            "No data yet. "
            "Click **Refresh prices** to fetch data, then check back after a few more refreshes."
        )
    else:
        t_iata = st.selectbox(
            "Destination",
            options=[d["iata"] for d in all_destinations],
            format_func=lambda iata: next(
                f"{d['city']}, {d['country']} ({iata})"
                for d in all_destinations if d["iata"] == iata
            ),
            key="t_dest",
        )

        conn = get_conn()
        timing_rows = get_price_history(t_iata, conn=conn)
        conn.close()

        # Filter to the sidebar window type
        timing_rows = [r for r in timing_rows if r["window_type"] == wl]

        t_label = next(
            f"{d['city']}, {d['country']} ({t_iata})"
            for d in all_destinations if d["iata"] == t_iata
        )

        if not timing_rows:
            st.info(
                f"No {WINDOW_DISPLAY[wl]} data for {t_label} yet. "
                "Try a different window type or destination, or click Refresh."
            )
        else:
            df_t = pd.DataFrame(timing_rows)
            df_t["fetched_at"]   = pd.to_datetime(df_t["fetched_at"])
            df_t["outbound_date"] = pd.to_datetime(df_t["outbound_date"])
            df_t["days_before"]  = (
                df_t["outbound_date"].dt.normalize()
                - df_t["fetched_at"].dt.normalize()
            ).dt.days

            # Drop rows where the departure had already passed at fetch time
            df_t = df_t[df_t["days_before"] >= 0]

            if df_t["fetched_at"].nunique() < 2:
                st.info(
                    "Not enough data yet to show a booking-timing trend — "
                    "check back after a few more refreshes."
                )
            else:
                df_t["Weekend"] = df_t["window_fri_date"].apply(
                    lambda w: date_range_label(date.fromisoformat(w), wl)
                )

                scatter_df = df_t.rename(columns={
                    "days_before":   "Days before departure",
                    "price_usd_2pax": "Price (USD, 2 pax)",
                    "Weekend":        "Weekend",
                })[["Days before departure", "Price (USD, 2 pax)", "Weekend"]]

                st.scatter_chart(
                    scatter_df,
                    x="Days before departure",
                    y="Price (USD, 2 pax)",
                    color="Weekend",
                    x_label="Days before departure",
                    y_label="Price (USD, 2 pax)",
                )

                # Quick interpretation hint
                min_row = df_t.loc[df_t["price_usd_2pax"].idxmin()]
                st.caption(
                    f"Cheapest observation for {t_label}: "
                    f"**${min_row['price_usd_2pax']:,.0f}** checked "
                    f"**{int(min_row['days_before'])} days** before departure "
                    f"({min_row['window_fri_date']}, {WINDOW_DISPLAY[wl]}).  "
                    f"All prices: 2 people, roundtrip, USD."
                )
