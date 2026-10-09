"""School-day logic shared by the binary sensors and the start/end sensors.

Everything here reads the coordinator's cached current+next-week timetable
(no extra Librus requests). A day counts as a school day when it has at
least one lesson that isn't cancelled and isn't inside a free day from
`SchoolFreeDays`/`ClassFreeDays` (some schools leave the timetable filled
in on a day off).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from librus_synergia.models import LessonData, LibrusData

from .ai_summary import _day
from .forecast import DataMemo

# Five entities re-check this every minute (school day today/tomorrow, in
# school, school start/end) - ten calls a minute that each parsed every
# lesson's times and expanded every free-day range. Computed once per data
# object and local date instead.
_SCHOOL_DAYS = DataMemo()


@dataclass(frozen=True, slots=True)
class SchoolDay:
    """The held lessons of one day, in order."""

    day: date
    first_start: datetime
    last_end: datetime
    first_lesson: LessonData
    last_lesson: LessonData


def _free_dates(data: LibrusData) -> set[date]:
    dates: set[date] = set()
    for free in data.free_days:
        start, end = _day(free.date_from), _day(free.date_to)
        if start is None or end is None or end < start or (end - start).days > 400:
            continue
        dates.update(start + timedelta(days=i) for i in range((end - start).days + 1))
    return dates


def school_days(data: LibrusData | None) -> dict[date, SchoolDay]:
    """Every day in the cached timetable that has lessons, keyed by date.
    The dict is shared between callers - don't modify it."""
    if data is None:
        return {}
    # Keyed by the local date too, so a result never outlives the day it
    # was worked out on (lesson times become local datetimes).
    return _SCHOOL_DAYS.get(data, dt_util.now().date(), lambda: _school_days(data))


def _school_days(data: LibrusData) -> dict[date, SchoolDay]:
    # Imported here: sensor.py imports this module for its own entities.
    from .sensor import _lesson_bounds  # noqa: PLC0415

    free = _free_dates(data)
    days: dict[date, SchoolDay] = {}
    for day, lessons in data.timetable.items():
        if day in free:
            continue
        held = [
            (bounds, lesson)
            for lesson in lessons
            if not lesson.is_canceled and (bounds := _lesson_bounds(day, lesson)) is not None
        ]
        if not held:
            continue
        held.sort(key=lambda item: item[0][0])
        first = held[0]
        last = max(held, key=lambda item: item[0][1])
        days[day] = SchoolDay(day, first[0][0], last[0][1], first[1], last[1])
    return days


def in_school(days: dict[date, SchoolDay], now: datetime) -> bool:
    """Between the first lesson's start and the last lesson's end today -
    breaks included."""
    today = days.get(now.date())
    return today is not None and today.first_start <= now < today.last_end


def next_start(days: dict[date, SchoolDay], now: datetime) -> SchoolDay | None:
    """The school day whose first lesson is the next to start: today's
    until it starts, then the next school day's."""
    upcoming = [d for d in days.values() if d.first_start > now]
    return min(upcoming, key=lambda d: d.first_start, default=None)


def next_end(days: dict[date, SchoolDay], now: datetime) -> SchoolDay | None:
    """The school day whose last lesson is the next to end: today's while
    school is on (or before it starts), then the next school day's."""
    upcoming = [d for d in days.values() if d.last_end > now]
    return min(upcoming, key=lambda d: d.last_end, default=None)


class MinuteRefresh:
    """Mixin for coordinator entities whose state depends on the clock:
    re-evaluated every minute, written only when it actually changed (so
    history isn't flooded with identical states)."""

    _last_written: Any = None

    def _signature(self) -> Any:
        return (self.state, self.extra_state_attributes)  # type: ignore[attr-defined]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()  # type: ignore[misc]
        self.async_on_remove(  # type: ignore[attr-defined]
            async_track_time_change(self.hass, self._async_minute, second=0)  # type: ignore[attr-defined]
        )

    @callback
    def _async_minute(self, _now: datetime) -> None:
        if self._signature() != self._last_written:
            self.async_write_ha_state()  # type: ignore[attr-defined]

    @callback
    def async_write_ha_state(self) -> None:
        self._last_written = self._signature()
        super().async_write_ha_state()  # type: ignore[misc]
