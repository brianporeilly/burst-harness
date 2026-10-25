"""Cron schedules: parsing, and when they are due, across summer time changes."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from harness import cron
from harness.job import JobError, _trigger

ANCHORAGE = ZoneInfo("America/Anchorage")


def at(text: str, zone=ANCHORAGE) -> datetime:
    """A wall-clock time in a zone, as the UTC instant."""
    return datetime.fromisoformat(text).replace(tzinfo=zone).astimezone(UTC)


def test_fields_numbers_ranges_lists_steps_and_names():
    s = cron.parse("*/15 8-18/2 1,15 jan-mar,dec mon-fri")
    assert s.minutes == (0, 15, 30, 45) and s.hours == (8, 10, 12, 14, 16, 18)
    assert s.days == {1, 15} and s.months == {1, 2, 3, 12} and s.weekdays == {1, 2, 3, 4, 5}
    assert cron.parse("0 0 * * 7").weekdays == {0} == cron.parse("0 0 * * sun").weekdays
    assert cron.parse("5/20 * * * *").minutes == (5, 25, 45)  # from 5 on


@pytest.mark.parametrize("text", ["* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *",
                                  "* * * * 8", "5-1 * * * *", "*/0 * * * *", "* * * * funday", "a b c d e"])
def test_bad_schedules_are_refused(text):
    with pytest.raises(cron.CronError):
        cron.parse(text)


def test_a_jobs_trigger_is_checked_with_the_same_rules():
    assert _trigger(" 0  7 * * 1-5 ") == "0 7 * * 1-5" and _trigger("manual") == "manual"
    with pytest.raises(JobError, match="not between 0 and 23"):
        _trigger("0 25 * * *")


def test_day_of_month_and_day_of_week_either_will_do_when_both_are_set():
    s = cron.parse("0 9 1 * mon")
    first_a_tuesday, a_monday, neither = datetime(2026, 9, 1).date(), datetime(2026, 9, 7).date(), datetime(2026, 9, 8).date()
    assert s.on(first_a_tuesday) and s.on(a_monday) and not s.on(neither)
    assert not cron.parse("0 9 * * mon").on(first_a_tuesday)  # day of month *: only the weekday counts


def test_next_and_last_due_are_wall_clock_times_in_the_zone():
    s = cron.parse("0 8 * * *")
    now = at("2026-10-25T07:59:30")
    assert cron.next_due(s, ANCHORAGE, now) == at("2026-10-25T08:00")
    assert cron.last_due(s, ANCHORAGE, now, timedelta(hours=1)) is None
    later = at("2026-10-25T08:40")
    assert cron.last_due(s, ANCHORAGE, later, timedelta(hours=1)) == at("2026-10-25T08:00")
    assert cron.last_due(s, ANCHORAGE, at("2026-10-25T09:01"), timedelta(hours=1)) is None  # missed by too long
    weekdays = cron.parse("0 7 * * 1-5")
    assert cron.next_due(weekdays, ANCHORAGE, at("2026-10-23T08:00")) == at("2026-10-26T07:00")  # Friday to Monday


def test_summer_time_skips_a_time_once_and_repeats_none():
    # Anchorage: 2026-03-08 02:00 -> 03:00 (02:30 never happens); 2026-11-01 02:00 -> 01:00 (01:30 happens twice).
    s = cron.parse("30 2 * * *")
    assert cron.next_due(s, ANCHORAGE, at("2026-03-07T03:00")) == at("2026-03-09T02:30")
    twice = cron.parse("30 1 * * *")
    first = cron.next_due(twice, ANCHORAGE, at("2026-11-01T00:00"))
    assert first == datetime(2026, 11, 1, 9, 30, tzinfo=UTC)  # 01:30 AKDT, the first of the two
    assert cron.next_due(twice, ANCHORAGE, first) == at("2026-11-02T01:30")
    # An hourly schedule has each wall-clock hour once through the change back, the repeated 01:00 too.
    hourly, now, hours = cron.parse("0 * * * *"), at("2026-10-31T23:30"), []
    for _ in range(5):
        now = cron.next_due(hourly, ANCHORAGE, now)
        hours.append(now.astimezone(ANCHORAGE).hour)
    assert hours == [0, 1, 2, 3, 4]


def test_a_schedule_that_never_comes_has_no_next_time():
    assert cron.next_due(cron.parse("0 0 31 2 *"), UTC, datetime(2026, 1, 1, tzinfo=UTC)) is None
    leap = cron.next_due(cron.parse("0 0 29 2 *"), UTC, datetime(2026, 1, 1, tzinfo=UTC))
    assert leap == datetime(2028, 2, 29, tzinfo=UTC)
