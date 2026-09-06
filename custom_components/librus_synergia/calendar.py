"""Calendar platform for the Librus Synergia (unofficial) integration."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import LibrusConfigEntry, librus_device_info
from .coordinator import LibrusDataUpdateCoordinator, merge_timetables
from .librus_api import LibrusError
from .librus_api.models import (
    FreeDayData,
    HomeworkEventData,
    LessonData,
    LibrusData,
    ParentTeacherConferenceData,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LibrusConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Librus Synergia calendars from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            LibrusTimetableCalendar(coordinator, entry),
            LibrusAgendaCalendar(coordinator, entry),
            LibrusFreeDaysCalendar(coordinator, entry),
        ]
    )


def _iso_week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _lesson_to_event(day: date, lesson: LessonData, data: LibrusData) -> CalendarEvent | None:
    if lesson.hour_from is None or lesson.hour_to is None:
        return None
    try:
        start_time = datetime.strptime(lesson.hour_from, "%H:%M").time()
        end_time = datetime.strptime(lesson.hour_to, "%H:%M").time()
    except ValueError:
        return None

    subject_name = (
        data.subjects.get(lesson.subject_id, f"Lekcja {lesson.subject_id}")
        if lesson.subject_id is not None
        else "Lekcja"
    )
    summary = subject_name
    if lesson.is_canceled:
        summary = f"{summary} (odwołane)"
    elif lesson.is_substitution:
        summary = f"{summary} (zastępstwo)"

    teacher_name = data.teachers.get(lesson.teacher_id) if lesson.teacher_id is not None else None
    classroom_name = (
        data.classrooms.get(lesson.classroom_id) if lesson.classroom_id is not None else None
    )

    return CalendarEvent(
        start=dt_util.as_local(datetime.combine(day, start_time)),
        end=dt_util.as_local(datetime.combine(day, end_time)),
        summary=summary,
        location=classroom_name,
        description=teacher_name,
    )


def _homework_to_event(item: HomeworkEventData, data: LibrusData) -> CalendarEvent | None:
    if not item.date:
        return None
    try:
        day = date.fromisoformat(item.date[:10])
    except ValueError:
        return None

    subject_name = data.subjects.get(item.subject_id) if item.subject_id is not None else None
    content = (item.content or "").strip()
    if subject_name and content:
        summary = f"{subject_name}: {content[:80]}"
    else:
        summary = content[:80] or subject_name or "Wydarzenie"

    # HomeWorks/Categories confirmed live (e.g. "Sprawdzian", "Wycieczka",
    # "Konkurs") - prefix it when known, since it's the single most useful
    # bit of context for scanning a list of agenda events at a glance.
    category_name = (
        data.homework_categories.get(item.category_id) if item.category_id is not None else None
    )
    if category_name:
        summary = f"[{category_name}] {summary}"

    return CalendarEvent(
        start=day,
        end=day + timedelta(days=1),
        summary=summary,
        description=content or None,
    )


def _pt_conference_to_event(item: ParentTeacherConferenceData, data: LibrusData) -> CalendarEvent | None:
    """Defensive extra merge - see ParentTeacherConferenceData's docstring
    for why this is a belt-and-suspenders addition, not the primary
    source, of "wywiadówka"/"zebranie" events. Always an all-day event
    (the `Time` field goes into the description instead) - matches every
    other event in this calendar and avoids mixing date/datetime `start`/
    `end` types within the same event list, which HA's CalendarEvent
    comparisons can't handle."""
    if not item.date:
        return None
    try:
        day = date.fromisoformat(item.date[:10])
    except ValueError:
        return None

    teacher_name = data.teachers.get(item.teacher_id) if item.teacher_id is not None else None
    summary = f"[Zebranie z Rodzicami] {item.topic}".strip() if item.topic else "Zebranie z Rodzicami"
    description_parts = [p for p in (item.time, teacher_name) if p]

    return CalendarEvent(
        start=day,
        end=day + timedelta(days=1),
        summary=summary,
        description=" - ".join(description_parts) or None,
    )


def _free_day_to_event(item: FreeDayData) -> CalendarEvent | None:
    try:
        start = date.fromisoformat(item.date_from[:10])
        end = date.fromisoformat(item.date_to[:10])
    except ValueError:
        return None
    return CalendarEvent(
        start=start,
        # CalendarEvent's `end` for an all-day event is EXCLUSIVE (the day
        # after the last free day), matching _homework_to_event's
        # single-day convention above.
        end=end + timedelta(days=1),
        summary=item.name or "Dzień wolny",
    )


class LibrusTimetableCalendar(CoordinatorEntity[LibrusDataUpdateCoordinator], CalendarEntity):
    """The student's lesson timetable, including known substitutions.

    Dashboards can ask `async_get_events` for arbitrary ranges (e.g. "next
    month") outside the coordinator's cached current+next-week window, so
    this fetches specific weeks on demand through the client directly,
    caching each week locally to avoid refetching it repeatedly.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "timetable"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_timetable"
        self._attr_device_info = librus_device_info(entry)
        self._week_cache: dict[date, dict[date, list[LessonData]]] = {}

    async def _async_get_week(self, week_start: date) -> dict[date, list[LessonData]]:
        if week_start not in self._week_cache:
            try:
                payload = await self.coordinator.async_fetch_timetable_week(week_start)
            except LibrusError:
                # Forced re-login (inside async_fetch_timetable_week) also
                # failed - don't crash the whole calendar REST request over
                # one week's worth of lessons; log and degrade to "no
                # lessons known for this week" instead. Not cached, so the
                # next request for this same week tries again fresh.
                _LOGGER.warning(
                    "Failed to fetch timetable for week starting %s "
                    "(session recovery also failed) - returning no lessons "
                    "for this week",
                    week_start,
                )
                return {}
            self._week_cache[week_start] = merge_timetables(payload)
        return self._week_cache[week_start]

    @property
    def event(self) -> CalendarEvent | None:
        if self.coordinator.data is None:
            return None
        now = dt_util.now()
        upcoming = [
            event
            for day, lessons in self.coordinator.data.timetable.items()
            for lesson in lessons
            if (event := _lesson_to_event(day, lesson, self.coordinator.data)) is not None
            and event.end >= now
        ]
        return min(upcoming, key=lambda event: event.start) if upcoming else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        if self.coordinator.data is None:
            return []
        merged: dict[date, list[LessonData]] = {}
        week_start = _iso_week_start(start_date.date())
        last_week_start = _iso_week_start(end_date.date())
        while week_start <= last_week_start:
            merged.update(await self._async_get_week(week_start))
            week_start += timedelta(days=7)

        events: list[CalendarEvent] = []
        for day, lessons in merged.items():
            if day < start_date.date() or day > end_date.date():
                continue
            for lesson in lessons:
                event = _lesson_to_event(day, lesson, self.coordinator.data)
                if event is not None:
                    events.append(event)
        return events


class LibrusAgendaCalendar(CoordinatorEntity[LibrusDataUpdateCoordinator], CalendarEntity):
    """General agenda/events feed (tests, trips, homework) from `HomeWorks`,
    plus a defensive merge of `ParentTeacherConferences` (see
    `ParentTeacherConferenceData`'s docstring - live-verified redundant
    with `HomeWorks` for this account, kept as a belt-and-suspenders extra).

    Unlike the timetable, `HomeWorks` isn't confirmed to accept a date-range
    query (see the project's empirical-gaps notes), so this only serves
    whatever the coordinator's own full fetch already returned rather than
    fetching specific out-of-range windows on demand.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "agenda"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_agenda"
        self._attr_device_info = librus_device_info(entry)

    @property
    def event(self) -> CalendarEvent | None:
        if self.coordinator.data is None:
            return None
        today = dt_util.now().date()
        upcoming = [
            event
            for item in self.coordinator.data.homeworks
            if (event := _homework_to_event(item, self.coordinator.data)) is not None
            and event.end >= today
        ] + [
            event
            for item in self.coordinator.data.parent_teacher_conferences
            if (event := _pt_conference_to_event(item, self.coordinator.data)) is not None
            and event.end >= today
        ]
        return min(upcoming, key=lambda event: event.start) if upcoming else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        if self.coordinator.data is None:
            return []
        start, end = start_date.date(), end_date.date()
        events: list[CalendarEvent] = []
        for item in self.coordinator.data.homeworks:
            event = _homework_to_event(item, self.coordinator.data)
            if event is not None and start <= event.start <= end:
                events.append(event)
        for item in self.coordinator.data.parent_teacher_conferences:
            event = _pt_conference_to_event(item, self.coordinator.data)
            if event is not None and start <= event.start <= end:
                events.append(event)
        return events


class LibrusFreeDaysCalendar(CoordinatorEntity[LibrusDataUpdateCoordinator], CalendarEntity):
    """School holidays/breaks for the whole year, from `SchoolFreeDays` +
    `ClassFreeDays` (confirmed live - both real endpoints, same shape).

    Small, whole-year dataset refreshed on the coordinator's normal 24h
    reference-data cadence - no per-range on-demand fetching needed, unlike
    the timetable calendar.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "free_days"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_free_days"
        self._attr_device_info = librus_device_info(entry)

    @property
    def event(self) -> CalendarEvent | None:
        if self.coordinator.data is None:
            return None
        today = dt_util.now().date()
        upcoming = [
            event
            for item in self.coordinator.data.free_days
            if (event := _free_day_to_event(item)) is not None and event.end > today
        ]
        return min(upcoming, key=lambda event: event.start) if upcoming else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        if self.coordinator.data is None:
            return []
        start, end = start_date.date(), end_date.date()
        events: list[CalendarEvent] = []
        for item in self.coordinator.data.free_days:
            event = _free_day_to_event(item)
            # Overlap check, not containment - a multi-day break can start
            # before the requested window and/or end after it.
            if event is not None and event.start <= end and event.end > start:
                events.append(event)
        return events
