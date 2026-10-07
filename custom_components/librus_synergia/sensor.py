"""Sensor platform for the Librus Synergia (unofficial) integration."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from librus_synergia.models import (
    AttendanceTypeData,
    BehaviourGradeData,
    GradeData,
    HomeworkEventData,
    LessonData,
    LibrusData,
    MessageData,
)

from . import LibrusConfigEntry, librus_device_info
from .ai_summary import MAX_STATE_LENGTH, LibrusWeeklySummary
from .const import (
    ATTR_SUBJECT_ID,
    AVERAGE_MODE_ARITHMETIC,
    AVERAGE_MODE_WEIGHTED,
    CONF_ANNOUNCEMENTS_ENABLED,
    CONF_AVERAGE_MODE,
    CONF_BEHAVIOUR_GRADES_ENABLED,
    CONF_DESCRIPTIVE_GRADES_ENABLED,
    CONF_HIDE_EMPTY_SUBJECTS,
    CONF_STUDENT_NUMBER,
    DEFAULT_ANNOUNCEMENTS_ENABLED,
    DEFAULT_AVERAGE_MODE,
    DEFAULT_BEHAVIOUR_GRADES_ENABLED,
    DEFAULT_DESCRIPTIVE_GRADES_ENABLED,
    DEFAULT_HIDE_EMPTY_SUBJECTS,
    DOMAIN,
    STATUS_OPTIONS,
)
from .coordinator import (
    LibrusDataUpdateCoordinator,
    days_since_last_absence,
    days_since_last_negative_note,
    good_grade_streak,
    grade_improvements,
    infer_subject_id,
    teacher_subject_ids,
)
from .coordinator import (
    calculate_average as _calculate_average,
)
from .forecast import (
    HONOURS_AVERAGE,
    SubjectForecast,
    forecast_basis,
    report_average,
    subject_forecasts,
)
from .school_day import MinuteRefresh, SchoolDay, next_end, next_start, school_days


def _latest_grade(grades: list[GradeData], *, subject_id: int | None = None) -> GradeData | None:
    candidates = [
        g
        for g in grades
        if not g.is_semester_proposition
        and not g.is_final_proposition
        and not g.is_semester
        and not g.is_final
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


def _has_parallel_group(day: date, lesson: LessonData, data: LibrusData) -> bool:
    """Whether `lesson`'s own period slot (same day + LessonNo) holds MORE
    than one lesson - parallel groups, e.g. split language subgroups,
    CONFIRMED real (see `merge_timetables`' docstring). Librus's API
    doesn't expose which group the logged-in student is actually in
    anywhere this project has found, so there's no way to pick the "right"
    one - `LibrusNextLessonSensor`/`LibrusCurrentLessonSensor`'s `_pick()`
    just takes the first lesson in `_sorted_lessons`' flattened order,
    which may or may not be the student's real group. This makes that
    ambiguity visible (via the `has_parallel_group` attribute) instead of
    silently showing a subject that might be wrong (code review).

    Kindergarten lessons have no LessonNo at all (always None) - comparing
    None to None would flag every lesson of the day, so those match on the
    same start time instead."""
    if lesson.lesson_no is None:
        return (
            sum(1 for other in data.timetable.get(day, []) if other.hour_from == lesson.hour_from)
            > 1
        )
    return sum(1 for other in data.timetable.get(day, []) if other.lesson_no == lesson.lesson_no) > 1


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
        "has_parallel_group": _has_parallel_group(day, lesson, data),
    }


# Agenda ("HomeWorks") category names that mark a graded assessment worth
# counting down to. `Sprawdzian` and `Diagnoza` are confirmed-live real
# category names for this account; "praca klasowa"/"kartkówka"/"egzamin"
# are the other standard Polish assessment names. Best-effort by design -
# a school naming a test category something else just won't be picked up
# (no false positives is the priority over catching every one).
#
# CONFIRMED real-world gap (GitHub issue #1, "Kartkowki w inne"): some
# teachers file a "kartkówka" under the generic `Inne` (Other) category
# and only name it in the free-text description instead - a category-only
# match misses these entirely. `_upcoming()` below therefore also tests
# `item.content` against this same regex as a fallback whenever the
# category itself doesn't match, rather than requiring a second, separate
# keyword list to stay in sync with this one.
#
# "quiz" added on user request (same issue thread) - another word some
# teachers use for a short/informal test, same as "kartkówka".
_EXAM_CATEGORY_RE = re.compile(
    r"sprawdzian|praca\s+klasowa|kartków|egzamin|diagnoz|quiz", re.IGNORECASE
)


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


def _subject_teachers(data: LibrusData) -> dict[str, list[str]]:
    """subject name -> sorted list of distinct teacher names, derived from
    every current+next-week timetable lesson - the closest thing to a
    "who teaches what" directory this API exposes. A subject taught by more
    than one teacher (parallel groups, e.g. split language classes) lists
    all of them. Previously the homeroom teacher (Class sensor) was the
    only teacher surfaced anywhere - subject teachers never were."""
    seen: dict[str, set[str]] = {}
    for lessons in data.timetable.values():
        for lesson in lessons:
            if lesson.subject_id is None or lesson.teacher_id is None:
                continue
            teacher_name = data.teachers.get(lesson.teacher_id)
            if not teacher_name:
                continue
            subject_name = data.subjects.get(lesson.subject_id, f"Lekcja {lesson.subject_id}")
            seen.setdefault(subject_name, set()).add(teacher_name)
    return {subject: sorted(names) for subject, names in sorted(seen.items())}


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
            LibrusGradeForecastSensor(coordinator, entry),
            LibrusAttendanceSensor(coordinator, entry),
            LibrusUnexcusedAbsencesSensor(coordinator, entry),
            LibrusSubjectAttendanceSensor(coordinator, entry),
            LibrusNextLessonSensor(coordinator, entry),
            LibrusCurrentLessonSensor(coordinator, entry),
            LibrusSchoolStartSensor(coordinator, entry),
            LibrusSchoolEndSensor(coordinator, entry),
            LibrusNextExamSensor(coordinator, entry),
            LibrusLuckyNumberSensor(coordinator, entry),
            LibrusUnreadAnnouncementsSensor(coordinator, entry),
            LibrusBehaviourNoticesSensor(coordinator, entry),
            LibrusUnreadMessagesSensor(coordinator, entry),
            LibrusSchoolSensor(coordinator, entry),
            LibrusClassSensor(coordinator, entry),
            LibrusHomeworkAssignmentsSensor(coordinator, entry),
            LibrusBehaviourGradeSensor(coordinator, entry),
            LibrusDescriptiveGradesSensor(coordinator, entry),
            LibrusAttendanceStreakSensor(coordinator, entry),
            LibrusBehaviourStreakSensor(coordinator, entry),
            LibrusGoodGradeStreakSensor(coordinator, entry),
            LibrusRankSensor(coordinator, entry),
            LibrusStatusSensor(coordinator, entry),
            LibrusLastUpdateSensor(coordinator, entry),
        ]
    )

    # The weekly AI summary exists only while an ai_task entity is picked in
    # the options.
    if coordinator.weekly_summary is not None:
        async_add_entities([LibrusWeeklySummarySensor(coordinator.weekly_summary, entry)])
    elif entity_id := er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_weekly_summary"
    ):
        er.async_get(hass).async_remove(entity_id)

    # Subjects are only known from live account data - discover new ones as
    # the coordinator sees them and add an average sensor per subject.
    known_subject_ids: set[int] = set()
    hide_empty = bool(entry.options.get(CONF_HIDE_EMPTY_SUBJECTS, DEFAULT_HIDE_EMPTY_SUBJECTS))

    def _add_new_subjects() -> None:
        data = coordinator.data
        if data is None:
            return
        graded_ids = {g.subject_id for g in data.grades if g.subject_id is not None}
        # Fall back to subject ids seen on grades even if the (unverified)
        # Subjects lookup hasn't resolved a name for it yet. With "hide
        # subjects without grades" on, only subjects that have a grade.
        seen_ids = graded_ids if hide_empty else set(data.subjects) | graded_ids
        new_ids = seen_ids - known_subject_ids
        if not new_ids:
            return
        known_subject_ids.update(new_ids)
        async_add_entities(
            LibrusSubjectAverageSensor(coordinator, entry, subject_id) for subject_id in new_ids
        )

    if hide_empty and coordinator.data is not None:
        # Turning the option on removes the existing sensors of subjects
        # that still have no grade (the options change reloads the entry).
        graded = {str(g.subject_id) for g in coordinator.data.grades if g.subject_id is not None}
        registry = er.async_get(hass)
        prefix, suffix = f"{entry.entry_id}_subject_", "_average"
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
            uid = reg_entry.unique_id
            if uid.startswith(prefix) and uid.endswith(suffix):
                if uid[len(prefix) : -len(suffix)] not in graded:
                    registry.async_remove(reg_entry.entity_id)

    _add_new_subjects()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_subjects))


class LibrusSensorBase(CoordinatorEntity[LibrusDataUpdateCoordinator], SensorEntity):
    """Common bits for every Librus Synergia sensor.

    Lists and per-item breakdowns in attributes are kept out of the recorder
    (`_unrecorded_attributes` on each sensor): they change with every new
    record, and storing a copy each time grows the database for no use -
    history graphs only need the state. Templates and cards still read them
    from the live state."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry, key: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = librus_device_info(entry)
        self._entry = entry

    @property
    def _weighted(self) -> bool:
        """Whether average sensors report the weighted average as their
        state (the default) or the plain arithmetic one - the options
        flow's average mode."""
        mode = self._entry.options.get(CONF_AVERAGE_MODE, DEFAULT_AVERAGE_MODE)
        return mode != AVERAGE_MODE_ARITHMETIC


class LibrusOverallAverageSensor(LibrusSensorBase):
    """Average across every subject - weighted unless the options flow's
    average mode says arithmetic."""

    _attr_translation_key = "overall_average"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "overall_average")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return _calculate_average(
            self.coordinator.data.grades,
            self.coordinator.data.grade_categories,
            weighted=self._weighted,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        grades = self.coordinator.data.grades
        cats = self.coordinator.data.grade_categories
        weighted = self._weighted
        return {
            # Both figures regardless of which one the state shows; the
            # per-semester ones follow the selected mode, like the state.
            "average_mode": AVERAGE_MODE_WEIGHTED if weighted else AVERAGE_MODE_ARITHMETIC,
            "average_weighted": _calculate_average(grades, cats),
            "average_arithmetic": _calculate_average(grades, cats, weighted=False),
            "average_semester_1": _calculate_average(grades, cats, semester=1, weighted=weighted),
            "average_semester_2": _calculate_average(grades, cats, semester=2, weighted=weighted),
        }


class LibrusSubjectAverageSensor(LibrusSensorBase):
    """Average for a single subject (same mode as the overall one),
    discovered dynamically."""

    _attr_translation_key = "subject_average"
    _unrecorded_attributes = frozenset({"grades", "latest_grade_comments"})
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
            weighted=self._weighted,
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
            and not g.is_semester
            and not g.is_final
        ]
        count = len(subject_grades)
        categories = self.coordinator.data.grade_categories
        improves, improved = grade_improvements(grades)
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
                "teacher": (
                    self.coordinator.data.teachers.get(g.teacher_id)
                    if g.teacher_id is not None
                    else None
                ),
                # Corrections: `improves` is the earlier grade's value on a
                # correction, `improved` marks the earlier grade itself.
                "improves": improves.get(g.id),
                "improved": g.id in improved,
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
            "average_weighted": _calculate_average(
                grades, categories, subject_id=self._subject_id
            ),
            "average_arithmetic": _calculate_average(
                grades, categories, subject_id=self._subject_id, weighted=False
            ),
            "average_semester_1": _calculate_average(
                grades,
                categories,
                subject_id=self._subject_id,
                semester=1,
                weighted=self._weighted,
            ),
            "average_semester_2": _calculate_average(
                grades,
                categories,
                subject_id=self._subject_id,
                semester=2,
                weighted=self._weighted,
            ),
            **self._forecast_attrs(),
        }

    def _forecast_attrs(self) -> dict[str, Any]:
        forecast = next(
            (
                f
                for f in subject_forecasts(
                    self.coordinator.data,
                    dt_util.now().date(),
                    self.coordinator.grade_thresholds,
                    weighted=self._weighted,
                )
                if f.subject_id == self._subject_id
            ),
            None,
        )
        if forecast is None:
            return {"predicted_grade": None}
        return {
            # The average and weight total the forecast is computed on
            # (this semester, or the whole year in semester 2).
            "forecast_average": forecast.average,
            "forecast_weight": forecast.weight_total,
            "predicted_grade": forecast.predicted,
            "next_grade_at": forecast.next_grade_at,
            "sixes_to_next_grade": forecast.sixes_to_next,
            "ones_to_drop_grade": forecast.ones_to_drop,
            "forecast_declining": forecast.declining,
        }


class LibrusGradeForecastSensor(LibrusSensorBase):
    """The report card the averages point to: state = the mean of every
    subject's forecast grade, attributes = the forecast per subject (worst
    first), subjects at risk of a 1 and subjects whose forecast fell by a
    grade in the last two weeks. See forecast.py."""

    _attr_translation_key = "grade_forecast"
    _unrecorded_attributes = frozenset({"subjects"})
    _attr_icon = "mdi:crystal-ball"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "grade_forecast")

    def _forecasts(self) -> list[SubjectForecast]:
        return subject_forecasts(
            self.coordinator.data,
            dt_util.now().date(),
            self.coordinator.grade_thresholds,
            weighted=self._weighted,
        )

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return report_average(self._forecasts())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if data is None:
            return None
        forecasts = self._forecasts()
        average = report_average(forecasts)
        basis, _semester = forecast_basis(data, dt_util.now().date())
        return {
            "basis": basis,
            "thresholds": list(self.coordinator.grade_thresholds),
            # Only the average part of "świadectwo z wyróżnieniem" - the
            # behaviour grade must be at least very good as well.
            "honours_average": average is not None and average >= HONOURS_AVERAGE,
            "at_risk": [f.subject for f in forecasts if f.at_risk],
            "declining": [f.subject for f in forecasts if f.declining],
            "subjects": [f.as_dict() for f in forecasts],
        }


def _attendance_type(data: LibrusData, type_id: int | str | None) -> AttendanceTypeData | None:
    # type_id can now be a str (see AttendanceData.type_id) - `.get()` on
    # the int-keyed `attendance_types` dict just misses (None) for one,
    # same as any other genuinely-unknown type id.
    return data.attendance_types.get(type_id) if type_id is not None else None


# Whether a non-presence type is an EXCUSED absence lives on the model now
# (`AttendanceTypeData.is_excused_absence`) - Librus has no separate API
# flag for it, so it's a best-effort match on "uspr." in the type name.
# Found live: a parent excused a real absence and it kept showing
# identically to an unexcused one in every summary/tile view, with no way
# to tell "already resolved" from "still needs attention".


def _absence_split(data: LibrusData) -> tuple[int, int]:
    """(excused, unexcused) real-absence record counts - non-presence
    types only, split on `AttendanceTypeData.is_excused_absence`. Shared by
    the Attendance sensor's attributes and the dedicated Unexcused absences
    sensor."""
    excused = unexcused = 0
    for attendance in data.attendances:
        attendance_type = _attendance_type(data, attendance.type_id)
        if attendance_type is None or attendance_type.is_presence_kind:
            continue
        if attendance_type.is_excused_absence:
            excused += 1
        else:
            unexcused += 1
    return excused, unexcused


# "Spóźnienie" (late) is a PRESENCE-kind type (IsPresenceKind: true, same
# flag value as plain "Obecność") - Librus has no separate API flag telling
# late apart from ordinary presence either, so (same best-effort text-match
# approach as `AttendanceTypeData.is_excused_absence`) "późn" (rdzeń for
# "spóźnienie") in the type's own name is the only signal. Needed for
# by_weekday below -
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
    return "warn" if attendance_type.is_excused_absence else "bad"


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
    _unrecorded_attributes = frozenset(
        {"breakdown", "presence_by_type", "by_date", "by_semester", "by_weekday", "by_subject"}
    )
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
        # There's also a dedicated `sensor.*_unexcused_absences` for the
        # unexcused figure alone.
        excused_count, unexcused_count = _absence_split(data)

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
                bucket_key = "excused" if t.is_excused_absence else "unexcused"
            try:
                weekday = date.fromisoformat(a.date).isoweekday()
            except ValueError:
                continue
            day_bucket = by_weekday.setdefault(
                str(weekday), {"excused": 0, "unexcused": 0, "late": 0}
            )
            day_bucket[bucket_key] += 1

        # Same excused/unexcused/late split again, but grouped by SUBJECT
        # NAME instead of weekday - "which subject is missed most often"
        # (user-requested feature). Attendances carries no Subject field of
        # its own; resolved via lesson_id -> Lessons' subject_id -> Subjects'
        # name (CONFIRMED live 2026-09-23 that a real record's lesson_id
        # correlates against Lessons). A record whose lesson_id doesn't
        # resolve (Lessons only covers the current timetable structure, not
        # necessarily every historical lesson) is skipped rather than
        # guessed at - same "don't fabricate a bucket" discipline as
        # everywhere else in this sensor.
        by_subject: dict[str, dict[str, int]] = {}
        for a in data.attendances:
            t = _attendance_type(data, a.type_id)
            if t is None:
                continue
            if t.is_presence_kind:
                if not _is_late_type_name(t.name):
                    continue
                bucket_key = "late"
            else:
                bucket_key = "excused" if t.is_excused_absence else "unexcused"
            if a.lesson_id is None:
                continue
            subject_id = data.lesson_subjects.get(a.lesson_id)
            if subject_id is None:
                continue
            subject_name = data.subjects.get(subject_id)
            if not subject_name:
                continue
            subject_bucket = by_subject.setdefault(
                subject_name, {"excused": 0, "unexcused": 0, "late": 0}
            )
            subject_bucket[bucket_key] += 1

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
            "by_subject": by_subject,
        }


class LibrusUnexcusedAbsencesSensor(LibrusSensorBase):
    """Just the unexcused real-absence count, as its own entity - the one
    number a parent actually needs to act on (the Attendance sensor blends
    excused and unexcused into its state). Pairs with the
    `librus_synergia_new_absence` event. `recent_dates` in attributes
    lists the days still needing a justification."""

    _attr_translation_key = "unexcused_absences"
    _unrecorded_attributes = frozenset({"recent_dates"})
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:account-alert-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "unexcused_absences")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return _absence_split(self.coordinator.data)[1]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        data = self.coordinator.data
        excused, _unexcused = _absence_split(data)
        recent_dates = sorted(
            {
                a.date
                for a in data.attendances
                if a.date
                and (t := _attendance_type(data, a.type_id)) is not None
                and not t.is_presence_kind
                and not t.is_excused_absence
            },
            reverse=True,
        )[:10]
        return {"excused_count": excused, "recent_dates": recent_dates}


# Below this share of attended lessons a student can be left unclassified
# ("nieklasyfikowany") in a subject - absences over half of the lessons.
_SUBJECT_ATTENDANCE_RISK_PERCENTAGE = 50.0

# A subject needs at least this many attendance records to count toward the
# state/`subject`/`at_risk`. CONFIRMED live (2026-10-03): some teachers
# don't take attendance in Librus at all - a subject taught twice a week
# had 2 records after a month, both absences, which made it "0%" and
# pinned the sensor there. Still listed in `subjects` either way.
_SUBJECT_ATTENDANCE_MIN_RECORDS = 5


def _subject_attendance(data: LibrusData) -> dict[str, dict[str, Any]]:
    """subject name -> {total, present, absent, percentage}, lowest
    percentage first. Same lesson_id -> Lessons -> Subjects resolution as
    the Attendance sensor's `by_subject` (unresolvable records skipped), but
    counting EVERY record, presence marks included, so there's a
    denominator. Excused and unexcused absences both count as absent."""
    by_subject: dict[str, dict[str, Any]] = {}
    for a in data.attendances:
        t = _attendance_type(data, a.type_id)
        if t is None or a.lesson_id is None:
            continue
        subject_id = data.lesson_subjects.get(a.lesson_id)
        subject_name = data.subjects.get(subject_id) if subject_id is not None else None
        if not subject_name:
            continue
        bucket = by_subject.setdefault(subject_name, {"total": 0, "present": 0})
        bucket["total"] += 1
        if t.is_presence_kind:
            bucket["present"] += 1
    for bucket in by_subject.values():
        bucket["absent"] = bucket["total"] - bucket["present"]
        bucket["percentage"] = round(100 * bucket["present"] / bucket["total"], 1)
    return dict(sorted(by_subject.items(), key=lambda kv: (kv[1]["percentage"], kv[0])))


class LibrusSubjectAttendanceSensor(LibrusSensorBase):
    """Attendance percentage of the subject with the LOWEST one - the
    figure that matters for the 50% "nieklasyfikowany" rule. `subjects` in
    attributes has every subject's own percentage, `at_risk` the ones
    already under 50%. Only subjects with at least
    `_SUBJECT_ATTENDANCE_MIN_RECORDS` records count for the state,
    `subject` and `at_risk`."""

    _attr_translation_key = "subject_attendance"
    _unrecorded_attributes = frozenset({"subjects"})
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:account-check-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "subject_attendance")

    @staticmethod
    def _counted(subjects: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {
            name: bucket
            for name, bucket in subjects.items()
            if bucket["total"] >= _SUBJECT_ATTENDANCE_MIN_RECORDS
        }

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        counted = self._counted(_subject_attendance(self.coordinator.data))
        return next(iter(counted.values()))["percentage"] if counted else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        subjects = _subject_attendance(self.coordinator.data)
        counted = self._counted(subjects)
        return {
            "subject": next(iter(counted), None),
            "subjects": subjects,
            "at_risk": [
                name
                for name, bucket in counted.items()
                if bucket["percentage"] < _SUBJECT_ATTENDANCE_RISK_PERCENTAGE
            ],
            "min_records": _SUBJECT_ATTENDANCE_MIN_RECORDS,
        }


# ----------------------------------------------------------------------
# Gamification - "passy" (streaks) and a cosmetic rank derived from data
# already fetched every cycle. Deliberately NOT an invented points/scoring
# system (see coordinator.py's own note on this) - every number here is a
# plain, honest count or day-tally a student/parent can verify by hand.
# ----------------------------------------------------------------------


class LibrusAttendanceStreakSensor(LibrusSensorBase):
    """Days since the last real absence - "passa obecności". Falls back to
    days since the school year started for a student with a perfect
    record so far, rather than showing `unknown`."""

    _attr_translation_key = "attendance_streak"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.DAYS
    _attr_icon = "mdi:fire"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "attendance_streak")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        data = self.coordinator.data
        return days_since_last_absence(
            data.attendances, data.attendance_types, data.school_class, dt_util.now().date()
        )


class LibrusBehaviourStreakSensor(LibrusSensorBase):
    """Days since the last negative behaviour note - "passa dobrego
    zachowania". Same start-of-school-year fallback as the attendance
    streak above."""

    _attr_translation_key = "behaviour_streak"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.DAYS
    _attr_icon = "mdi:fire"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "behaviour_streak")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        data = self.coordinator.data
        return days_since_last_negative_note(data.notes, data.school_class, dt_util.now().date())


class LibrusGoodGradeStreakSensor(LibrusSensorBase):
    """Consecutive most-recent numeric grades of 4 ("dobry") or better -
    "passa dobrych ocen". A non-numeric mark (bz/np/...) doesn't break the
    streak, only an actual low grade does - see
    `coordinator.good_grade_streak`'s own docstring for why."""

    _attr_translation_key = "good_grade_streak"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:fire"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "good_grade_streak")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return good_grade_streak(self.coordinator.data.grades)


# Tiers for LibrusRankSensor, ordered highest-first so the first threshold
# an average clears wins. Purely cosmetic and entirely this integration's
# own invention (Librus has no rank/tier concept) - a bit of extra polish
# on top of the Overall average sensor's own raw number. `SensorDeviceClass
# .ENUM` + these internal (untranslated) keys, with the actual display text
# coming from `entity.sensor.rank.state.<key>` in strings.json/en/pl, is
# the idiomatic HA way to get a properly localized state value here.
_RANK_TIERS: list[tuple[float, str, str]] = [
    (5.0, "diamond", "mdi:diamond-stone"),
    (4.0, "gold", "mdi:trophy"),
    (3.0, "silver", "mdi:trophy-outline"),
    (0.0, "bronze", "mdi:medal-outline"),
]


def _rank_for_average(average: float | None) -> tuple[str, str] | None:
    if average is None:
        return None
    for threshold, key, icon in _RANK_TIERS:
        if average >= threshold:
            return key, icon
    return None  # unreachable - the last tier's threshold is 0.0


class LibrusRankSensor(LibrusSensorBase):
    """A cosmetic Bronze/Silver/Gold/Diamond tier derived from the Overall
    average sensor's own state - turns a raw number into
    something a bit more game-like on a dashboard. No extra API calls."""

    _attr_translation_key = "rank"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [key for _, key, _ in _RANK_TIERS]

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "rank")

    def _average(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return _calculate_average(
            self.coordinator.data.grades,
            self.coordinator.data.grade_categories,
            weighted=self._weighted,
        )

    @property
    def icon(self) -> str | None:
        tier = _rank_for_average(self._average())
        return tier[1] if tier else "mdi:trophy-outline"

    @property
    def native_value(self) -> str | None:
        tier = _rank_for_average(self._average())
        return tier[0] if tier else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        average = self._average()
        if average is None:
            return None
        # How far above the CURRENT tier's own threshold, and how much
        # more average is needed to reach the next one up - `None` once
        # already at Diamond, the top tier.
        next_threshold = next((t for t, _, _ in reversed(_RANK_TIERS) if t > average), None)
        return {
            "average": average,
            "points_to_next_tier": round(next_threshold - average, 2)
            if next_threshold is not None
            else None,
        }


def _student_number(
    coordinator: LibrusDataUpdateCoordinator,
) -> tuple[int | None, str | None]:
    """The class register number and where it came from: the number typed
    in Configure wins (`"options"`), otherwise the one Librus shows on its
    informacja page (`"librus"`)."""
    entry = coordinator.config_entry
    raw = entry.options.get(CONF_STUDENT_NUMBER) if entry else None
    if raw is not None:
        return int(raw), "options"
    if coordinator.student_number_from_librus is not None:
        return coordinator.student_number_from_librus, "librus"
    return None, None


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
        student_number, source = _student_number(self.coordinator)
        is_yours = (
            student_number == self.coordinator.data.lucky_number.number
            if student_number is not None
            else None
        )
        return {
            "day": day,
            "is_today": day == dt_util.now().date().isoformat() if day else None,
            "student_number": student_number,
            "student_number_source": source,
            "is_yours": is_yours,
        }


class LibrusUnreadAnnouncementsSensor(LibrusSensorBase):
    """Count of school notices ("ogłoszenia") not yet marked read, with a
    recent-items attribute (subject/content preview/dates) matching the
    Behaviour notices and Unread messages sensors' pattern."""

    _attr_translation_key = "unread_announcements"
    _unrecorded_attributes = frozenset({"recent"})
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:bullhorn"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "unread_announcements")

    @property
    def available(self) -> bool:
        # `unavailable` while turned off in the options flow, rather than a
        # real-looking "0" - same precedent as the Unread messages sensor's
        # own `messages_available` gate.
        if not super().available:
            return False
        entry = self.coordinator.config_entry
        return entry is None or bool(
            entry.options.get(CONF_ANNOUNCEMENTS_ENABLED, DEFAULT_ANNOUNCEMENTS_ENABLED)
        )

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
    _unrecorded_attributes = frozenset({"recent"})
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
    _unrecorded_attributes = frozenset({"recent"})
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
        data = self.coordinator.data
        teachers = data.teachers
        by_teacher = teacher_subject_ids(data.timetable)
        recent = sorted(
            (a for a in data.homework_assignments if a.due_date),
            key=lambda a: a.due_date,
        )[:10]
        return {
            "recent": [
                {
                    "id": a.id,
                    "subject": (
                        data.subjects.get(subject_id)
                        if (subject_id := infer_subject_id(a.teacher_id, by_teacher)) is not None
                        else None
                    ),
                    "topic": a.topic,
                    # Long instructions (projects, lapbooks) are common -
                    # 200 chars cut real ones mid-sentence.
                    "text": a.text[:1000],
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
    the most recent grade in short form - "bdb" on the classic scale, or the
    points - with the full name ("bardzo dobre") in the `name` attribute.
    Found live (2026-10-05): a monthly "bdb" left this sensor blank, since
    the classic-scale grade only lives in `BehaviourGrade.Id`
    (`BehaviourGradeData.display`/`name`, librus-synergia 0.3.1)."""

    _attr_translation_key = "behaviour_grade"
    _unrecorded_attributes = frozenset({"recent"})
    _attr_icon = "mdi:medal-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "behaviour_grade")

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        entry = self.coordinator.config_entry
        return entry is None or bool(
            entry.options.get(CONF_BEHAVIOUR_GRADES_ENABLED, DEFAULT_BEHAVIOUR_GRADES_ENABLED)
        )

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
        return (latest.display or None) if latest else None

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
        latest = recent[0] if recent else None
        return {
            "name": latest.name if latest else None,
            "comment": latest.comments[0].strip() if latest and latest.comments else None,
            "recent": [
                {
                    "grade": g.display,
                    "name": g.name,
                    "short_name": g.short_name,
                    "value": g.value,
                    "category": categories.get(g.category_id) if g.category_id else None,
                    "date": g.add_date,
                    "text": g.text[:200],
                    "comments": [c.strip() for c in g.comments],
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
    _unrecorded_attributes = frozenset({"recent"})
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:text-box-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "descriptive_grades")

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        entry = self.coordinator.config_entry
        return entry is None or bool(
            entry.options.get(CONF_DESCRIPTIVE_GRADES_ENABLED, DEFAULT_DESCRIPTIVE_GRADES_ENABLED)
        )

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
    _unrecorded_attributes = frozenset(
        {"recent", "substitutions_recent", "alerts_recent", "justifications_recent"}
    )
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
    _unrecorded_attributes = frozenset({"bell_schedule", "subject_teachers"})
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
            # Subject -> teacher(s) directory, same timetable-derived
            # approach as bell_schedule above. Previously the ONLY teacher
            # surfaced anywhere was the homeroom teacher (Class sensor) -
            # subject teachers never were, despite the data already being
            # fetched every cycle for the Next/Current lesson sensors.
            "subject_teachers": _subject_teachers(self.coordinator.data),
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
            # Class register number ("nr w dzienniku") - same value and
            # precedence as the Lucky number sensor's attribute.
            "student_number": _student_number(self.coordinator)[0],
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
    so it can see through to Monday from a Friday evening but not further.

    `has_parallel_group` (code review): when the picked period slot holds
    more than one lesson (parallel groups, e.g. split language subgroups -
    CONFIRMED real, see `merge_timetables`'s docstring), `_pick()` below
    just takes the first one in `_sorted_lessons`' flattened order - Librus
    doesn't expose which group the student is actually in anywhere this
    project has found, so the shown subject might be the wrong group's.
    This attribute makes that ambiguity visible instead of silently
    guessing."""

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
    sensor - including `has_parallel_group` (see that class's docstring)."""

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


class _LibrusSchoolTimeSensor(MinuteRefresh, LibrusSensorBase):
    """Timestamp of the next first-lesson start / last-lesson end - a
    sensor an automation can trigger on with an offset ("45 minutes
    before school starts"). Re-checked every minute so it moves on to the
    next school day right after the moment passes."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def _pick(self) -> SchoolDay | None:
        raise NotImplementedError

    def _moment(self, school_day: SchoolDay) -> datetime:
        raise NotImplementedError

    def _lesson(self, school_day: SchoolDay) -> LessonData:
        raise NotImplementedError

    @property
    def native_value(self) -> datetime | None:
        picked = self._pick()
        return self._moment(picked) if picked else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        picked = self._pick()
        if picked is None or self.coordinator.data is None:
            return None
        lesson = self._lesson(picked)
        return {
            "date": picked.day.isoformat(),
            "is_today": picked.day == dt_util.now().date(),
            "lesson_no": lesson.lesson_no,
            "subject": _lesson_subject(lesson, self.coordinator.data),
        }


class LibrusSchoolStartSensor(_LibrusSchoolTimeSensor):
    """Start of the first lesson of today (until it starts), then of the
    next school day. For alarm clocks."""

    _attr_translation_key = "school_start"
    _attr_icon = "mdi:alarm"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "school_start")

    def _pick(self) -> SchoolDay | None:
        return next_start(school_days(self.coordinator.data), dt_util.now())

    def _moment(self, school_day: SchoolDay) -> datetime:
        return school_day.first_start

    def _lesson(self, school_day: SchoolDay) -> LessonData:
        return school_day.first_lesson


class LibrusSchoolEndSensor(_LibrusSchoolTimeSensor):
    """End of the last lesson of today (until it ends), then of the next
    school day. For pick-up reminders."""

    _attr_translation_key = "school_end"
    _attr_icon = "mdi:home-import-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "school_end")

    def _pick(self) -> SchoolDay | None:
        return next_end(school_days(self.coordinator.data), dt_util.now())

    def _moment(self, school_day: SchoolDay) -> datetime:
        return school_day.last_end

    def _lesson(self, school_day: SchoolDay) -> LessonData:
        return school_day.last_lesson


class LibrusNextExamSensor(LibrusSensorBase):
    """Date of the next graded assessment ("sprawdzian" and friends) from
    the Agenda feed. State is a date (`device_class: date`); attributes
    carry `days_until`, the subject, the category name, the description and
    an `upcoming` list. `unknown` when nothing assessment-like is on the
    agenda. Exam detection is by the Agenda category name, falling back to
    the free-text description when the category itself doesn't match (see
    `_EXAM_CATEGORY_RE`) - deliberately conservative."""

    _attr_translation_key = "next_exam"
    _unrecorded_attributes = frozenset({"upcoming"})
    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:file-document-alert-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "next_exam")

    def _upcoming(self) -> list[tuple[date, HomeworkEventData]]:
        if self.coordinator.data is None:
            return []
        data = self.coordinator.data
        today = dt_util.now().date()
        out: list[tuple[date, HomeworkEventData]] = []
        for item in data.homeworks:
            if not item.date:
                continue
            category = (
                data.homework_categories.get(item.category_id)
                if item.category_id is not None
                else None
            )
            category_match = category is not None and _EXAM_CATEGORY_RE.search(category)
            content_match = item.content and _EXAM_CATEGORY_RE.search(item.content)
            if not category_match and not content_match:
                continue
            try:
                day = date.fromisoformat(item.date[:10])
            except ValueError:
                continue
            if day < today:
                continue
            out.append((day, item))
        out.sort(key=lambda pair: pair[0])
        return out

    @property
    def native_value(self) -> date | None:
        upcoming = self._upcoming()
        return upcoming[0][0] if upcoming else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        upcoming = self._upcoming()
        if not upcoming:
            return None
        data = self.coordinator.data
        today = dt_util.now().date()
        day, item = upcoming[0]

        def _subject(it: HomeworkEventData) -> str | None:
            return data.subjects.get(it.subject_id) if it.subject_id is not None else None

        def _category(it: HomeworkEventData) -> str | None:
            return (
                data.homework_categories.get(it.category_id)
                if it.category_id is not None
                else None
            )

        return {
            "days_until": (day - today).days,
            "subject": _subject(item),
            "category": _category(item),
            "content": item.content,
            "upcoming": [
                {
                    "date": d.isoformat(),
                    "subject": _subject(it),
                    "category": _category(it),
                    "content": it.content[:200],
                }
                for d, it in upcoming[:10]
            ],
        }


class LibrusStatusSensor(LibrusSensorBase):
    """Connection health: `ok`, `degraded` (some section failed this cycle
    and its last good copy is shown), `stale` (Librus isn't answering, the
    last data is shown) or `error` (no usable data - the other entities are
    unavailable). Stays available itself so the outage can be seen."""

    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:lan-check"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "status")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        return self.coordinator.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coordinator = self.coordinator

        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "data_source": coordinator.data_source,
            "last_success": iso(coordinator.last_success_at),
            "last_attempt": iso(coordinator.last_attempt_at),
            "last_error": coordinator.last_error,
            "failures": coordinator.failures,
            "next_attempt": iso(coordinator.next_attempt_at),
            "fallback_sections": sorted(coordinator.fallback_sections),
            "degraded_endpoints": {
                label: since.isoformat() for label, since in coordinator.degraded_endpoints.items()
            },
        }


class LibrusLastUpdateSensor(LibrusSensorBase):
    """When Librus last answered a full refresh - survives restarts, so it
    also tells how old the data shown during an outage is."""

    _attr_translation_key = "last_update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cloud-clock-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_update")

    @property
    def available(self) -> bool:
        return self.coordinator.last_success_at is not None

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_success_at


class LibrusWeeklySummarySensor(SensorEntity):
    """The weekly AI summary: headline as the state, the rest as attributes."""

    _attr_has_entity_name = True
    _attr_translation_key = "weekly_summary"
    _attr_icon = "mdi:creation"
    _attr_should_poll = False
    # Long text is only useful live on a card, never worth keeping in history.
    _unrecorded_attributes = frozenset({"sections", "summary", "advice", "warning", "error"})

    def __init__(self, summary: LibrusWeeklySummary, entry: LibrusConfigEntry) -> None:
        self._summary = summary
        self._attr_unique_id = f"{entry.entry_id}_weekly_summary"
        self._attr_device_info = librus_device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Follow the summary's updates."""
        self.async_on_remove(self._summary.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> str | None:
        result = self._summary.result or {}
        headline = result.get("headline") or result.get("summary")
        return headline[:MAX_STATE_LENGTH] if headline else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._summary.result or {}
        return {
            "status": result.get("status"),
            # {"grades" | "attendance" | "behaviour" | "next_week" |
            # "school_news": {"status", "text"}}, in display order; a section
            # with no text is left out.
            "sections": result.get("sections") or {},
            # Only on a plain-text answer (no structured output support).
            "summary": result.get("summary"),
            "advice": result.get("advice") or [],
            "warning": result.get("warning"),
            "week_from": result.get("week_from"),
            "week_to": result.get("week_to"),
            "audience": result.get("audience") or self._summary.audience,
            "generated_at": result.get("generated_at"),
            "next_run": self._summary.next_run.isoformat(),
            "ai_task_entity": self._summary.ai_task_entity,
            "generating": self._summary.running,
            # The "Automatic weekly summary" switch is off: no scheduled runs.
            "paused": not self._summary.enabled,
            "error": self._summary.last_error,
        }
