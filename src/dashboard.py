"""Streamlit dashboard for Moldova Flight Tracker.

Run from the project root:
    streamlit run src/dashboard.py
"""
import sys
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
from src.fetcher import fetch_explore, parse_explore_results

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


# ── CSS ──────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* Weekend cards */
.pc            { border-radius:8px; padding:16px 18px 12px; margin-bottom:8px; line-height:1.35; }
.t-great       { background:#d4edda; border-left:5px solid #28a745; }
.t-good        { background:#fff3cd; border-left:5px solid #ffc107; }
.t-ok          { background:#f8f9fa; border-left:5px solid #adb5bd; }
.t-empty       { background:#f1f3f4; border-left:5px solid #dee2e6; color:#999; }
.c-date        { font-size:.82em; color:#666; margin:0 0 3px; }
.c-price       { font-size:1.95em; font-weight:700; margin:2px 0 5px; line-height:1.05; }
.c-dest        { font-size:1.05em; margin:0 0 4px; }
.c-sub         { font-size:.82em; color:#555; margin:0; }
/* Badges */
.badge         { display:inline-block; padding:1px 7px; border-radius:4px;
                 font-size:.68em; font-weight:700; vertical-align:middle; margin-left:5px; }
.b-great       { background:#28a745; color:#fff; }
.b-good        { background:#ffc107; color:#333; }
.b-ok          { background:#6c757d; color:#fff; }
.b-wizz        { background:#c5166a; color:#fff; }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
if "refresh_msg" not in st.session_state:
    st.session_state.refresh_msg = None

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


# ── Refresh ───────────────────────────────────────────────────────────────────
def do_refresh() -> dict:
    today      = date.today()
    fetched_at = datetime.utcnow().isoformat()
    windows    = [w for w in get_all_windows(today) if w["outbound_date"] >= today]

    if not windows:
        return {"rows": 0, "calls": 0, "errors": 0}

    all_rows, n_errors = [], 0
    prog = st.progress(0)

    for i, w in enumerate(windows, 1):
        prog.progress(
            i / len(windows),
            text=(
                f"Fetching {WINDOW_DISPLAY[w['label']]}  "
                f"{fmt_day(w['outbound_date'])} - {fmt_day(w['return_date'])}"
            ),
        )
        try:
            raw  = fetch_explore(w["outbound_date"], w["return_date"])
            rows = parse_explore_results(raw, w["fri_date"], w["label"], fetched_at)
            all_rows.extend(rows)
        except Exception as exc:
            n_errors += 1
            st.warning(f"Error ({w['label']} {w['fri_date']}): {exc}")

    prog.empty()

    if all_rows:
        conn = get_conn()
        insert_flights(all_rows, conn=conn)
        log_fetch(
            {
                "fetched_at":       fetched_at,
                "weekends_fetched": len({w["fri_date"] for w in windows}),
                "calls_made":       len(windows) - n_errors,
                "status":           "success" if not n_errors else "partial",
                "notes":            f"{n_errors} errors" if n_errors else None,
            },
            conn=conn,
        )
        conn.close()

    return {"rows": len(all_rows), "calls": len(windows), "errors": n_errors}


# ── Header ────────────────────────────────────────────────────────────────────
h_col, r_col = st.columns([5, 1])
with h_col:
    st.title("Moldova Flight Tracker")
    st.caption(
        f"Departing from **RMO** (Chisinau)  |  "
        f"Last updated: **{get_last_updated_str()}**"
    )
with r_col:
    st.write("")  # push button down to align with title baseline
    refresh_btn = st.button(
        "Refresh prices", type="primary", use_container_width=True
    )

st.divider()

# ── Handle refresh click ───────────────────────────────────────────────────────
if refresh_btn:
    result = do_refresh()
    if result["rows"]:
        msg = f"Fetched {result['rows']} prices across {result['calls']} API calls."
        if result["errors"]:
            msg += f" ({result['errors']} errors — see warnings above.)"
    elif result["calls"] == 0:
        msg = "No upcoming windows to fetch — season may be over."
    else:
        msg = "No prices returned. Check your SERPAPI_KEY in .env."
    st.session_state.refresh_msg = msg
    st.rerun()

if st.session_state.refresh_msg:
    st.success(st.session_state.refresh_msg)
    st.session_state.refresh_msg = None


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
                html = (
                    f'<div class="pc t-empty">'
                    f'<p class="c-date">{date_lbl}</p>'
                    f'<p class="c-price" style="font-size:1.4em;color:#ccc;">—</p>'
                    f'<p class="c-sub">No data — click Refresh</p>'
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
                html = (
                    f'<div class="pc t-{tier}">'
                    f'<p class="c-date">{date_lbl}</p>'
                    f'<p class="c-price">${price:,.0f}{t_badge}</p>'
                    f'<p class="c-dest"><strong>{city}</strong>, {country}</p>'
                    f'<p class="c-sub">{airline}{w_badge} &middot; {stops_s}</p>'
                    f'</div>'
                )

            with cols[idx % 3]:
                st.markdown(html, unsafe_allow_html=True)

        # Legend
        st.divider()
        lcols = st.columns(4, gap="small")
        lcols[0].markdown(
            '<span class="badge b-great" style="font-size:.9em;">Great Deal</span>'
            '&nbsp; under $400', unsafe_allow_html=True
        )
        lcols[1].markdown(
            '<span class="badge b-good" style="font-size:.9em;">Good Option</span>'
            '&nbsp; $400 - $499', unsafe_allow_html=True
        )
        lcols[2].markdown(
            '<span class="badge b-ok" style="font-size:.9em;">OK</span>'
            '&nbsp; $500 - $999', unsafe_allow_html=True
        )
        lcols[3].markdown(
            '<span class="badge b-wizz" style="font-size:.9em;">W</span>'
            '&nbsp; Wizz Air (membership discount applies)', unsafe_allow_html=True
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
                    "City":           df["destination_city"],
                    "Country":        df["destination_country"],
                    "IATA":           df["destination_iata"],
                    "Price (2 pax)":  df["price_usd_2pax"].apply(lambda p: f"${p:,.0f}"),
                    "Tier":           df["price_usd_2pax"].apply(
                                          lambda p: TIER_LABEL[price_tier(p)]
                                      ),
                    "Stops":          df["outbound_stops"].apply(
                                          lambda s: "Nonstop" if s == 0 else str(s)
                                      ),
                    "Duration":       df["outbound_duration_min"].apply(fmt_duration),
                    "Airline":        df["airline"].fillna("-"),
                    "W":              df["airline"].apply(
                                          lambda a: "W" if is_wizz(a) else ""
                                      ),
                })

                st.caption(
                    f"{len(display)} destinations  |  "
                    f"{WINDOW_DISPLAY[wl]}  |  sorted cheapest first"
                )

                st.dataframe(
                    display,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "IATA":           st.column_config.TextColumn("IATA",           width="small"),
                        "Price (2 pax)":  st.column_config.TextColumn("Price (2 pax)",  width="small"),
                        "Tier":           st.column_config.TextColumn("Tier",           width="medium"),
                        "Stops":          st.column_config.TextColumn("Stops",          width="small"),
                        "Duration":       st.column_config.TextColumn("Duration",       width="small"),
                        "W":              st.column_config.TextColumn("W",              width="small"),
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
