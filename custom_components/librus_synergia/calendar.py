"""Calendar platform for the Librus Synergia (unofficial) integration."""

from __future__ import annotations

import logging
from bisect import bisect_left
from collections.abc import Hashable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from librus_synergia.models import (
    FreeDayData,
    HomeworkEventData,
    LessonData,
    LibrusData,
    ParentTeacherConferenceData,
)

from . import LibrusConfigEntry, librus_device_info
from .const import (
    CONF_FREE_DAYS_ENABLED,
    CONF_MERGE_PARALLEL_LESSONS,
    DEFAULT_FREE_DAYS_ENABLED,
    DEFAULT_MERGE_PARALLEL_LESSONS,
)
from .coordinator import LibrusDataUpdateCoordinator, lesson_change, on_demand_week_starts
from .forecast import DataMemo

_LOGGER = logging.getLogger(__name__)

# Summary suffix per lesson change (see coordinator.lesson_change). The
# companion cards match these to mark the lesson.
_CHANGE_LABELS = {
    "canceled": "odwołane",
    "substitution": "zastępstwo",
    "room_change": "zmiana sali",
    "moved": "przeniesiona",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LibrusConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Librus Synergia calendars from a config entry."""
    coordinator = entry.runtime_data
    entities: list[CalendarEntity] = [
        LibrusTimetableCalendar(coordinator, entry),
        LibrusAgendaCalendar(coordinator, entry),
    ]
    # Skipped entirely (not just left empty) when turned off in the options
    # flow - unlike the sensors gated the same way, a calendar entity has no
    # "unavailable" state that reads as clearly as just not existing.
    if entry.options.get(CONF_FREE_DAYS_ENABLED, DEFAULT_FREE_DAYS_ENABLED):
        entities.append(LibrusFreeDaysCalendar(coordinator, entry))
    async_add_entities(entities)


def _event_overlaps(event: CalendarEvent, start: date, end: date) -> bool:
    """True if `event` (an all-day event, `start`/`end` as `date`) overlaps
    the closed `[start, end]` day-range window - not single-POINT
    containment. Shared by `LibrusAgendaCalendar`/`LibrusFreeDaysCalendar`'s
    own `async_get_events` (code review - previously only the latter had
    this, the former checked `start <= event.start <= end` instead, which
    misses an event that starts before the window but still overlaps it).
    A multi-day event (a school break already, or - if Agenda ever grows
    one - a multi-day trip) can start before the requested window and/or
    end after it, which single-point containment would silently miss."""
    return event.start <= end and event.end > start


def _inclusive_end_date(end_date: datetime) -> date:
    """The last calendar DATE actually inside a half-open `[start, end)`
    window. `end_date.date()` alone over-includes by one day whenever
    `end_date` lands exactly on a local-midnight boundary (the case for
    essentially every card in this repo's own dev harness/cards - "today"
    is requested as [today 00:00, tomorrow 00:00)) - midnight technically
    belongs to the NEXT calendar day, so a bare `.date()` silently pulls
    that whole next day's all-day events into the "current" range.
    Nudging back by one microsecond first fixes exactly that case while
    leaving any other end time's date unaffected."""
    return (end_date - timedelta(microseconds=1)).date()


# (date, lesson number) -> topic, and the built calendar events, per
# coordinator data object and entry (see forecast.DataMemo). The topic index
# used to be keyed by a bare `id()` of the topics list: a recycled id could
# hand one student's topics to another, and two students evicted each
# other's index on every call.
_TOPIC_INDEX = DataMemo()
_CALENDAR_EVENTS = DataMemo()


def _lesson_topic(
    day: date, lesson: LessonData, data: LibrusData, owner: Hashable | None = None
) -> str | None:
    """The topic Librus has for this lesson (same date and lesson number;
    `Realizations`), if it has been held and filled in."""
    index = _TOPIC_INDEX.get(
        data,
        None,
        lambda: {
            ((t.date or "")[:10], t.lesson_no): t.topic for t in data.lesson_topics if t.topic
        },
        owner=owner,
    )
    return index.get((day.isoformat(), lesson.lesson_no))


@dataclass(frozen=True)
class _LessonParts:
    """One lesson ready for the calendar; `teacher` and `details` become
    the event description (teacher on the first line - the companion cards
    read it from there)."""

    start: datetime
    end: datetime
    summary: str
    location: str | None
    teacher: str | None
    details: list[str]
    changed: bool

    def to_event(self) -> CalendarEvent:
        description = "\n".join([self.teacher or "", *self.details]).strip() or None
        return CalendarEvent(
            start=self.start,
            end=self.end,
            summary=self.summary,
            location=self.location,
            description=description,
        )


def _lesson_to_event(
    day: date, lesson: LessonData, data: LibrusData, owner: Hashable | None = None
) -> CalendarEvent | None:
    parts = _lesson_parts(day, lesson, data, owner)
    return parts.to_event() if parts is not None else None


def _join_unique(values: list[str | None], separator: str) -> str | None:
    return separator.join(dict.fromkeys(v for v in values if v)) or None


def _merge_parts(group: list[_LessonParts]) -> CalendarEvent:
    """One event for lessons held at the same time (issue #14 - e.g. a
    subject plus "Wspomaganie", a support teacher in the same room, or
    split groups): "Edukacja wczesnoszkolna + Wspomaganie", all teachers
    and rooms."""
    return _LessonParts(
        start=group[0].start,
        end=group[0].end,
        summary=_join_unique([p.summary for p in group], " + ") or "",
        location=_join_unique([p.location for p in group], ", "),
        teacher=_join_unique([p.teacher for p in group], ", "),
        details=list(dict.fromkeys(line for p in group for line in p.details)),
        changed=False,
    ).to_event()


def _day_events(
    day: date,
    lessons: list[LessonData],
    data: LibrusData,
    merge: bool,
    owner: Hashable | None = None,
) -> list[CalendarEvent]:
    """Calendar events for one day. With `merge`, lessons with the same
    start and end become one event. Changed lessons (cancelled,
    substitution, room change, moved) always stay separate, so a
    cancelled lesson and its substitution still show as two entries."""
    events: list[CalendarEvent] = []
    groups: dict[tuple[datetime, datetime], list[_LessonParts]] = {}
    for lesson in lessons:
        parts = _lesson_parts(day, lesson, data, owner)
        if parts is None:
            continue
        if merge and not parts.changed:
            groups.setdefault((parts.start, parts.end), []).append(parts)
        else:
            events.append(parts.to_event())
    for group in groups.values():
        events.append(group[0].to_event() if len(group) == 1 else _merge_parts(group))
    events.sort(key=lambda event: event.start)
    return events


def _lesson_parts(
    day: date, lesson: LessonData, data: LibrusData, owner: Hashable | None = None
) -> _LessonParts | None:
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
    change = lesson_change(day, lesson, data)
    summary = subject_name
    if change["kind"] is not None:
        summary = f"{summary} ({_CHANGE_LABELS[change['kind']]})"

    # Kindergarten blocks can list several teachers (PR #8).
    teacher_ids = lesson.teacher_ids or (
        (lesson.teacher_id,) if lesson.teacher_id is not None else ()
    )
    teacher_names = [name for tid in teacher_ids if (name := data.teachers.get(tid))]
    teacher_name = ", ".join(dict.fromkeys(teacher_names)) or None
    classroom_name = (
        data.classrooms.get(lesson.classroom_id) if lesson.classroom_id is not None else None
    )

    # After the teacher (first description line), what a substitution changes.
    lines: list[str] = []
    # The original subject only when it differs ("Zastępstwo za: Jan Kowal"
    # for the same subject with another teacher).
    original_subject = change["original_subject"]
    if original_subject == subject_name:
        original_subject = None
    if change["kind"] == "substitution" and (original_subject or change["original_teacher"]):
        replaced = ", ".join(n for n in (original_subject, change["original_teacher"]) if n)
        lines.append(f"Zastępstwo za: {replaced}")
    if change["room_changed"]:
        lines.append(f"Zmiana sali: {change['original_classroom'] or '?'} → {classroom_name or '?'}")
    topic = _lesson_topic(day, lesson, data, owner)
    if topic:
        lines.append(f"Temat: {topic}")
    if change["kind"] == "moved":
        when = change["original_date"] or ""
        when = f"{when[8:10]}.{when[5:7]}" if len(when) >= 10 else when
        number = change["original_lesson_no"]
        lines.append(f"Przeniesiona z: {when}" + (f", lekcja {number}" if number is not None else ""))

    return _LessonParts(
        start=dt_util.as_local(datetime.combine(day, start_time)),
        end=dt_util.as_local(datetime.combine(day, end_time)),
        summary=summary,
        location=classroom_name,
        teacher=teacher_name,
        details=lines,
        changed=change["kind"] is not None or bool(change["room_changed"]),
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


def _pt_conferences_not_in_agenda(data: LibrusData) -> list[ParentTeacherConferenceData]:
    """Parent-teacher conferences that `HomeWorks` doesn't already list.

    CONFIRMED live (2026-10-03): the same meeting comes through BOTH
    endpoints - same date and same time ("17:00:00" as `HomeWorks.TimeFrom`
    and as `ParentTeacherConferences.Time`), different wording - so merging
    both showed it twice in the Agenda. A conference is skipped when an
    Agenda entry has the same date and time; one without a time can't be
    matched safely and is kept."""
    agenda_slots = {
        (item.date[:10], item.time_from) for item in data.homeworks if item.date and item.time_from
    }
    return [
        item
        for item in data.parent_teacher_conferences
        if not (item.date and item.time and (item.date[:10], item.time) in agenda_slots)
    ]


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


def _sorted_events(events: list[CalendarEvent]) -> list[CalendarEvent]:
    """All-day events by start (then summary) - built once per data object,
    so the current event is the first one not over yet and range queries
    come back in order."""
    return sorted(events, key=lambda event: (event.start, event.summary))


def _agenda_events(data: LibrusData) -> list[CalendarEvent]:
    """Every Agenda entry and every parent-teacher conference HomeWorks
    doesn't already list, as all-day events, sorted."""
    events = [
        event for item in data.homeworks if (event := _homework_to_event(item, data)) is not None
    ]
    events += [
        event
        for item in _pt_conferences_not_in_agenda(data)
        if (event := _pt_conference_to_event(item, data)) is not None
    ]
    return _sorted_events(events)


def _free_day_events(data: LibrusData) -> list[CalendarEvent]:
    return _sorted_events(
        [event for item in data.free_days if (event := _free_day_to_event(item)) is not None]
    )


def _first_not_over(events: list[CalendarEvent], today: date) -> CalendarEvent | None:
    """The first of start-sorted all-day events that isn't over by `today`
    (`end` is exclusive: yesterday's entry ends today and is over)."""
    return next((event for event in events if event.end > today), None)


@dataclass(frozen=True, slots=True)
class _TimetableEvents:
    """The polled timetable's events: per day (for range queries) and all of
    them sorted by start with their start times alongside (for the current/
    next event, found by bisection)."""

    by_day: dict[date, list[CalendarEvent]]
    ordered: list[CalendarEvent]
    starts: list[datetime]


def _timetable_events(data: LibrusData, merge: bool, owner: Hashable | None) -> _TimetableEvents:
    by_day = {
        day: _day_events(day, lessons, data, merge, owner) for day, lessons in data.timetable.items()
    }
    ordered = sorted((event for events in by_day.values() for event in events), key=lambda e: e.start)
    return _TimetableEvents(by_day, ordered, [event.start for event in ordered])


class LibrusTimetableCalendar(CoordinatorEntity[LibrusDataUpdateCoordinator], CalendarEntity):
    """The student's lesson timetable, including known substitutions.

    This week and next come straight from the coordinator's timetable,
    which is fetched on every refresh - so a substitution added today
    shows up in the cards at the next refresh. Dashboards can also ask
    `async_get_events` for other ranges (e.g. "next month"); those weeks
    are fetched on demand and cached by the coordinator (shared with the
    Assist timetable tool - see `LibrusDataUpdateCoordinator.
    async_get_timetable_week`).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "timetable"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_timetable"
        self._attr_device_info = librus_device_info(entry)
        # Changing the option reloads the entry, so reading it once is enough.
        self._merge = bool(
            entry.options.get(CONF_MERGE_PARALLEL_LESSONS, DEFAULT_MERGE_PARALLEL_LESSONS)
        )

    def _polled_events(self) -> _TimetableEvents | None:
        """The polled timetable's events, built once per coordinator data
        object: HA reads `event` on every state write, and every range query
        within this week and next needs the same events."""
        data = self.coordinator.data
        if data is None:
            return None
        owner = self.coordinator.memo_owner
        return _CALENDAR_EVENTS.get(
            data, ("timetable", self._merge), lambda: _timetable_events(data, self._merge, owner),
            owner=owner,
        )

    @property
    def event(self) -> CalendarEvent | None:
        events = self._polled_events()
        if events is None:
            return None
        now = dt_util.now()
        # Sorted by start, so the first one not over yet is the current or
        # next lesson. A lesson never lasts a day: everything that started
        # before `now - 1 day` is over and skipped by bisection instead of
        # being walked through on every state write.
        index = bisect_left(events.starts, now - timedelta(days=1))
        return next((event for event in events.ordered[index:] if event.end >= now), None)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        data = self.coordinator.data
        polled = self._polled_events()
        if data is None or polled is None:
            return []
        # At most ON_DEMAND_WEEKS weeks either side of this one: a whole year
        # (or a far-off range) asked for at once used to mean a request to
        # Librus per week.
        week_starts = on_demand_week_starts(start_date.date(), _inclusive_end_date(end_date))
        if not week_starts:
            return []
        lessons_by_day = await self.coordinator.async_get_timetable_weeks(week_starts)

        events: list[CalendarEvent] = []
        for day, lessons in lessons_by_day.items():
            # The polled weeks' events are already built (same lessons).
            day_events = (
                polled.by_day[day]
                if data.timetable.get(day) is lessons and day in polled.by_day
                else _day_events(day, lessons, data, self._merge, self.coordinator.memo_owner)
            )
            for event in day_events:
                # BUG FIX (2026-09-06, found live): compare the lesson's
                # OWN start/end datetimes against the real [start_date,
                # end_date) window, not a day-level filter derived from
                # `.date()` - the old `day > end_date.date()` check treated
                # an exclusive local-midnight end boundary (e.g. "today",
                # which every card here requests as [today 00:00, tomorrow
                # 00:00)) as inclusive of the next day's `.date()`, so a
                # "today's lessons" query wrongly returned tomorrow's whole
                # timetable too. Confirmed live via
                # ha_config_get_calendar_events on a real Sunday.
                if event.start < end_date and event.end > start_date:
                    events.append(event)
        return events


class LibrusAgendaCalendar(CoordinatorEntity[LibrusDataUpdateCoordinator], CalendarEntity):
    """General agenda/events feed (tests, trips, homework) from `HomeWorks`,
    plus `ParentTeacherConferences` that `HomeWorks` doesn't already list
    (the same meeting usually comes through both - see
    `_pt_conferences_not_in_agenda`).

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
        # `end` of an all-day event is exclusive (the day after): `>` keeps
        # yesterday's entry (end == today) from showing as current all day,
        # same as the free-days calendar below.
        return _first_not_over(self._events(), dt_util.now().date())

    def _events(self) -> list[CalendarEvent]:
        """Every Agenda event, built once per coordinator data object (the
        state, every range query and the next-event lookup all need them)."""
        data = self.coordinator.data
        if data is None:
            return []
        return _CALENDAR_EVENTS.get(
            data, "agenda", lambda: _agenda_events(data), owner=self.coordinator.memo_owner
        )

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        if self.coordinator.data is None:
            return []
        # BUG FIX (2026-09-06): see _inclusive_end_date - a bare
        # `end_date.date()` over-includes one day at an exact local-
        # midnight boundary.
        start, end = start_date.date(), _inclusive_end_date(end_date)
        # BUG FIX (code review): was single-point containment (`start <=
        # event.start <= end`), unlike LibrusFreeDaysCalendar below which
        # already used a proper overlap check for the same class of range
        # query - currently masked because every Agenda event today is
        # single-day (containment and overlap agree for those), but fixed
        # properly now via the shared `_event_overlaps` helper since this
        # exact bug class ("midnight-boundary"-adjacent date-range bugs) has
        # bitten this project multiple times already.
        return [event for event in self._events() if _event_overlaps(event, start, end)]


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
        return _first_not_over(self._events(), dt_util.now().date())

    def _events(self) -> list[CalendarEvent]:
        data = self.coordinator.data
        if data is None:
            return []
        return _CALENDAR_EVENTS.get(
            data, "free_days", lambda: _free_day_events(data), owner=self.coordinator.memo_owner
        )

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        if self.coordinator.data is None:
            return []
        # BUG FIX (2026-09-06): see _inclusive_end_date - a bare
        # `end_date.date()` over-includes one day at an exact local-
        # midnight boundary.
        start, end = start_date.date(), _inclusive_end_date(end_date)
        # Overlap check, not containment - a multi-day break can start
        # before the requested window and/or end after it. See
        # `_event_overlaps` (now shared with LibrusAgendaCalendar above).
        return [event for event in self._events() if _event_overlaps(event, start, end)]
