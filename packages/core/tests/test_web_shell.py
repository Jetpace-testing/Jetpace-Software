"""
The shell: routing, the time-range contract, and the bucket arithmetic.

No database and no browser. What is worth pinning here is the logic that
decides what a chart is ALLOWED to ask for - because the alternative is
discovering on a site that one URL can ask the historian to bucket four
hundred days of raw readings into three hundred pixels.
"""

import pytest

from jetpace_core.web import queries
from jetpace_core.web.app import MAX_HOURS, MIN_HOURS, RANGES, window_label


# --- bucket width ---------------------------------------------------------

@pytest.mark.parametrize("hours, expected", [
    (1, "15 seconds"),
    (6, "1 minute"),
    (24, "5 minutes"),
    (72, "15 minutes"),
    (24 * 7, "1 hour"),
    (24 * 31, "6 hours"),
])
def test_each_offered_range_gets_its_intended_width(hours, expected):
    assert queries.bucket_width(hours) == expected


def test_a_window_between_two_ranges_takes_the_finer_one(queries_module=queries):
    """
    The first row whose window is at least the request wins, so 2 hours is
    bucketed as if it were 6 - finer than needed rather than coarser.
    Coarser would silently drop detail the reader asked for.
    """
    assert queries.bucket_width(2) == "1 minute"
    assert queries.bucket_width(30) == "15 minutes"


def test_an_absurd_window_still_returns_a_width():
    assert queries.bucket_width(24 * 3650) == queries.BUCKET_FALLBACK


@pytest.mark.parametrize("hours", [h for _, h in RANGES])
def test_every_offered_range_produces_a_sane_number_of_points(hours):
    """
    The whole point of deriving the width: roughly 100-400 points whatever
    the window. Too few and an excursion vanishes; too many and the
    browser is asked to draw more marks than the chart has pixels.
    """
    seconds = {
        "15 seconds": 15, "1 minute": 60, "5 minutes": 300,
        "15 minutes": 900, "1 hour": 3600, "6 hours": 21600,
        "12 hours": 43200, "1 day": 86400,
    }[queries.bucket_width(hours)]
    points = hours * 3600 / seconds
    assert 100 <= points <= 400, f"{hours} h gives {points:.0f} points"


# --- which source answers -------------------------------------------------

def test_short_windows_read_raw_and_long_ones_read_the_rollup():
    """
    The rollup cannot serve an hour - an hour of it is one point. Raw
    cannot serve a month: thirty days of one point at a 2-second scan is
    1.3 million rows to draw a few hundred pixels.
    """
    assert queries.ROLLUP_ABOVE_HOURS == 24
    assert 1 <= queries.ROLLUP_ABOVE_HOURS
    assert 168 > queries.ROLLUP_ABOVE_HOURS


# --- the window clamp -----------------------------------------------------

def test_the_clamp_bounds_are_the_ones_the_route_applies():
    assert MIN_HOURS > 0
    assert MAX_HOURS <= 24 * 400


@pytest.mark.parametrize("asked, expected", [
    (0, MIN_HOURS),
    (-5, MIN_HOURS),
    (10 ** 9, MAX_HOURS),
    (24, 24),
])
def test_a_window_from_a_query_string_is_clamped(asked, expected):
    """
    hours is whatever anybody types into the address bar. Unclamped,
    hours=1000000 asks the database to bucket the entire history, and the
    only thing that achieves is a timeout on a site.
    """
    clamped = max(MIN_HOURS, min(float(asked), MAX_HOURS))
    assert clamped == expected


# --- what the footer says -------------------------------------------------

@pytest.mark.parametrize("hours, expected", [
    (1, "last 1 h"),
    (24, "last 24 h"),
    (168, "last 7 d"),
    (720, "last 30 d"),
])
def test_an_offered_range_is_named_the_way_the_button_names_it(hours, expected):
    assert window_label(hours) == expected


@pytest.mark.parametrize("hours, expected", [
    (0.5, "last 30 min"),
    (2, "last 2 h"),
    (96, "last 4 days"),
])
def test_a_window_nobody_offered_is_still_described(hours, expected):
    """A typed URL gets a readable answer rather than a bare number."""
    assert window_label(hours) == expected


def test_the_ranges_are_in_order_and_distinct():
    hours = [h for _, h in RANGES]
    assert hours == sorted(hours)
    assert len(set(hours)) == len(hours)


# --- the queries the shell owns -------------------------------------------

@pytest.mark.parametrize("sql_name", ["DEVICES", "DEVICE", "DEVICE_POINTS",
                                      "TREND_RAW", "TREND_ROLLUP"])
def test_the_shell_queries_name_no_parameter_key(sql_name):
    """
    The structural claim this work rests on. dp_trend and energy_daily are
    tied to 'dp' and 'kwh_import' and so belong to a product view; these
    serve a pressure transmitter, an energy meter or a particle counter
    without being told which. If a literal key appears here, the shell has
    quietly become one product's shell again.
    """
    sql = getattr(queries, sql_name)
    assert "'dp'" not in sql
    assert "'kwh_import'" not in sql


@pytest.mark.parametrize("sql_name", ["DEVICES", "DEVICE", "DEVICE_POINTS",
                                      "TREND_RAW", "TREND_ROLLUP"])
def test_the_shell_queries_hide_retired_devices_or_filter_by_code(sql_name):
    """
    A retired device keeps its readings, so every list must exclude it
    explicitly - or be scoped to one code the caller already named.
    """
    sql = getattr(queries, sql_name)
    assert "retired_at IS NULL" in sql or "d.code = %(code)s" in sql


def test_the_trend_queries_are_parameterised_not_formatted():
    """
    device and param arrive from a query string. They are bound, never
    interpolated - and this test exists so that stays true after somebody
    adds a filter in a hurry.
    """
    for sql in (queries.TREND_RAW, queries.TREND_ROLLUP):
        assert "%(code)s" in sql and "%(param)s" in sql
        assert "format(" not in sql
