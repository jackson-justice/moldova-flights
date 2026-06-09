"""SQLite helpers: schema creation, insert, and query functions."""
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "flights.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flights (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at            DATETIME NOT NULL,
    window_fri_date       DATE NOT NULL,
    window_type           TEXT NOT NULL,        -- 'fri_sun', 'thu_sun', 'fri_mon', 'thu_mon'
    outbound_date         DATE NOT NULL,
    return_date           DATE NOT NULL,
    destination_iata      TEXT NOT NULL,
    destination_city      TEXT NOT NULL,
    destination_country   TEXT NOT NULL,
    price_usd_2pax        REAL NOT NULL,        -- total for 2 people, roundtrip, USD
    outbound_stops        INTEGER NOT NULL,
    return_stops          INTEGER NOT NULL,
    outbound_duration_min INTEGER,
    return_duration_min   INTEGER,
    airline               TEXT,
    booking_link          TEXT
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
    airline, booking_link
) VALUES (
    :fetched_at, :window_fri_date, :window_type,
    :outbound_date, :return_date,
    :destination_iata, :destination_city, :destination_country,
    :price_usd_2pax, :outbound_stops, :return_stops,
    :outbound_duration_min, :return_duration_min,
    :airline, :booking_link
)
"""

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


def init_db(path: Path = None) -> None:
    """Create tables and indexes if they don't already exist."""
    with get_conn(path) as conn:
        conn.executescript(_SCHEMA)


def insert_flights(rows: list, conn: sqlite3.Connection = None) -> int:
    """
    Bulk-insert flight rows. Each row must be a dict matching the flights schema.
    Returns the number of rows inserted.
    """
    if not rows:
        return 0

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
