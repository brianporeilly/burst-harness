"""Five-field cron schedules, as a job's `trigger` gives them ("0 7 * * 1-5"), for
the orchestrator's scheduler.

The fields are minute, hour, day of month, month and day of week, each `*`, a
number, a range (`1-5`), a list of those (`1,15`) or any of them with a step
(`*/15`, `8-18/2`). Months and days of the week may be named (`jan`, `mon`),
and Sunday is 0 or 7. As in cron, when both the day of the month and the day
of the week are restricted, a day matching either will do.

Times are wall-clock times in the scheduler's timezone. A time that a change
to summer time skips does not happen that day; one that the change back
repeats happens once, the first time.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo

MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
DAYS = {name: number for number, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
FIELDS = (("minute", 0, 59, {}), ("hour", 0, 23, {}), ("day of month", 1, 31, {}),
          ("month", 1, 12, MONTHS), ("day of week", 0, 7, DAYS))
PART = re.compile(r"(?:(\*)|([a-z0-9]+)(?:-([a-z0-9]+))?)(?:/([0-9]+))?")
# How far ahead next_due looks: far enough for the 29th of February.
HORIZON_DAYS = 5 * 366


class CronError(ValueError):
    """A schedule that is not five valid cron fields."""


@dataclass(frozen=True)
class Schedule:
    text: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 is Sunday
    any_day: bool
    any_weekday: bool

    def on(self, day: date) -> bool:
        """Whether the schedule has times on that day."""
        if day.month not in self.months:
            return False
        by_date, by_weekday = day.day in self.days, (day.weekday() + 1) % 7 in self.weekdays
        if self.any_day or self.any_weekday:
            return by_date and by_weekday
        return by_date or by_weekday


def _field(text: str, name: str, low: int, high: int, names: dict[str, int]) -> tuple[set[int], bool]:
    def number(token: str) -> int:
        value = names.get(token, int(token) if token.isdigit() else -1)
        if not low <= value <= high:
            raise CronError(f"{name}: {token!r} is not between {low} and {high}.")
        return value

    found: set[int] = set()
    for part in text.lower().split(","):
        if not (match := PART.fullmatch(part)):
            raise CronError(f"{name}: {part!r} is not *, a number, a range or a list, with an optional /step.")
        star, first, last, step = match.groups()
        start, end = (low, high) if star else (number(first), number(last) if last else None)
        if end is None:
            end = high if step else start  # "5/15" is from 5 on
        if start > end:
            raise CronError(f"{name}: {part!r} runs backwards.")
        if step is not None and int(step) == 0:
            raise CronError(f"{name}: a step of 0.")
        found.update(range(start, end + 1, int(step or 1)))
    return found, text == "*"


def parse(text: str) -> Schedule:
    parts = text.split()
    if len(parts) != 5:
        raise CronError(f"{text!r} is not five fields (minute hour day-of-month month day-of-week).")
    (minutes, _), (hours, _), (days, any_day), (months, _), (weekdays, any_weekday) = (
        _field(part, *spec) for part, spec in zip(parts, FIELDS, strict=True))
    return Schedule(" ".join(parts), tuple(sorted(minutes)), tuple(sorted(hours)), frozenset(days),
                    frozenset(months), frozenset(day % 7 for day in weekdays), any_day, any_weekday)


def _times(schedule: Schedule, zone: tzinfo, first: date, last: date) -> Iterator[datetime]:
    """Each time of the schedule on the days first to last, in order, as UTC instants."""
    day = first
    while day <= last:
        if schedule.on(day):
            for hour in schedule.hours:
                for minute in schedule.minutes:
                    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
                    instant = local.astimezone(UTC)
                    if instant.astimezone(zone).replace(tzinfo=None) == local.replace(tzinfo=None):
                        yield instant  # a wall-clock time that exists that day
        day += timedelta(days=1)


def last_due(schedule: Schedule, zone: tzinfo, now: datetime, within: timedelta) -> datetime | None:
    """The latest time of the schedule in (now - within, now], or None."""
    since = now - within
    found = None
    for instant in _times(schedule, zone, since.astimezone(zone).date(), now.astimezone(zone).date()):
        if since < instant <= now:
            found = instant
    return found


def next_due(schedule: Schedule, zone: tzinfo, now: datetime) -> datetime | None:
    """The first time of the schedule after now, or None if it never comes (the 31st of February)."""
    today = now.astimezone(zone).date()
    return next((t for t in _times(schedule, zone, today, today + timedelta(days=HORIZON_DAYS)) if t > now), None)
