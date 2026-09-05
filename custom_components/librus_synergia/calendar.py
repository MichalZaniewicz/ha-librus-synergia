"""Calendar platform for the Librus Synergia (unofficial) integration."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import LibrusConfigEntry, librus_device_info
from .coordinator import LibrusDataUpdateCoordinator, merge_timetables
from .librus_api.models import HomeworkEventData, LessonData, LibrusData


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

    return CalendarEvent(
        start=day,
        end=day + timedelta(days=1),
        summary=summary,
        description=content or None,
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
            payload = await self.coordinator.client.async_get_timetable(week_start)
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
    """General agenda/events feed (tests, trips, homework) from `HomeWorks`.

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
        return events
