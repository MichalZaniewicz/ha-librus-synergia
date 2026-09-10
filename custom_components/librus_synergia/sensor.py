"""Sensor platform for the Librus Synergia (unofficial) integration."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import LibrusConfigEntry, librus_device_info
from .const import ATTR_SUBJECT_ID
from .coordinator import LibrusDataUpdateCoordinator
from .librus_api.models import (
    AttendanceTypeData,
    BehaviourGradeData,
    GradeCategoryData,
    GradeData,
    LessonData,
    LibrusData,
    MessageData,
)


def _parse_grade_value(value: str) -> float | None:
    """Convert a Librus grade string ("5+", "4-", "3", "bz"...) to a number.

    The "+"/"-" modifiers (+0.5 / -0.25) follow the convention used by most
    third-party Polish gradebook average calculators. CONFIRMED live
    (2026-09-05) via the `Grades/Types` reference endpoint that every
    non-numeric value Librus actually uses (`bz`, `np`, `nk`, `uł`, `nł`,
    `zl`, `nz`, `zw`, `uc`, `nu`, bare `+`/`-`) correctly falls through to
    returning None here and is excluded from the average - the numeric
    +/- MODIFIER convention itself is still a third-party inference, not
    something Librus documents, since no real numeric grade has been
    issued on the test account yet to check the exact value it produces.
    """
    value = value.strip()
    if not value:
        return None
    modifier = 0.0
    if value.endswith("+"):
        modifier = 0.5
        value = value[:-1]
    elif value.endswith("-"):
        modifier = -0.25
        value = value[:-1]
    try:
        return float(value.replace(",", ".")) + modifier
    except ValueError:
        return None


def _calculate_average(
    grades: list[GradeData],
    categories: dict[int, GradeCategoryData],
    *,
    subject_id: int | None = None,
) -> float | None:
    """Weighted grade average, excluding semester/final proposition entries
    and categories marked as not counting toward the average."""
    weighted_sum = 0.0
    weight_total = 0.0
    for grade in grades:
        if grade.is_semester_proposition or grade.is_final_proposition:
            continue
        if subject_id is not None and grade.subject_id != subject_id:
            continue
        category = categories.get(grade.category_id) if grade.category_id is not None else None
        if category is not None and not category.count_to_average:
            continue
        numeric = _parse_grade_value(grade.value)
        if numeric is None:
            continue
        weight = category.weight if category is not None else 1
        weighted_sum += numeric * weight
        weight_total += weight
    if weight_total <= 0:
        return None
    return round(weighted_sum / weight_total, 2)


def _latest_grade(grades: list[GradeData], *, subject_id: int | None = None) -> GradeData | None:
    candidates = [
        g
        for g in grades
        if not g.is_semester_proposition
        and not g.is_final_proposition
        and (subject_id is None or g.subject_id == subject_id)
        and g.add_date
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda g: g.add_date)


# ----------------------------------------------------------------------
# Timetable-derived helpers (shared by the Next/Current lesson sensors and
# the School sensor's bell-schedule attribute). All client-side over the
# coordinator's already-fetched current+next-week timetable - no extra API
# calls.
# ----------------------------------------------------------------------


def _lesson_bounds(day: date, lesson: LessonData) -> tuple[datetime, datetime] | None:
    """Local-timezone (start, end) datetimes for one lesson, or None if it
    has no usable HourFrom/HourTo (mirrors calendar.py's `_lesson_to_event`
    parsing)."""
    if not lesson.hour_from or not lesson.hour_to:
        return None
    try:
        start_t = datetime.strptime(lesson.hour_from, "%H:%M").time()
        end_t = datetime.strptime(lesson.hour_to, "%H:%M").time()
    except ValueError:
        return None
    return (
        dt_util.as_local(datetime.combine(day, start_t)),
        dt_util.as_local(datetime.combine(day, end_t)),
    )


def _sorted_lessons(data: LibrusData) -> list[tuple[datetime, datetime, date, LessonData]]:
    """Every timetable lesson with a valid time, flattened and sorted by
    start. Keeps parallel-group lessons (a single period split into two
    language classes, say) - both appear, ordered by start then arbitrarily."""
    out: list[tuple[datetime, datetime, date, LessonData]] = []
    for day, lessons in data.timetable.items():
        for lesson in lessons:
            bounds = _lesson_bounds(day, lesson)
            if bounds is not None:
                out.append((bounds[0], bounds[1], day, lesson))
    out.sort(key=lambda item: item[0])
    return out


def _lesson_subject(lesson: LessonData, data: LibrusData) -> str:
    if lesson.subject_id is None:
        return "Lekcja"
    return data.subjects.get(lesson.subject_id, f"Lekcja {lesson.subject_id}")


def _lesson_attrs(
    start: datetime, end: datetime, day: date, lesson: LessonData, data: LibrusData
) -> dict[str, Any]:
    return {
        ATTR_SUBJECT_ID: lesson.subject_id,
        "subject": _lesson_subject(lesson, data),
        "lesson_no": lesson.lesson_no,
        "date": day.isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "teacher": data.teachers.get(lesson.teacher_id) if lesson.teacher_id is not None else None,
        "classroom": (
            data.classrooms.get(lesson.classroom_id) if lesson.classroom_id is not None else None
        ),
        "is_substitution": lesson.is_substitution,
    }


def _bell_schedule(timetable: dict[date, list[LessonData]]) -> list[dict[str, Any]]:
    """A period-number -> {start, end} table derived from whatever times
    actually appear in the timetable. Per lesson number, the most commonly
    seen HourFrom/HourTo pair wins (handles the odd shortened day without
    letting it redefine the normal bell times)."""
    seen: dict[int, dict[tuple[str, str], int]] = {}
    for lessons in timetable.values():
        for lesson in lessons:
            if lesson.lesson_no is None or not lesson.hour_from or not lesson.hour_to:
                continue
            slot = seen.setdefault(lesson.lesson_no, {})
            key = (lesson.hour_from, lesson.hour_to)
            slot[key] = slot.get(key, 0) + 1
    schedule: list[dict[str, Any]] = []
    for lesson_no in sorted(seen):
        (hour_from, hour_to), _count = max(seen[lesson_no].items(), key=lambda kv: kv[1])
        schedule.append({"lesson_no": lesson_no, "start": hour_from, "end": hour_to})
    return schedule


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LibrusConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Librus Synergia sensors from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            LibrusOverallAverageSensor(coordinator, entry),
            LibrusAttendanceSensor(coordinator, entry),
            LibrusNextLessonSensor(coordinator, entry),
            LibrusCurrentLessonSensor(coordinator, entry),
            LibrusLuckyNumberSensor(coordinator, entry),
            LibrusUnreadAnnouncementsSensor(coordinator, entry),
            LibrusBehaviourNoticesSensor(coordinator, entry),
            LibrusUnreadMessagesSensor(coordinator, entry),
            LibrusSchoolSensor(coordinator, entry),
            LibrusClassSensor(coordinator, entry),
            LibrusHomeworkAssignmentsSensor(coordinator, entry),
            LibrusBehaviourGradeSensor(coordinator, entry),
            LibrusDescriptiveGradesSensor(coordinator, entry),
        ]
    )

    # Subjects are only known from live account data - discover new ones as
    # the coordinator sees them and add an average sensor per subject.
    known_subject_ids: set[int] = set()

    def _add_new_subjects() -> None:
        data = coordinator.data
        if data is None:
            return
        # Fall back to subject ids seen on grades even if the (unverified)
        # Subjects lookup hasn't resolved a name for it yet.
        seen_ids = set(data.subjects) | {
            g.subject_id for g in data.grades if g.subject_id is not None
        }
        new_ids = seen_ids - known_subject_ids
        if not new_ids:
            return
        known_subject_ids.update(new_ids)
        async_add_entities(
            LibrusSubjectAverageSensor(coordinator, entry, subject_id) for subject_id in new_ids
        )

    _add_new_subjects()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_subjects))


class LibrusSensorBase(CoordinatorEntity[LibrusDataUpdateCoordinator], SensorEntity):
    """Common bits for every Librus Synergia sensor."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry, key: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = librus_device_info(entry)


class LibrusOverallAverageSensor(LibrusSensorBase):
    """Weighted average across every subject."""

    _attr_translation_key = "overall_average"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "overall_average")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return _calculate_average(self.coordinator.data.grades, self.coordinator.data.grade_categories)


class LibrusSubjectAverageSensor(LibrusSensorBase):
    """Weighted average for a single subject, discovered dynamically."""

    _attr_translation_key = "subject_average"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(
        self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry, subject_id: int
    ) -> None:
        super().__init__(coordinator, entry, f"subject_{subject_id}_average")
        self._subject_id = subject_id

    @property
    def _subject_name(self) -> str:
        if self.coordinator.data is None:
            return str(self._subject_id)
        return self.coordinator.data.subjects.get(self._subject_id, str(self._subject_id))

    @property
    def translation_placeholders(self) -> dict[str, str]:
        return {"subject": self._subject_name}

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return _calculate_average(
            self.coordinator.data.grades,
            self.coordinator.data.grade_categories,
            subject_id=self._subject_id,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        grades = self.coordinator.data.grades
        latest = _latest_grade(grades, subject_id=self._subject_id)
        proposed = next(
            (g for g in grades if g.subject_id == self._subject_id and g.is_semester_proposition),
            None,
        )
        final = next(
            (g for g in grades if g.subject_id == self._subject_id and g.is_final_proposition),
            None,
        )
        subject_grades = [
            g
            for g in grades
            if g.subject_id == self._subject_id
            and not g.is_semester_proposition
            and not g.is_final_proposition
        ]
        count = len(subject_grades)
        categories = self.coordinator.data.grade_categories
        # Full per-grade list for this one subject - lets a dashboard card
        # show the actual grade log, not just the computed average. Sorted
        # newest-first; date is a plain "YYYY-MM-DD"-prefixed string from
        # Librus so lexicographic sort matches chronological order.
        grade_log = [
            {
                "value": g.value,
                "category": categories[g.category_id].name if g.category_id in categories else None,
                "date": g.add_date,
                "comments": g.comments,
            }
            for g in sorted(subject_grades, key=lambda g: g.add_date or "", reverse=True)
        ]
        return {
            ATTR_SUBJECT_ID: self._subject_id,
            # A clean, language-independent name for dashboard cards to key
            # off - the friendly_name is built from a per-language
            # translation string ("{subject} average" vs "Średnia -
            # {subject}"), which is fragile to parse back apart in JS.
            "subject": self._subject_name,
            "latest_grade": latest.value if latest else None,
            "latest_grade_date": latest.add_date if latest else None,
            "latest_grade_comments": latest.comments if latest else [],
            "grade_count": count,
            "grades": grade_log,
            "proposed_semester_grade": proposed.value if proposed else None,
            "final_grade": final.value if final else None,
        }


def _attendance_type(data: LibrusData, type_id: int | None) -> AttendanceTypeData | None:
    return data.attendance_types.get(type_id) if type_id is not None else None


# A non-presence type ("Nieobecność uspr.") can still be an EXCUSED absence -
# Librus has no separate API flag for this (IsPresenceKind only says
# present/not), so "uspr." (skrót od "usprawiedliwiona") in the type's own
# name is the only signal available, same best-effort text-match class as
# the companion cards' own EXCUSED_HINT. Found live: a parent excused a real
# absence and it kept showing identically to an unexcused one in every
# summary/tile view, with no way to tell "already resolved" from "still
# needs attention" without opening the full Attendance card's legend.
_EXCUSED_TYPE_NAME_RE = re.compile(r"uspr\.?", re.IGNORECASE)


def _is_excused_type_name(name: str) -> bool:
    return _EXCUSED_TYPE_NAME_RE.search(name) is not None


# "Spóźnienie" (late) is a PRESENCE-kind type (IsPresenceKind: true, same
# flag value as plain "Obecność") - Librus has no separate API flag telling
# late apart from ordinary presence either, so (same best-effort text-match
# approach as _is_excused_type_name above) "późn" (rdzeń for "spóźnienie")
# in the type's own name is the only signal. Needed for by_weekday below -
# _record_status()/by_date fold "late" into plain "good", which is fine for
# a single worst-status-per-day heatmap but can't answer "how many lates
# happened on a given weekday" on its own.
_LATE_TYPE_NAME_RE = re.compile(r"późn", re.IGNORECASE)


def _is_late_type_name(name: str) -> bool:
    return _LATE_TYPE_NAME_RE.search(name) is not None


# Same three-way status used by the companion cards' own attendanceStatus()
# (good=present, warn=excused absence, bad=unexcused absence) - computed
# once here so a per-day heatmap card doesn't need to re-derive it from
# raw type names itself.
_STATUS_RANK = {"good": 0, "warn": 1, "bad": 2}


def _record_status(attendance_type: AttendanceTypeData | None) -> str:
    if attendance_type is None:
        return "good"  # unknown type - no evidence to flag it as concerning
    if attendance_type.is_presence_kind:
        return "good"
    return "warn" if _is_excused_type_name(attendance_type.name) else "bad"


class LibrusAttendanceSensor(LibrusSensorBase):
    """Count of real absences - excludes "present"/"late"/"excused" marks.

    CONFIRMED live: the overwhelming majority of attendance records are
    ordinary "Obecność" (present) marks (`IsPresenceKind: true`), so a raw
    total-record count mostly just tracks how many lessons happened, not
    anything a parent cares about. This counts only types the school itself
    classifies as NOT a presence kind (real absences, excused or not); the
    full breakdown (including presence marks) is still in attributes.
    """

    _attr_translation_key = "attendance"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:calendar-remove"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "attendance")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        data = self.coordinator.data
        return sum(
            1
            for a in data.attendances
            if (t := _attendance_type(data, a.type_id)) is not None and not t.is_presence_kind
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        data = self.coordinator.data
        breakdown: dict[str, int] = {}
        # Whether each breakdown NAME counts as a presence, per the school's
        # own AttendanceTypes[].IsPresenceKind - not every "sounds like an
        # absence" name actually is one (e.g. "Spóźnienie"/late and
        # "Zwolnienie"/excused-release both count as present) and, in the
        # other direction, "Nieobecność" contains "obecność" as a literal
        # substring, so a consumer guessing from the name text alone (a real
        # bug found live in the companion cards - both "Obecność" and
        # "Nieobecność" rendered with the same color) gets it wrong. Expose
        # the authoritative flag instead of making every consumer re-guess it.
        presence_by_type: dict[str, bool] = {}
        for attendance in data.attendances:
            attendance_type = _attendance_type(data, attendance.type_id)
            name = attendance_type.name if attendance_type is not None else str(attendance.type_id)
            breakdown[name] = breakdown.get(name, 0) + 1
            if attendance_type is not None:
                presence_by_type[name] = attendance_type.is_presence_kind
        # Most recent real-absence date, for a "days since last absence"
        # streak card - date strings are "YYYY-MM-DD"-prefixed so a plain
        # max() over them matches chronological order.
        absence_dates = [
            a.date
            for a in data.attendances
            if a.date and (t := _attendance_type(data, a.type_id)) is not None and not t.is_presence_kind
        ]

        # Split the main "absences" count into excused/unexcused - found
        # live: a parent excused a real absence and it kept showing
        # identically to an unexcused one in every summary/tile view (only
        # the full card's per-type legend distinguished them at all). The
        # bare total is still the sensor's own state (unchanged meaning -
        # both still count as "not present"); this lets a summary/tile
        # surface "N still need attention" instead of a blended figure.
        excused_count = 0
        unexcused_count = 0
        for a in data.attendances:
            t = _attendance_type(data, a.type_id)
            if t is None or t.is_presence_kind:
                continue
            if _is_excused_type_name(t.name):
                excused_count += 1
            else:
                unexcused_count += 1

        # One status per calendar DATE (not per record - a single day can
        # carry several period-level records), for a "year at a glance"
        # heatmap card. A day with multiple records takes its WORST status
        # (bad > warn > good) - one unexcused-absence period that day is
        # what a parent needs to see, even if the other periods were
        # present.
        by_date: dict[str, str] = {}
        for a in data.attendances:
            if not a.date:
                continue
            status = _record_status(_attendance_type(data, a.type_id))
            existing = by_date.get(a.date)
            if existing is None or _STATUS_RANK[status] > _STATUS_RANK[existing]:
                by_date[a.date] = status

        # Independent attendance-percentage calculation - inspired by a
        # feature comparison against dani3l0/librusik (a third-party
        # Librus web client), which computes this itself rather than
        # relying on Librus's own UI showing it (some schools disable
        # theirs). AttendanceData.semester was already parsed but never
        # actually used until now.
        total_records = len(data.attendances)
        presence_records = sum(
            1
            for a in data.attendances
            if (t := _attendance_type(data, a.type_id)) is not None and t.is_presence_kind
        )
        percentage = round(100 * presence_records / total_records, 1) if total_records else None

        by_semester: dict[str, dict[str, Any]] = {}
        for a in data.attendances:
            if a.semester is None:
                continue
            bucket = by_semester.setdefault(str(a.semester), {"total": 0, "present": 0})
            bucket["total"] += 1
            t = _attendance_type(data, a.type_id)
            if t is not None and t.is_presence_kind:
                bucket["present"] += 1
        for bucket in by_semester.values():
            bucket["percentage"] = (
                round(100 * bucket["present"] / bucket["total"], 1) if bucket["total"] else None
            )

        # Same excused/unexcused/late split as above, but grouped by ISO
        # WEEKDAY (1=Monday..7=Sunday, string keys for JSON) instead of by
        # calendar date - for a "which day of the week is this happening
        # on" chart. Unlike by_date (one status per DAY, and late folds
        # into "good" there), this counts every matching RECORD, so a day
        # with two late periods counts twice - by_date intentionally can't
        # answer this question at all (a single status per day has no room
        # for a fourth "late" value alongside good/warn/bad).
        by_weekday: dict[str, dict[str, int]] = {}
        for a in data.attendances:
            if not a.date:
                continue
            t = _attendance_type(data, a.type_id)
            if t is None:
                continue
            if t.is_presence_kind:
                if not _is_late_type_name(t.name):
                    continue  # ordinary presence - not part of this breakdown
                bucket_key = "late"
            else:
                bucket_key = "excused" if _is_excused_type_name(t.name) else "unexcused"
            try:
                weekday = date.fromisoformat(a.date).isoweekday()
            except ValueError:
                continue
            day_bucket = by_weekday.setdefault(
                str(weekday), {"excused": 0, "unexcused": 0, "late": 0}
            )
            day_bucket[bucket_key] += 1

        return {
            "breakdown": breakdown,
            "presence_by_type": presence_by_type,
            "total_records": total_records,
            "last_absence_date": max(absence_dates) if absence_dates else None,
            "percentage": percentage,
            "by_semester": by_semester,
            "excused_count": excused_count,
            "unexcused_count": unexcused_count,
            "by_date": by_date,
            "by_weekday": by_weekday,
        }


class LibrusLuckyNumberSensor(LibrusSensorBase):
    """The most recently published "szczęśliwy numerek" (lucky number).

    BUG FIX (2026-09-06, found live): the state was always presented as
    "today's" number (the card's own subtitle literally says so) without
    ever checking Librus's own `LuckyNumberDay` field against today's real
    date - CONFIRMED live (user cross-checked against the real Librus app)
    that Librus can publish the NEXT school day's number a day ahead (e.g.
    Monday's number visible already on Sunday), and this integration was
    showing that value as if it were for today regardless. The number
    itself is still the state (still the single most useful "latest known"
    value, matching what the raw sensor showed before), but `day`/
    `is_today` are now exposed so a card can label it honestly instead of
    hardcoding "today".
    """

    _attr_translation_key = "lucky_number"
    _attr_icon = "mdi:dice-5"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "lucky_number")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None or self.coordinator.data.lucky_number is None:
            return None
        return self.coordinator.data.lucky_number.number

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None or self.coordinator.data.lucky_number is None:
            return None
        day = self.coordinator.data.lucky_number.day
        return {
            "day": day,
            "is_today": day == dt_util.now().date().isoformat() if day else None,
        }


class LibrusUnreadAnnouncementsSensor(LibrusSensorBase):
    """Count of school notices ("ogłoszenia") not yet marked read, with a
    recent-items attribute (subject/content preview/dates) matching the
    Behaviour notices and Unread messages sensors' pattern."""

    _attr_translation_key = "unread_announcements"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:bullhorn"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "unread_announcements")

    def _unread(self) -> list[Any]:
        if self.coordinator.data is None:
            return []
        return [n for n in self.coordinator.data.school_notices if not n.was_read]

    @property
    def native_value(self) -> int:
        return len(self._unread())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "recent": [
                {
                    "id": n.id,
                    "subject": n.subject,
                    # Unlike the Wiadomości mailboxes, Librus does NOT
                    # truncate this endpoint's content server-side - it was
                    # this integration that used to cut it to 200 chars for
                    # no real reason, making the full text impossible for a
                    # card to ever show. Expose it whole; there's no read-
                    # marking side effect or extra API call to worry about
                    # here, unlike the Wiadomości "click to read" feature.
                    "content": n.content,
                    "start_date": n.start_date,
                    "end_date": n.end_date,
                    "creation_date": n.creation_date,
                }
                for n in self._unread()[:10]
            ]
        }


class LibrusBehaviourNoticesSensor(LibrusSensorBase):
    """Count of behaviour notices ("uwagi"), with a short recent-items list."""

    _attr_translation_key = "behaviour_notices"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "behaviour_notices")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return len(self.coordinator.data.notes)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        recent = sorted(
            (n for n in self.coordinator.data.notes if n.date),
            key=lambda n: n.date,
            reverse=True,
        )[:5]
        categories = self.coordinator.data.note_categories
        return {
            "recent": [
                {
                    "date": n.date,
                    "sentiment": n.sentiment,
                    "category": categories.get(n.category_id) if n.category_id else None,
                    "text": n.text[:200],
                }
                for n in recent
            ]
        }


class LibrusHomeworkAssignmentsSensor(LibrusSensorBase):
    """Count of real homework assignments ("zadania domowe") - distinct
    from the Agenda calendar's general `HomeWorks` feed (tests/trips/etc.
    too). Fields CONFIRMED via szkolny-android's reference parser
    (2026-09-06), but never seen populated - the test account's
    `HomeWorkAssignments` endpoint has always been empty."""

    _attr_translation_key = "homework_assignments"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:notebook-edit-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "homework_assignments")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return len(self.coordinator.data.homework_assignments)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        teachers = self.coordinator.data.teachers
        recent = sorted(
            (a for a in self.coordinator.data.homework_assignments if a.due_date),
            key=lambda a: a.due_date,
        )[:10]
        return {
            "recent": [
                {
                    "topic": a.topic,
                    "text": a.text[:200],
                    "due_date": a.due_date,
                    "date": a.date,
                    "teacher": teachers.get(a.teacher_id) if a.teacher_id else None,
                }
                for a in recent
            ]
        }


class LibrusBehaviourGradeSensor(LibrusSensorBase):
    """A formal "ocena zachowania" (behaviour grade) - distinct from the
    Behaviour notices sensor above ("uwagi", free-text remarks). State is
    the most recent grade's short name (e.g. "wz" for "wzorowe") if any
    exist. Fields CONFIRMED via szkolny-android's reference parser
    (2026-09-06), but never seen populated."""

    _attr_translation_key = "behaviour_grade"
    _attr_icon = "mdi:medal-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "behaviour_grade")

    def _latest(self) -> BehaviourGradeData | None:
        if self.coordinator.data is None:
            return None
        graded = [g for g in self.coordinator.data.behaviour_grades if g.add_date]
        if not graded:
            return None
        return max(graded, key=lambda g: g.add_date)

    @property
    def native_value(self) -> str | None:
        latest = self._latest()
        return latest.short_name if latest else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        categories = self.coordinator.data.behaviour_grade_categories
        recent = sorted(
            (g for g in self.coordinator.data.behaviour_grades if g.add_date),
            key=lambda g: g.add_date,
            reverse=True,
        )[:5]
        return {
            "recent": [
                {
                    "short_name": g.short_name,
                    "value": g.value,
                    "category": categories.get(g.category_id) if g.category_id else None,
                    "date": g.add_date,
                    "text": g.text[:200],
                    "comments": g.comments,
                }
                for g in recent
            ]
        }


class LibrusDescriptiveGradesSensor(LibrusSensorBase):
    """An alternate, non-numeric grading system - CONFIRMED enabled for
    this school (via `Units`), unlike `PointGrades`. Fields CONFIRMED via
    szkolny-android's reference parser (2026-09-06), but never seen
    populated."""

    _attr_translation_key = "descriptive_grades"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:text-box-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "descriptive_grades")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return len(self.coordinator.data.descriptive_grades)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        subjects = self.coordinator.data.subjects
        recent = sorted(
            (g for g in self.coordinator.data.descriptive_grades if g.add_date),
            key=lambda g: g.add_date,
            reverse=True,
        )[:5]
        return {
            "recent": [
                {
                    "subject": subjects.get(g.subject_id) if g.subject_id else None,
                    "value": g.value,
                    "skill_id": g.skill_id,
                    "category_id": g.category_id,
                    "date": g.add_date,
                }
                for g in recent
            ]
        }


def _message_list_attr(messages: list[MessageData]) -> list[dict[str, Any]]:
    """Same shape used for every mailbox's `*_recent` attribute - `id` +
    `mailbox` together are what a card needs to pass to the `get_message`
    service to load a specific message's full content."""
    return [
        {
            "id": m.id,
            "mailbox": m.mailbox,
            "sender": m.sender_name,
            "topic": m.topic,
            "content": m.content[:200],
            "date": m.send_date,
            "unread": m.read_date is None,
            "has_attachment": m.has_attachment,
        }
        for m in messages[:10]
    ]


class LibrusUnreadMessagesSensor(LibrusSensorBase):
    """Unread count in the main Wiadomości inbox, with a preview list.

    Reading this sensor never marks anything read in real Librus - the
    coordinator only ever calls the message LIST/count endpoints, never a
    single-message detail endpoint (see LibrusApiClient's Wiadomości
    methods). `unknown` if this Librus install doesn't have the messages
    module enabled at all (`messages_available=False`), rather than 0 -
    those are different situations and shouldn't look the same.
    """

    _attr_translation_key = "unread_messages"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:email-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "unread_messages")

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self.coordinator.data.messages_available
        )

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.unread_message_count

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        data = self.coordinator.data
        return {
            "mailbox_breakdown": dict(data.unread_messages_by_mailbox),
            "recent": _message_list_attr(data.messages),
            # Full CONTENT (not just the count already in
            # mailbox_breakdown) for the secondary mailboxes most worth
            # actually reading - "substitutions" (zastępstwa, schedule
            # changes), "alerts" (alerty), and "justifications"
            # (usprawiedliwienia - a parent's submitted absence excuse and
            # its pending/accepted status, added 2026-09-06 on user
            # request).
            "substitutions_recent": _message_list_attr(data.substitution_messages),
            "alerts_recent": _message_list_attr(data.alert_messages),
            "justifications_recent": _message_list_attr(data.justification_messages),
        }


class LibrusSchoolSensor(LibrusSensorBase):
    """The student's school - state is the school name, attributes carry
    address/contact/head-teacher/bell-schedule details. Near-static
    (refreshed on the same 24h cadence as subjects/teachers/classrooms)."""

    _attr_translation_key = "school"
    _attr_icon = "mdi:school"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "school")

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None or self.coordinator.data.school is None:
            return None
        return self.coordinator.data.school.name or None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None or self.coordinator.data.school is None:
            return None
        school = self.coordinator.data.school
        return {
            "town": school.town,
            "street": school.street,
            "building_number": school.building_number,
            "post_code": school.post_code,
            "head_teacher": school.head_teacher_name,
            "email": school.email,
            "phone_number": school.phone_number,
            # Bell schedule (period number -> start/end time), derived from
            # the times that actually appear in this student's timetable -
            # lets a card show "period 3 = 09:40-10:25" without hardcoding.
            "bell_schedule": _bell_schedule(self.coordinator.data.timetable),
        }


class LibrusClassSensor(LibrusSensorBase):
    """The student's class - state is the short class name (e.g. "7d"),
    attributes carry the homeroom teacher and semester boundary dates."""

    _attr_translation_key = "school_class"
    _attr_icon = "mdi:google-classroom"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "school_class")

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None or self.coordinator.data.school_class is None:
            return None
        return self.coordinator.data.school_class.display_name or None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None or self.coordinator.data.school_class is None:
            return None
        data = self.coordinator.data
        cls = data.school_class
        tutor = data.teachers.get(cls.tutor_id) if cls.tutor_id is not None else None
        return {
            "homeroom_teacher": tutor,
            "school_year_start": cls.begin_school_year,
            "first_semester_end": cls.end_first_semester,
            "school_year_end": cls.end_school_year,
        }


class LibrusNextLessonSensor(LibrusSensorBase):
    """The next lesson that will actually take place (cancelled slots are
    skipped). State is the subject name; attributes carry the start/end
    time, `minutes_until`, teacher, classroom and whether it's a
    substitution - everything a "leaving for school" TTS or a countdown
    card needs, without each consumer re-deriving it from the timetable
    calendar. Client-side over the coordinator's current+next-week window,
    so it can see through to Monday from a Friday evening but not further."""

    _attr_translation_key = "next_lesson"
    _attr_icon = "mdi:clock-start"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "next_lesson")

    def _pick(self) -> tuple[datetime, datetime, date, LessonData] | None:
        if self.coordinator.data is None:
            return None
        now = dt_util.now()
        for start, end, day, lesson in _sorted_lessons(self.coordinator.data):
            if lesson.is_canceled or start <= now:
                continue
            return start, end, day, lesson
        return None

    @property
    def native_value(self) -> str | None:
        picked = self._pick()
        return _lesson_subject(picked[3], self.coordinator.data) if picked else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        picked = self._pick()
        if picked is None:
            return None
        start, end, day, lesson = picked
        attrs = _lesson_attrs(start, end, day, lesson, self.coordinator.data)
        attrs["minutes_until"] = max(0, int((start - dt_util.now()).total_seconds() // 60))
        return attrs


class LibrusCurrentLessonSensor(LibrusSensorBase):
    """The lesson happening right now (`unknown` during breaks / outside
    school hours). State is the subject name; attributes carry `minutes_left`
    and the same teacher/classroom/period detail as the Next lesson
    sensor."""

    _attr_translation_key = "current_lesson"
    _attr_icon = "mdi:clock-time-four-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "current_lesson")

    def _pick(self) -> tuple[datetime, datetime, date, LessonData] | None:
        if self.coordinator.data is None:
            return None
        now = dt_util.now()
        for start, end, day, lesson in _sorted_lessons(self.coordinator.data):
            if lesson.is_canceled:
                continue
            if start <= now <= end:
                return start, end, day, lesson
        return None

    @property
    def native_value(self) -> str | None:
        picked = self._pick()
        return _lesson_subject(picked[3], self.coordinator.data) if picked else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        picked = self._pick()
        if picked is None:
            return None
        start, end, day, lesson = picked
        attrs = _lesson_attrs(start, end, day, lesson, self.coordinator.data)
        attrs["minutes_left"] = max(0, int((end - dt_util.now()).total_seconds() // 60))
        return attrs
