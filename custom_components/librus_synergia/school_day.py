"""School-day logic shared by the binary sensors and the start/end sensors.

Everything here reads the coordinator's cached current+next-week timetable
(no extra Librus requests). A day counts as a school day when it has at
least one lesson that isn't cancelled and isn't inside a free day from
`SchoolFreeDays`/`ClassFreeDays` (some schools leave the timetable filled
in on a day off).
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from librus_synergia.models import LessonData, LibrusData

from .ai_summary import _day
from .forecast import DataMemo

# Five entities read this (school day today/tomorrow, in school, school
# start/end), plus the shared clock and smart polling - each call used to
# parse every lesson's times and expand every free-day range. Computed once
# per data object and local date instead.
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


def school_days(data: LibrusData | None, owner: Hashable | None = None) -> dict[date, SchoolDay]:
    """Every day in the cached timetable that has lessons, keyed by date.
    The dict is shared between callers - don't modify it. `owner` is the
    config entry id (see forecast.DataMemo)."""
    if data is None:
        return {}
    # Keyed by the local date too, so a result never outlives the day it
    # was worked out on (lesson times become local datetimes).
    return _SCHOOL_DAYS.get(
        data, dt_util.now().date(), lambda: _school_days(data), owner=owner
    )


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


def _next_transition(days: dict[date, SchoolDay], now: datetime) -> datetime:
    """The next moment a school-day entity can change: a first lesson
    starting, a last lesson ending, or the next local midnight (today and
    tomorrow move on)."""
    midnight = dt_util.start_of_local_day(now.date() + timedelta(days=1))
    moments = [
        moment
        for day in days.values()
        for moment in (day.first_start, day.last_end)
        if now < moment < midnight
    ]
    return min(moments, default=midnight)


class SchoolDayClock:
    """One timer per coordinator for the school-day entities (school day
    today/tomorrow, in school, school start/end): it fires at the next real
    transition (see `_next_transition`) instead of every entity re-checking
    itself every minute, and is re-armed after firing and on every
    coordinator update (new lessons can move the next transition)."""

    def __init__(self, coordinator: Any) -> None:
        self._coordinator = coordinator
        self._listeners: list[Callable[[], None]] = []
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._unsub_coordinator: CALLBACK_TYPE | None = None

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(update)
        if len(self._listeners) == 1:
            self._unsub_coordinator = self._coordinator.async_add_listener(self._arm)
            self._arm()

        @callback
        def remove() -> None:
            self._listeners.remove(update)
            if not self._listeners:
                self._stop()

        return remove

    @callback
    def _stop(self) -> None:
        for unsub in (self._unsub_timer, self._unsub_coordinator):
            if unsub is not None:
                unsub()
        self._unsub_timer = self._unsub_coordinator = None

    @callback
    def async_stop(self) -> None:
        """The entry is unloading: no listeners, no timer."""
        self._listeners.clear()
        self._stop()

    @callback
    def _arm(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
        days = school_days(self._coordinator.data, self._coordinator.memo_owner)
        # A second past the moment: a lesson's start/end itself is inclusive
        # on one side of each check, so the state is read once it's over.
        moment = _next_transition(days, dt_util.now()) + timedelta(seconds=1)
        self._unsub_timer = async_track_point_in_time(
            self._coordinator.hass, self._async_fire, moment
        )

    @callback
    def _async_fire(self, _now: datetime) -> None:
        self._unsub_timer = None
        for update in list(self._listeners):
            update()
        if self._listeners:
            self._arm()


def school_day_clock(coordinator: Any) -> SchoolDayClock:
    """The coordinator's shared SchoolDayClock (created on first use), kept
    on the coordinator itself - a module-level map from coordinator to clock
    kept every unloaded coordinator alive (the clock refers back to its
    coordinator, so a weak key never went away). Stopped on unload
    (`LibrusDataUpdateCoordinator.async_close_state`)."""
    clock = getattr(coordinator, "school_day_clock", None)
    if not isinstance(clock, SchoolDayClock):
        clock = SchoolDayClock(coordinator)
        coordinator.school_day_clock = clock
    return clock


class SchoolDayRefresh:
    """Mixin for coordinator entities whose state depends on the clock:
    re-evaluated at each school-day transition (`SchoolDayClock`), written
    only when it actually changed (so history isn't flooded with identical
    states). Replaces a per-entity check every minute - five entities per
    student re-reading their state 1440 times a day for a handful of real
    changes."""

    _last_written: Any = None

    def _signature(self) -> Any:
        return (self.state, self.extra_state_attributes)  # type: ignore[attr-defined]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()  # type: ignore[misc]
        self.async_on_remove(  # type: ignore[attr-defined]
            school_day_clock(self.coordinator).async_add_listener(  # type: ignore[attr-defined]
                self._async_transition
            )
        )

    @callback
    def _async_transition(self) -> None:
        if self._signature() != self._last_written:
            self.async_write_ha_state()  # type: ignore[attr-defined]

    @callback
    def async_write_ha_state(self) -> None:
        self._last_written = self._signature()
        super().async_write_ha_state()  # type: ignore[misc]


class SkipUnchangedUpdates:
    """Mixin for coordinator entities: a coordinator update that changed
    nothing this entity shows doesn't write its state again.

    "Nothing changed" = the same data object (the coordinator keeps the old
    one when a refresh brings identical data, and a skipped poll returns it
    as is), the same local date (most attributes count days from today; the
    coordinator's midnight tick brings the new date), the same
    `last_update_success` and the same `_side_state()` - what an entity
    reads from the coordinator besides the data (badges, read receipts,
    ...). Entities whose state depends on the time of day set
    `_always_write`. Every student's ~40 entities used to be written on
    every poll, identical or not."""

    _always_write = False
    _written_data: Any = None
    _written_key: Any = None

    def _side_state(self) -> Any:
        """What else besides the data this entity shows - compared with
        `==`, so return a snapshot (a copy of anything mutated in place)."""
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        coordinator = self.coordinator  # type: ignore[attr-defined]
        data = coordinator.data
        key = (coordinator.last_update_success, dt_util.now().date(), self._side_state())
        if (
            not self._always_write
            and self._written_key is not None
            and data is self._written_data
            and key == self._written_key
        ):
            return
        self._written_data, self._written_key = data, key
        super()._handle_coordinator_update()  # type: ignore[misc]
