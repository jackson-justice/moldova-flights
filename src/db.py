"""SQLite helpers: schema creation, insert, and query functions."""
import sqlite3
from datetime import datetime
from pathlib import Path

from src.config import SCHEMA_VERSION

DB_PATH = Path(__file__).parent.parent / "data" / "flights.db"

# Column DDL for the flights table — single source of truth, reused by both the
# normal create and the migration rebuild (CREATE TABLE flights_new ...).
_FLIGHTS_COLS = """
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at            DATETIME NOT NULL,
    window_fri_date       DATE NOT NULL,        -- 'fri_sun', 'thu_sun', 'fri_mon', 'thu_mon'
    window_type           TEXT NOT NULL,
    outbound_date         DATE NOT NULL,
    return_date           DATE NOT NULL,
    destination_iata      TEXT NOT NULL,
    destination_city      TEXT NOT NULL,
    destination_country   TEXT NOT NULL,
    price_usd_2pax        REAL NOT NULL,        -- total for 2 people, roundtrip, USD
    outbound_stops        INTEGER NOT NULL,
    return_stops          INTEGER,              -- nullable: v1 google_flights pricing has no return-leg detail
    outbound_duration_min INTEGER,
    return_duration_min   INTEGER,
    airline               TEXT,
    booking_link          TEXT,
    price_source          TEXT,                 -- 'google_flights' | 'explore' | NULL (legacy)
    departure_token       TEXT
"""

# Ordered column names — keep in sync with _FLIGHTS_COLS. Used by the migration
# copy to select the intersection of old and new columns.
_FLIGHTS_COLUMN_NAMES = [
    "id", "fetched_at", "window_fri_date", "window_type", "outbound_date",
    "return_date", "destination_iata", "destination_city", "destination_country",
    "price_usd_2pax", "outbound_stops", "return_stops", "outbound_duration_min",
    "return_duration_min", "airline", "booking_link", "price_source",
    "departure_token",
]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS flights (
{_FLIGHTS_COLS}
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at       DATETIME NOT NULL,
    weekends_fetched INTEGER,
    calls_made       INTEGER,
    status           TEXT,                      -- 'success', 'error', 'rate_limited'
    notes            TEXT
);

CREATE INDEX IF NOT EXISTS idx_flights_window
    ON flights (window_fri_date, window_type);

CREATE INDEX IF NOT EXISTS idx_flights_destination
    ON flights (destination_iata, fetched_at);
"""

_INSERT_FLIGHT = """
INSERT INTO flights (
    fetched_at, window_fri_date, window_type,
    outbound_date, return_date,
    destination_iata, destination_city, destination_country,
    price_usd_2pax, outbound_stops, return_stops,
    outbound_duration_min, return_duration_min,
    airline, booking_link, price_source, departure_token
) VALUES (
    :fetched_at, :window_fri_date, :window_type,
    :outbound_date, :return_date,
    :destination_iata, :destination_city, :destination_country,
    :price_usd_2pax, :outbound_stops, :return_stops,
    :outbound_duration_min, :return_duration_min,
    :airline, :booking_link, :price_source, :departure_token
)
"""

# Optional flight columns default to NULL when a row dict omits them, so both the
# Explore parser (no price_source/departure_token) and the google_flights parser
# (full set) can be inserted through the same statement.
_FLIGHT_OPTIONAL_DEFAULTS = {
    "return_stops":          None,
    "outbound_duration_min": None,
    "return_duration_min":   None,
    "airline":               None,
    "booking_link":          None,
    "price_source":          None,
    "departure_token":       None,
}

_INSERT_LOG = """
INSERT INTO fetch_log (fetched_at, weekends_fetched, calls_made, status, notes)
VALUES (:fetched_at, :weekends_fetched, :calls_made, :status, :notes)
"""


def get_conn(path: Path = None) -> sqlite3.Connection:
    """Return an open connection. Caller is responsible for closing it."""
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate_flights(conn: sqlite3.Connection) -> None:
    """
    Rebuild an existing legacy `flights` table to the current schema using the
    create-new / copy-compatible-columns / drop / rename pattern.

    No-op when there is no flights table (fresh DB) or it already has the new
    columns. Indexes are recreated afterwards by the _SCHEMA executescript.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='flights'"
    ).fetchone()
    if not exists:
        return

    cols = {r[1] for r in conn.execute("PRAGMA table_info(flights)")}
    if {"price_source", "departure_token"} <= cols:
        return  # already on the new schema

    conn.execute("DROP TABLE IF EXISTS flights_new")
    conn.execute(f"CREATE TABLE flights_new ({_FLIGHTS_COLS})")

    # Copy only columns present in both old and new schemas.
    compatible = [c for c in _FLIGHTS_COLUMN_NAMES if c in cols]
    collist = ", ".join(compatible)
    conn.execute(
        f"INSERT INTO flights_new ({collist}) SELECT {collist} FROM flights"
    )
    conn.execute("DROP TABLE flights")
    conn.execute("ALTER TABLE flights_new RENAME TO flights")


def init_db(path: Path = None) -> None:
    """
    Create tables/indexes if missing, and migrate the flights table when the DB's
    schema version is behind SCHEMA_VERSION.
    """
    with get_conn(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            _migrate_flights(conn)                 # rebuild legacy table if present
            conn.executescript(_SCHEMA)            # create any missing tables/indexes
            conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        else:
            conn.executescript(_SCHEMA)            # idempotent ensure-exists


def insert_flights(rows: list, conn: sqlite3.Connection = None) -> int:
    """
    Bulk-insert flight rows. Each row must be a dict matching the flights schema.
    Returns the number of rows inserted.
    """
    if not rows:
        return 0

    # Fill any omitted optional columns with NULL so the Explore parser (which has
    # no price_source/departure_token) and the google_flights parser both insert.
    rows = [{**_FLIGHT_OPTIONAL_DEFAULTS, **r} for r in rows]

    close = conn is None
    if close:
        conn = get_conn()

    try:
        conn.executemany(_INSERT_FLIGHT, rows)
        conn.commit()
        return len(rows)
    finally:
        if close:
            conn.close()


def log_fetch(record: dict, conn: sqlite3.Connection = None) -> None:
    """
    Insert one row into fetch_log.
    record keys: fetched_at, weekends_fetched, calls_made, status, notes
    """
    record.setdefault("fetched_at", datetime.utcnow().isoformat())
    record.setdefault("weekends_fetched", None)
    record.setdefault("calls_made", None)
    record.setdefault("status", "success")
    record.setdefault("notes", None)

    close = conn is None
    if close:
        conn = get_conn()

    try:
        conn.execute(_INSERT_LOG, record)
        conn.commit()
    finally:
        if close:
            conn.close()


def get_cheapest_per_weekend(
    window_type: str = "fri_sun",
    conn: sqlite3.Connection = None,
) -> list:
    """
    Return the single cheapest price per upcoming weekend for the given window_type,
    using only the most recent fetch snapshot for each (window_fri_date, destination).
    Results are ordered by window_fri_date ascending.
    """
    sql = """
    WITH latest AS (
        SELECT
            window_fri_date,
            destination_iata,
            MAX(fetched_at) AS last_fetch
        FROM flights
        WHERE window_type = ?
        GROUP BY window_fri_date, destination_iata
    ),
    current_prices AS (
        SELECT f.*
        FROM flights f
        JOIN latest l
          ON  f.window_fri_date  = l.window_fri_date
          AND f.destination_iata = l.destination_iata
          AND f.fetched_at       = l.last_fetch
        WHERE f.window_type = ?
    )
    SELECT
        window_fri_date,
        outbound_date,
        return_date,
        destination_iata,
        destination_city,
        destination_country,
        MIN(price_usd_2pax) AS price_usd_2pax,
        outbound_stops,
        return_stops,
        airline,
        booking_link
    FROM current_prices
    GROUP BY window_fri_date
    ORDER BY window_fri_date
    """
    close = conn is None
    if close:
        conn = get_conn()

    try:
        cur = conn.execute(sql, (window_type, window_type))
        return [dict(r) for r in cur.fetchall()]
    finally:
        if close:
            conn.close()


def get_flights_for_window(
    fri_date: str,
    window_type: str = "fri_sun",
    limit: int = 200,
    conn: sqlite3.Connection = None,
) -> list:
    """
    Return all flights for a specific weekend + window_type from the latest fetch,
    sorted cheapest first. fri_date is a 'YYYY-MM-DD' string.
    """
    sql = """
    WITH latest_fetch AS (
        SELECT MAX(fetched_at) AS ts
        FROM flights
        WHERE window_fri_date = ? AND window_type = ?
    )
    SELECT f.*
    FROM flights f, latest_fetch lf
    WHERE f.window_fri_date = ?
      AND f.window_type     = ?
      AND f.fetched_at      = lf.ts
    ORDER BY f.price_usd_2pax
    LIMIT ?
    """
    close = conn is None
    if close:
        conn = get_conn()

    try:
        cur = conn.execute(sql, (fri_date, window_type, fri_date, window_type, limit))
        return [dict(r) for r in cur.fetchall()]
    finally:
        if close:
            conn.close()


def get_price_history(
    destination_iata: str,
    window_fri_date: str = None,
    conn: sqlite3.Connection = None,
) -> list:
    """
    Return all price snapshots for a destination, ordered by fetched_at ascending.
    Optionally filter to a single weekend (window_fri_date = 'YYYY-MM-DD').
    Used for the booking-timing and price-history charts.
    """
    if window_fri_date:
        sql = """
        SELECT fetched_at, window_fri_date, window_type, outbound_date,
               price_usd_2pax, outbound_stops, return_stops, airline
        FROM flights
        WHERE destination_iata = ? AND window_fri_date = ?
        ORDER BY fetched_at
        """
        args = (destination_iata, window_fri_date)
    else:
        sql = """
        SELECT fetched_at, window_fri_date, window_type, outbound_date,
               price_usd_2pax, outbound_stops, return_stops, airline
        FROM flights
        WHERE destination_iata = ?
        ORDER BY fetched_at
        """
        args = (destination_iata,)

    close = conn is None
    if close:
        conn = get_conn()

    try:
        cur = conn.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]
    finally:
        if close:
            conn.close()
