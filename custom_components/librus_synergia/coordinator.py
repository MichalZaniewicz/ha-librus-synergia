"""Data update coordinator for the Librus Synergia (unofficial) integration.

Single coordinator, not split by cadence: unlike ha-suunto (live heart rate
vs. sleep/workouts genuinely differ in volatility), Librus data - grades,
attendance, timetable, announcements - all change at "a few times a day at
most" cadence, so one coordinator covers everything. The one exception (the
daily lucky number) is special-cased inline rather than given its own
coordinator, for the same reason ha-suunto special-cases its one-off VO2max
deep-scan inside its normal coordinator instead of adding new machinery for a
single edge case.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import EVENT_NEW_ANNOUNCEMENT, EVENT_NEW_GRADE, EVENT_NEW_NOTE, LUCKY_NUMBER_PUBLISH_HOUR
from .librus_api import LibrusApiClient, LibrusAuthError, LibrusError
from .librus_api.models import (
    AttendanceData,
    GradeCategoryData,
    GradeData,
    HomeworkEventData,
    LessonData,
    LibrusData,
    LuckyNumberData,
    MeData,
    NoteData,
    SchoolNoticeData,
)

_LOGGER = logging.getLogger(__name__)


class LibrusDataUpdateCoordinator(DataUpdateCoordinator[LibrusData]):
    """Fetches everything Librus Synergia exposes for one student."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: LibrusApiClient,
        scan_interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"Librus Synergia ({client.username})",
            update_interval=scan_interval,
            config_entry=entry,
        )
        self._client = client

        # Subjects/teachers/classrooms are near-static reference data -
        # refetched at most once a day rather than every cycle.
        self._reference_data_fetched_at: datetime | None = None
        self._cached_subjects: dict[int, str] = {}
        self._cached_teachers: dict[int, str] = {}
        self._cached_classrooms: dict[int, str] = {}

        # The lucky number is normally published once a day; skip refetching
        # it before LUCKY_NUMBER_PUBLISH_HOUR once today's value is cached.
        self._cached_lucky_number: LuckyNumberData | None = None
        self._lucky_number_fetched_date: date | None = None

        # New-item bus events. In-memory only, None = never populated (the
        # next cycle just seeds it instead of replaying history as "new" on
        # first install). A HA restart re-seeds quietly instead of persisting
        # across restarts - same tradeoff ha-suunto makes for its
        # EVENT_NEW_WORKOUT tracking, and for the same reason: a few hundred
        # small ids a year is cheap to hold, not worth a Store-backed file.
        self._known_grade_ids: set[int] | None = None
        # SchoolNotices ids are strings (e.g. "LID-NBOARD-NOTICE-..."),
        # confirmed live - unlike every other endpoint's plain int ids.
        self._known_notice_ids: set[str] | None = None
        self._known_note_ids: set[int] | None = None

    @property
    def client(self) -> LibrusApiClient:
        """Expose the client so calendar entities can fetch arbitrary weeks
        on demand (dashboards can ask CalendarEntity.async_get_events for
        ranges outside this coordinator's current+next-week cache)."""
        return self._client

    async def _async_update_data(self) -> LibrusData:
        assert self.config_entry is not None
        try:
            await self._client.async_ensure_session_valid(self.config_entry.data[CONF_PASSWORD])
        except LibrusAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except LibrusError as err:
            raise UpdateFailed(str(err)) from err

        today = dt_util.now().date()
        week_start = today - timedelta(days=today.weekday())
        next_week_start = week_start + timedelta(days=7)

        try:
            (
                me_payload,
                grades_payload,
                categories_payload,
                notes_payload,
                attendances_payload,
                attendance_types_payload,
                timetable_this_week,
                timetable_next_week,
                homeworks_payload,
                notices_payload,
            ) = await asyncio.gather(
                self._client.async_get_me(),
                self._client.async_get_grades(),
                self._client.async_get_grade_categories(),
                self._client.async_get_notes(),
                self._client.async_get_attendances(),
                self._client.async_get_attendance_types(),
                self._client.async_get_timetable(week_start),
                self._client.async_get_timetable(next_week_start),
                self._client.async_get_homeworks(),
                self._client.async_get_school_notices(),
            )
        except LibrusAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except LibrusError as err:
            raise UpdateFailed(str(err)) from err

        lucky_number = await self._async_get_lucky_number(today)
        await self._async_refresh_reference_data()

        grades = _parse_grades(grades_payload)
        school_notices = _parse_school_notices(notices_payload)
        notes = _parse_notes(notes_payload)
        self._async_fire_new_item_events(grades, school_notices, notes)

        return LibrusData(
            me=_parse_me(me_payload),
            grades=grades,
            grade_categories=_parse_grade_categories(categories_payload),
            notes=notes,
            attendances=_parse_attendances(attendances_payload),
            attendance_types=_parse_attendance_types(attendance_types_payload),
            timetable=merge_timetables(timetable_this_week, timetable_next_week),
            homeworks=_parse_homeworks(homeworks_payload),
            school_notices=school_notices,
            lucky_number=lucky_number,
            subjects=self._cached_subjects,
            teachers=self._cached_teachers,
            classrooms=self._cached_classrooms,
        )

    async def _async_get_lucky_number(self, today: date) -> LuckyNumberData | None:
        now = dt_util.now()
        if (
            self._cached_lucky_number is not None
            and self._lucky_number_fetched_date == today
            and now.hour < LUCKY_NUMBER_PUBLISH_HOUR
        ):
            return self._cached_lucky_number
        try:
            payload = await self._client.async_get_lucky_number()
        except LibrusError:
            _LOGGER.debug("Lucky number fetch failed (non-fatal)", exc_info=True)
            return self._cached_lucky_number
        lucky = _parse_lucky_number(payload)
        if lucky is not None:
            self._cached_lucky_number = lucky
            self._lucky_number_fetched_date = today
        return self._cached_lucky_number

    async def _async_refresh_reference_data(self) -> None:
        """Refresh subject/teacher/classroom name lookups at most once a day.

        These three endpoint names are UNVERIFIED (see
        scripts/manual_smoke_test.py) - a failure here is non-fatal and
        entities fall back to showing the raw numeric id instead of a name.
        """
        now = dt_util.utcnow()
        if (
            self._reference_data_fetched_at is not None
            and now - self._reference_data_fetched_at < timedelta(hours=24)
        ):
            return
        try:
            subjects_payload, teachers_payload, classrooms_payload = await asyncio.gather(
                self._client.async_get_subjects(),
                self._client.async_get_teachers(),
                self._client.async_get_classrooms(),
            )
        except LibrusError:
            _LOGGER.warning(
                "Could not refresh subject/teacher/classroom names (endpoint "
                "names are unverified for this school) - entities will show "
                "raw numeric ids instead of names",
                exc_info=True,
            )
            return
        self._cached_subjects = _parse_id_name_map(subjects_payload, ("Subjects",))
        self._cached_teachers = _parse_id_name_map(teachers_payload, ("Users", "Teachers"))
        self._cached_classrooms = _parse_id_name_map(classrooms_payload, ("Classrooms",))
        self._reference_data_fetched_at = now

    def _async_fire_new_item_events(
        self, grades: list[GradeData], notices: list[SchoolNoticeData], notes: list[NoteData]
    ) -> None:
        entry_id = self.config_entry.entry_id if self.config_entry else None
        self._known_grade_ids = self._fire_for_new_ids(
            EVENT_NEW_GRADE,
            entry_id,
            self._known_grade_ids,
            {g.id: {"subject_id": g.subject_id, "value": g.value} for g in grades},
        )
        self._known_notice_ids = self._fire_for_new_ids(
            EVENT_NEW_ANNOUNCEMENT,
            entry_id,
            self._known_notice_ids,
            {n.id: {"subject": n.subject} for n in notices},
        )
        self._known_note_ids = self._fire_for_new_ids(
            EVENT_NEW_NOTE,
            entry_id,
            self._known_note_ids,
            {n.id: {"positive": n.positive} for n in notes},
        )

    def _fire_for_new_ids(
        self,
        event: str,
        entry_id: str | None,
        known: set[Any] | None,
        items: dict[Any, dict[str, Any]],
    ) -> set[Any]:
        current_ids = set(items)
        if known is None:
            # First-ever refresh for this entry: seed silently. Firing here
            # would replay the account's whole history as "new" on install.
            return current_ids
        new_ids = current_ids - known
        for item_id in new_ids:
            self.hass.bus.async_fire(event, {"entry_id": entry_id, "id": item_id, **items[item_id]})
        # Union, not replace: an item that later drops out of the fetch
        # window must not be re-announced if it reappears.
        return known | current_ids


# ----------------------------------------------------------------------
# Parsing. Defensive throughout (missing keys default sensibly) - Librus
# doesn't publish a schema and per-school variations are known to exist
# upstream (see RustySnek/librus-apix's README).
# ----------------------------------------------------------------------


def _parse_me(payload: dict[str, Any]) -> MeData:
    account = (payload.get("Me") or {}).get("Account") or {}
    return MeData(
        account_id=account.get("Id"),
        first_name=account.get("FirstName", ""),
        last_name=account.get("LastName", ""),
    )


def _parse_grade_categories(payload: dict[str, Any]) -> dict[int, GradeCategoryData]:
    items = payload.get("Categories")
    if not isinstance(items, list):
        return {}
    result: dict[int, GradeCategoryData] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        item_id = int(item["Id"])
        result[item_id] = GradeCategoryData(
            id=item_id,
            name=item.get("Name", ""),
            count_to_average=bool(item.get("CountToTheAverage", True)),
            weight=int(item.get("Weight") or 1),
        )
    return result


def _parse_grades(payload: dict[str, Any]) -> list[GradeData]:
    items = payload.get("Grades")
    if not isinstance(items, list):
        return []
    grades: list[GradeData] = []
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        category = item.get("Category") or {}
        subject = item.get("Subject") or {}
        comments = [
            comment.get("Text", "")
            for comment in (item.get("Comments") or [])
            if isinstance(comment, dict) and comment.get("Text")
        ]
        grades.append(
            GradeData(
                id=int(item["Id"]),
                value=str(item.get("Grade", "")),
                category_id=category.get("Id"),
                subject_id=subject.get("Id"),
                semester=item.get("Semester"),
                add_date=item.get("AddDate"),
                is_semester_proposition=bool(item.get("IsSemesterProposition")),
                is_final_proposition=bool(item.get("IsFinalProposition")),
                comments=comments,
            )
        )
    return grades


def _parse_notes(payload: dict[str, Any]) -> list[NoteData]:
    items = payload.get("Notes")
    if not isinstance(items, list):
        return []
    notes: list[NoteData] = []
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        category = item.get("Category") or {}
        teacher = item.get("Teacher") or {}
        notes.append(
            NoteData(
                id=int(item["Id"]),
                text=item.get("Text", ""),
                category_id=category.get("Id"),
                teacher_id=teacher.get("Id"),
                date=item.get("Date"),
                positive=item.get("Positive"),
            )
        )
    return notes


def _parse_attendances(payload: dict[str, Any]) -> list[AttendanceData]:
    items = payload.get("Attendances")
    if not isinstance(items, list):
        return []
    attendances: list[AttendanceData] = []
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        lesson = item.get("Lesson") or {}
        type_ = item.get("Type") or {}
        attendances.append(
            AttendanceData(
                id=int(item["Id"]),
                lesson_id=lesson.get("Id"),
                lesson_no=item.get("LessonNo"),
                date=item.get("Date"),
                semester=item.get("Semester"),
                type_id=type_.get("Id"),
            )
        )
    return attendances


def _parse_attendance_types(payload: dict[str, Any]) -> dict[int, str]:
    # CONFIRMED live: the response root key is "Types" (matching the
    # Attendances/Types endpoint path), not "AttendanceTypes".
    items = payload.get("Types")
    if not isinstance(items, list):
        return {}
    result: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        name = item.get("Name") or item.get("Short") or item.get("Shortcut") or ""
        result[int(item["Id"])] = name
    return result


def _as_int(value: Any) -> int | None:
    """Coerce an id to int. CONFIRMED live: Timetables returns Subject/
    Teacher/Classroom/Lesson ids as STRINGS ("41999"), unlike every other
    endpoint (Grades, Attendances, ...) which use plain ints - normalize
    here so lookups against `subjects`/`teachers`/`classrooms` (keyed by
    int) work regardless of which endpoint an id came from."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_lesson(raw: dict[str, Any]) -> LessonData:
    subject = raw.get("Subject") or {}
    teacher = raw.get("Teacher") or {}
    classroom = raw.get("Classroom") or {}
    return LessonData(
        lesson_no=_as_int(raw.get("LessonNo")),
        hour_from=raw.get("HourFrom"),
        hour_to=raw.get("HourTo"),
        subject_id=_as_int(subject.get("Id")),
        teacher_id=_as_int(teacher.get("Id")),
        classroom_id=_as_int(classroom.get("Id")),
        is_canceled=bool(raw.get("IsCanceled")),
        is_substitution=bool(raw.get("IsSubstitutionClass")),
    )


def merge_timetables(*payloads: dict[str, Any]) -> dict[date, list[LessonData]]:
    """Merge one or more `Timetables?weekStart=...` responses into a single
    date-keyed dict of lessons.

    CONFIRMED live: each date maps to a list of PERIOD SLOTS (one per
    lesson-number, always the same length even on days with no school),
    each itself a list of 0+ lesson dicts (more than one when a period is
    split into parallel groups, e.g. two language classes at once) - NOT a
    flat list of lessons per day as the reverse-engineered spec assumed.
    """
    result: dict[date, list[LessonData]] = {}
    for payload in payloads:
        timetable = payload.get("Timetable")
        if not isinstance(timetable, dict):
            continue
        for date_str, day_slots in timetable.items():
            if not isinstance(day_slots, list):
                continue
            try:
                day = date.fromisoformat(date_str)
            except (TypeError, ValueError):
                continue
            lessons: list[LessonData] = []
            for slot in day_slots:
                if not isinstance(slot, list):
                    continue
                lessons.extend(_parse_lesson(lesson) for lesson in slot if isinstance(lesson, dict))
            result[day] = lessons
    return result


def _parse_homeworks(payload: dict[str, Any]) -> list[HomeworkEventData]:
    items = payload.get("HomeWorks")
    if not isinstance(items, list):
        return []
    events: list[HomeworkEventData] = []
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        category = item.get("Category") or {}
        subject = item.get("Subject") or {}
        events.append(
            HomeworkEventData(
                id=int(item["Id"]),
                date=item.get("Date"),
                content=item.get("Content", ""),
                category_id=category.get("Id"),
                subject_id=subject.get("Id"),
                time_from=item.get("TimeFrom"),
            )
        )
    return events


def _parse_school_notices(payload: dict[str, Any]) -> list[SchoolNoticeData]:
    items = payload.get("SchoolNotices")
    if not isinstance(items, list):
        return []
    notices: list[SchoolNoticeData] = []
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        notices.append(
            SchoolNoticeData(
                id=str(item["Id"]),
                subject=item.get("Subject", ""),
                content=item.get("Content", ""),
                start_date=item.get("StartDate"),
                end_date=item.get("EndDate"),
                creation_date=item.get("CreationDate"),
                was_read=bool(item.get("WasRead")),
            )
        )
    return notices


def _parse_lucky_number(payload: dict[str, Any]) -> LuckyNumberData | None:
    raw = payload.get("LuckyNumber")
    if not isinstance(raw, dict) or raw.get("LuckyNumber") is None:
        return None
    try:
        number = int(raw["LuckyNumber"])
    except (TypeError, ValueError):
        return None
    return LuckyNumberData(day=raw.get("LuckyNumberDay"), number=number)


def _parse_id_name_map(payload: dict[str, Any], list_keys: tuple[str, ...]) -> dict[int, str]:
    items: Any = None
    for key in list_keys:
        if key in payload:
            items = payload[key]
            break
    if not isinstance(items, list):
        return {}
    result: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        # CONFIRMED live: some Users entries (school admin/secretariat
        # accounts) have FirstName explicitly `null`, not just absent - `or
        # ""` is required here, `.get(key, "")` alone does NOT catch a
        # present-but-None value.
        first = item.get("FirstName") or ""
        last = item.get("LastName") or ""
        name = item.get("Name") or f"{first} {last}".strip()
        if name:
            result[int(item["Id"])] = name
    return result
