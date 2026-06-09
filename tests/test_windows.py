"""Unit tests for src/windows.py rolling window logic."""
from datetime import date, timedelta

import pytest

from src.windows import get_rolling_fridays, get_window_dates, get_all_windows
from src.config import WINDOW_TYPES, ROLLING_WINDOW_SIZE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fri(s: str) -> date:
    """Parse 'YYYY-MM-DD' and assert it's a Friday."""
    d = date.fromisoformat(s)
    assert d.weekday() == 4, f"{s} is not a Friday"
    return d


# ---------------------------------------------------------------------------
# get_rolling_fridays — basic rolling window
# ---------------------------------------------------------------------------

class TestGetRollingFridays:

    def test_planning_doc_example(self):
        """Jun 9 (Tue) → [Jun 12, Jun 19, Jun 26, Jul 3, Jul 10, Jul 17]."""
        result = get_rolling_fridays(date(2026, 6, 9))
        expected = [
            date(2026, 6, 12),
            date(2026, 6, 19),
            date(2026, 6, 26),
            date(2026, 7,  3),
            date(2026, 7, 10),
            date(2026, 7, 17),
        ]
        assert result == expected

    def test_returns_six_fridays(self):
        result = get_rolling_fridays(date(2026, 6, 9))
        assert len(result) == ROLLING_WINDOW_SIZE

    def test_all_results_are_fridays(self):
        result = get_rolling_fridays(date(2026, 6, 9))
        for d in result:
            assert d.weekday() == 4, f"{d} is not a Friday"

    def test_results_are_one_week_apart(self):
        result = get_rolling_fridays(date(2026, 6, 9))
        for a, b in zip(result, result[1:]):
            assert (b - a).days == 7


# ---------------------------------------------------------------------------
# Expiry / same-day logic
# ---------------------------------------------------------------------------

class TestExpiry:

    def test_today_is_friday_still_shown(self):
        """If today IS the Friday, that weekend is still included."""
        today = date(2026, 6, 12)  # Friday
        assert today.weekday() == 4
        result = get_rolling_fridays(today)
        assert result[0] == today

    def test_saturday_friday_is_gone(self):
        """Once it's Saturday the previous Friday has expired; window moves on."""
        fri_jun12 = date(2026, 6, 12)
        sat_jun13 = date(2026, 6, 13)
        assert sat_jun13.weekday() == 5

        result = get_rolling_fridays(sat_jun13)
        assert fri_jun12 not in result
        assert result[0] == date(2026, 6, 19)

    def test_saturday_still_returns_six(self):
        result = get_rolling_fridays(date(2026, 6, 13))  # Saturday
        assert len(result) == ROLLING_WINDOW_SIZE

    def test_monday_shows_same_weeks_friday(self):
        """Monday should show the upcoming Friday of the same week."""
        today = date(2026, 6, 8)   # Monday
        assert today.weekday() == 0
        result = get_rolling_fridays(today)
        assert result[0] == date(2026, 6, 12)

    def test_thursday_shows_next_day_friday(self):
        today = date(2026, 6, 11)  # Thursday
        assert today.weekday() == 3
        result = get_rolling_fridays(today)
        assert result[0] == date(2026, 6, 12)

    def test_sunday_shows_next_friday(self):
        today = date(2026, 6, 14)  # Sunday
        assert today.weekday() == 6
        result = get_rolling_fridays(today)
        assert result[0] == date(2026, 6, 19)


# ---------------------------------------------------------------------------
# Aug 18-22 skip rule
# ---------------------------------------------------------------------------

class TestAugustSkip:

    AUG_21 = date(2026, 8, 21)  # The Friday that falls inside Aug 18-22

    def test_aug21_is_friday_in_skip_range(self):
        assert self.AUG_21.weekday() == 4

    def test_aug21_never_returned(self):
        """Aug 21 must not appear in any rolling window regardless of today."""
        for days_before in range(0, 50):
            today = self.AUG_21 - timedelta(days=days_before)
            result = get_rolling_fridays(today)
            assert self.AUG_21 not in result, f"Aug 21 appeared when today={today}"

    def test_aug28_included_after_skip(self):
        """The window after the skip (Aug 28) must appear next."""
        today = date(2026, 8, 10)  # Monday before the skip week
        result = get_rolling_fridays(today)
        assert date(2026, 8, 21) not in result
        assert date(2026, 8, 28) in result

    def test_window_before_skip_included(self):
        """Aug 14 (the Friday before the skip) should be present."""
        today = date(2026, 8, 10)
        result = get_rolling_fridays(today)
        assert date(2026, 8, 14) in result

    def test_aug21_friday_today_skip_still_applies(self):
        """Even if today IS Aug 21 (Friday), the skip removes it from the window."""
        result = get_rolling_fridays(self.AUG_21)
        assert self.AUG_21 not in result

    def test_returns_fewer_than_six_near_cutoff(self):
        """Near the end of the window, fewer than 6 results are expected."""
        today = date(2026, 8, 10)  # Aug 14, [skip 21], Aug 28, then Sep 4 stops
        result = get_rolling_fridays(today)
        assert len(result) < ROLLING_WINDOW_SIZE


# ---------------------------------------------------------------------------
# Sep 1 hard stop
# ---------------------------------------------------------------------------

class TestHardStop:

    def test_no_friday_on_or_after_sep1(self):
        result = get_rolling_fridays(date(2026, 6, 9))
        for d in result:
            assert d < date(2026, 9, 1), f"{d} is on or after Sep 1"

    def test_sep4_never_returned(self):
        """Sep 4 is the first Friday >= Sep 1; it must never be returned."""
        for days_before in range(1, 30):
            today = date(2026, 9, 4) - timedelta(days=days_before)
            result = get_rolling_fridays(today)
            assert date(2026, 9, 4) not in result

    def test_aug28_is_last_possible_friday(self):
        """Aug 28 is the last valid Friday (< Sep 1, not in skip range)."""
        today = date(2026, 8, 24)  # Monday after Aug 21 skip
        result = get_rolling_fridays(today)
        assert date(2026, 8, 28) in result
        # No Sep Fridays
        for d in result:
            assert d.month < 9

    def test_empty_when_past_all_valid_fridays(self):
        """After Aug 28, there are no more valid Fridays before Sep 1."""
        today = date(2026, 8, 29)  # Saturday after last valid Friday
        result = get_rolling_fridays(today)
        assert result == []

    def test_window_shrinks_as_summer_ends(self):
        """Window should progressively shrink in late August."""
        counts = [
            len(get_rolling_fridays(date(2026, 8, 10))),  # Aug 14, [skip 21], Aug 28 → 2 (maybe 3 if earlier window)
            len(get_rolling_fridays(date(2026, 8, 22))),  # Aug 28 only → 1
            len(get_rolling_fridays(date(2026, 8, 29))),  # nothing → 0
        ]
        # Each count should be <= the previous
        assert counts[0] >= counts[1] >= counts[2]
        assert counts[2] == 0


# ---------------------------------------------------------------------------
# get_window_dates
# ---------------------------------------------------------------------------

class TestGetWindowDates:

    FRI = date(2026, 6, 12)  # reference Friday

    def test_fri_sun(self):
        wt = next(w for w in WINDOW_TYPES if w["label"] == "fri_sun")
        out, ret = get_window_dates(self.FRI, wt)
        assert out == date(2026, 6, 12)   # Friday
        assert ret == date(2026, 6, 14)   # Sunday
        assert out.weekday() == 4
        assert ret.weekday() == 6

    def test_thu_sun(self):
        wt = next(w for w in WINDOW_TYPES if w["label"] == "thu_sun")
        out, ret = get_window_dates(self.FRI, wt)
        assert out == date(2026, 6, 11)   # Thursday
        assert ret == date(2026, 6, 14)   # Sunday
        assert out.weekday() == 3
        assert ret.weekday() == 6

    def test_fri_mon(self):
        wt = next(w for w in WINDOW_TYPES if w["label"] == "fri_mon")
        out, ret = get_window_dates(self.FRI, wt)
        assert out == date(2026, 6, 12)   # Friday
        assert ret == date(2026, 6, 15)   # Monday
        assert out.weekday() == 4
        assert ret.weekday() == 0

    def test_thu_mon(self):
        wt = next(w for w in WINDOW_TYPES if w["label"] == "thu_mon")
        out, ret = get_window_dates(self.FRI, wt)
        assert out == date(2026, 6, 11)   # Thursday
        assert ret == date(2026, 6, 15)   # Monday
        assert out.weekday() == 3
        assert ret.weekday() == 0


# ---------------------------------------------------------------------------
# get_all_windows
# ---------------------------------------------------------------------------

class TestGetAllWindows:

    def test_total_count(self):
        """6 Fridays × 4 window types = 24 entries."""
        windows = get_all_windows(date(2026, 6, 9))
        assert len(windows) == ROLLING_WINDOW_SIZE * len(WINDOW_TYPES)

    def test_required_keys(self):
        windows = get_all_windows(date(2026, 6, 9))
        for w in windows:
            assert "fri_date"      in w
            assert "label"         in w
            assert "outbound_date" in w
            assert "return_date"   in w

    def test_labels_present(self):
        labels = {w["label"] for w in get_all_windows(date(2026, 6, 9))}
        assert labels == {"fri_sun", "thu_sun", "fri_mon", "thu_mon"}

    def test_outbound_before_return(self):
        for w in get_all_windows(date(2026, 6, 9)):
            assert w["outbound_date"] < w["return_date"]

    def test_no_aug21_windows(self):
        for w in get_all_windows(date(2026, 6, 9)):
            assert w["fri_date"] != date(2026, 8, 21)

    def test_empty_after_season(self):
        windows = get_all_windows(date(2026, 8, 29))
        assert windows == []

    def test_reduced_near_cutoff(self):
        windows = get_all_windows(date(2026, 8, 10))
        assert 0 < len(windows) < ROLLING_WINDOW_SIZE * len(WINDOW_TYPES)
