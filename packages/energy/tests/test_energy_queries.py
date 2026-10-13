"""
That the energy queries arrived, and still say what they said.

The mirror of core's test_core_names_no_product - see the note in
packages/environment/tests/test_environment_queries.py.
"""

import pytest

from jetpace_energy import queries

ALL_SQL = (queries.ENERGY_DAILY, queries.ENERGY_PERIOD)


@pytest.mark.parametrize("sql", ALL_SQL)
def test_every_energy_query_is_about_imported_energy(sql):
    assert "'kwh_import'" in sql


@pytest.mark.parametrize("sql", ALL_SQL)
def test_no_energy_query_bills_a_retired_device(sql):
    assert "retired_at IS NULL" in sql


def test_a_billing_period_goes_through_the_consumption_function():
    """
    Not through close_value - open_value. consumption() is where the
    statuses live - ok, partial_new_meter, counter_reset, stale_open,
    not_a_counter - and architecture.md §7 requires a figure the system
    cannot defend to come back with a status rather than a plausible
    guess. Re-implementing the subtraction here would be that guess.
    """
    assert "consumption(" in queries.ENERGY_PERIOD
    assert "status" in queries.ENERGY_PERIOD


def test_a_counter_reset_is_not_counted_as_a_days_consumption():
    """
    Counters never run backwards (architecture.md §7). A meter that was
    replaced or rolled over shows close < open, and treating that
    difference as usage would invoice a negative number - or, worse, a
    very large one after an absolute-value fix.
    """
    assert "close_value >= open_value" in queries.ENERGY_DAILY


def test_the_daily_figures_come_from_the_daily_rollup():
    assert "reading_daily" in queries.ENERGY_DAILY


def test_a_period_only_includes_devices_that_have_actually_logged():
    """
    Otherwise every commissioned meter appears in a billing report with
    a blank, which reads as zero consumption rather than as no data.
    """
    assert "EXISTS" in queries.ENERGY_PERIOD
