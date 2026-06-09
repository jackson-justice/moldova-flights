from datetime import date, timedelta

from src.config import (
    HARD_STOP_DATE,
    ROLLING_WINDOW_SIZE,
    SKIP_RANGE_START,
    SKIP_RANGE_END,
    WINDOW_TYPES,
)


def get_rolling_fridays(today: date = None) -> list:
    """
    Return up to ROLLING_WINDOW_SIZE upcoming Friday dates.

    Expiry rule: a weekend is live when today <= its Friday.  If today IS the
    Friday, it's still shown (same-day booking is possible).  Once today is
    Saturday or later, that Friday has expired and is no longer returned.

    Skip rule: any Friday whose date falls within [SKIP_RANGE_START,
    SKIP_RANGE_END] is permanently excluded (Hungary trip, Aug 18-22).

    Hard stop: no Friday on or after HARD_STOP_DATE (Sep 1) is ever returned.
    """
    if today is None:
        today = date.today()

    # weekday(): Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    wd = today.weekday()
    if wd <= 4:
        days_to_fri = 4 - wd      # 0 when today is Friday
    else:
        days_to_fri = 11 - wd     # Sat→6 days, Sun→5 days

    candidate = today + timedelta(days=days_to_fri)

    fridays = []
    while len(fridays) < ROLLING_WINDOW_SIZE:
        if candidate >= HARD_STOP_DATE:
            break
        if not (SKIP_RANGE_START <= candidate <= SKIP_RANGE_END):
            fridays.append(candidate)
        candidate += timedelta(weeks=1)

    return fridays


def get_window_dates(fri_date: date, window_type: dict) -> tuple:
    """Return (outbound_date, return_date) for a window type anchored to fri_date."""
    outbound  = fri_date - timedelta(days=window_type["out_offset"])
    return_dt = fri_date + timedelta(days=window_type["ret_offset"])
    return outbound, return_dt


def get_all_windows(today: date = None) -> list:
    """
    Return one dict per (friday, window_type) combination — the full set of API
    calls the scheduler should make for the current rolling window.

    Each dict: {fri_date, label, outbound_date, return_date}
    """
    fridays = get_rolling_fridays(today)
    windows = []
    for fri in fridays:
        for wt in WINDOW_TYPES:
            outbound, ret = get_window_dates(fri, wt)
            windows.append({
                "fri_date":      fri,
                "label":         wt["label"],
                "outbound_date": outbound,
                "return_date":   ret,
            })
    return windows
