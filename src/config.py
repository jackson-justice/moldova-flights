from datetime import date

# --- Budget thresholds: total for 2 people, roundtrip, USD ---
PRICE_CAP   = 1000   # hard ceiling — nothing above is stored or shown
PRICE_GREAT =  400   # "Great deal" (green)
PRICE_GOOD  =  500   # "Good option" (amber); above this up to cap = "OK" (gray)

# --- Hard stop: no data for any weekend on or after this date ---
HARD_STOP_DATE = date(2026, 9, 1)

# --- Permanently skipped weekend: Hungary trip Aug 18-22 ---
# The Friday of that week (Aug 21) is what anchors the block in the rolling window.
# Any Friday within this inclusive range is excluded from all API calls and display.
SKIP_RANGE_START = date(2026, 8, 18)
SKIP_RANGE_END   = date(2026, 8, 22)

# --- Departure airport ---
ORIGIN_IATA = "RMO"

# --- Rolling window size ---
ROLLING_WINDOW_SIZE = 6

# --- Window type definitions ---
# out_offset: days before the anchor Friday that the outbound flight departs
# ret_offset: days after the anchor Friday that the return flight lands
WINDOW_TYPES = [
    {"label": "fri_sun", "out_offset": 0, "ret_offset": 2},  # Fri → Sun (highest priority)
    {"label": "thu_sun", "out_offset": 1, "ret_offset": 2},  # Thu → Sun
    {"label": "fri_mon", "out_offset": 0, "ret_offset": 3},  # Fri → Mon
    {"label": "thu_mon", "out_offset": 1, "ret_offset": 3},  # Thu → Mon (lowest priority, weekly only)
]

# --- SerpApi parameters ---
SERPAPI_CURRENCY = "USD"
SERPAPI_LANGUAGE = "en"
SERPAPI_ADULTS   = 2

# --- Flight constraints ---
MAX_STOPS = 1
