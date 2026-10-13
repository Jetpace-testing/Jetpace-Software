"""
That the environmental queries arrived, and still say what they said.

The mirror of core's test_core_names_no_product: that one proves the
product SQL left core, and on its own it would also pass if the queries
had simply been deleted. These assert they are here and intact.

Pure string assertions, no database - the same shape as core's
test_web_shell.
"""

import pytest

from jetpace_environment import queries

ALL_SQL = (queries.ENVIRONMENT_SUMMARY, queries.DP_TREND, queries.EXCURSIONS)


@pytest.mark.parametrize("sql", ALL_SQL)
def test_every_environment_query_is_about_differential_pressure(sql):
    """
    The reason these are a product's and not the shell's. A query naming
    a parameter key can only ever serve one product.
    """
    assert "p.key = 'dp'" in sql


@pytest.mark.parametrize("sql", ALL_SQL)
def test_no_environment_query_shows_a_retired_device(sql):
    """
    A decommissioned room must not appear in a count of rooms in spec,
    and must not contribute excursion minutes to a report.
    """
    assert "retired_at IS NULL" in sql


def test_the_summary_counts_rooms_and_excursions():
    sql = queries.ENVIRONMENT_SUMMARY
    assert "rooms_total" in sql
    assert "rooms_in_spec" in sql
    assert "excursions_24h" in sql


def test_in_spec_means_strictly_positive():
    """
    A cascade at exactly zero is not holding. `value > 0` for in-spec and
    `value <= 0` for an excursion have to agree on the boundary, or a
    room at 0 Pa is counted as both or neither.
    """
    assert "WHERE value > 0" in queries.ENVIRONMENT_SUMMARY
    assert "r.value <= 0" in queries.ENVIRONMENT_SUMMARY
    assert "r.value <= 0" in queries.EXCURSIONS


def test_the_trend_reads_the_rollup_not_raw_readings():
    """
    24 hours of raw data for three rooms is 4,320 rows; from
    reading_hourly it is 72. The chart cannot tell the difference.
    """
    assert "reading_hourly" in queries.DP_TREND
    assert "FROM   reading r" not in queries.DP_TREND


def test_the_excursion_count_reports_the_worst_reading_too():
    """
    Minutes below zero says how long; it does not say how bad. Both are
    needed before anyone can judge whether an excursion mattered.
    """
    assert "worst" in queries.EXCURSIONS
    assert "minutes_below_zero" in queries.EXCURSIONS
