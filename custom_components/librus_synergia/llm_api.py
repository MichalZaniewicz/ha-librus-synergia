"""Librus Synergia as a tool set for Home Assistant's conversation agents.

Registers one LLM API ("Librus Synergia") that a conversation agent (Assist
with Gemini, OpenAI, Ollama...) can turn on next to "Assist" in its
settings. The model then answers school questions - "what does he have
tomorrow?", "any grades this week?", "when is the next maths test?" - by
calling these tools instead of reading dozens of sensors.

Every tool is read-only and answers from the data the coordinators already
hold, so a question never adds a Librus request. The one exception is a
timetable date outside the cached current+next week, which fetches that
single week on demand (same path the timetable calendar uses).

Compatibility: Home Assistant's `llm` helper changed over time - tool
parameters moved from voluptuous to probatio schemas, tools gained
`ToolResult`/`ToolAnnotations`/`integration`, and `async_register_api`
only returns an unregister callback since 2025. Each difference is
detected at runtime instead of pinning one Home Assistant version.
"""

from __future__ import annotations

import importlib
import logging
from datetime import date, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util

from librus_synergia import LibrusError
from librus_synergia.models import LibrusData
from librus_synergia.parsers import plan_differences

from .ai_summary import _compact, _cut, _dated, _day
from .catch_up import catch_up
from .const import AVERAGE_MODE_ARITHMETIC, CONF_AVERAGE_MODE, DEFAULT_AVERAGE_MODE, DOMAIN
from .coordinator import (
    LibrusDataUpdateCoordinator,
    calculate_average,
    days_since_last_absence,
    days_since_last_negative_note,
    grade_improvements,
    infer_subject_id,
    merge_timetables,
    teacher_subject_ids,
)
from .exam_prep import exam_prep, is_exam, missed_lessons
from .forecast import subject_forecasts

_LOGGER = logging.getLogger(__name__)

LLM_API_ID = DOMAIN
LLM_API_NAME = "Librus Synergia"
_DATA_UNREGISTER = f"{DOMAIN}_llm_api_unregister"

MAX_DAYS = 60
MAX_GRADES = 60


# ---------------------------------------------------------------------------
# Version-tolerant building blocks
# ---------------------------------------------------------------------------


def _schema(fields: dict[str, tuple[type, str]]) -> Any:
    """An all-optional parameter schema in whatever schema library this
    Home Assistant's `llm.Tool.parameters` uses (voluptuous, or probatio
    on newer versions). Validators stay plain `str`/`int`, which both
    libraries understand and turn into OpenAPI types; ranges are clamped
    in the tools themselves."""
    schema_cls = type(llm.Tool.parameters)
    library = importlib.import_module(schema_cls.__module__.split(".")[0])
    return schema_cls(
        {
            library.Optional(name, description=description): validator
            for name, (validator, description) in fields.items()
        }
    )


def _result(data: dict[str, Any], *, error: bool = False) -> Any:
    tool_result = getattr(llm, "ToolResult", None)
    if tool_result is None:
        return data
    return tool_result(data=data, error=error)


_STUDENT_FIELD = (
    str,
    "The student's first name. Only needed when more than one student is set up; "
    "leave it out to get every student.",
)


class _LibrusTool(llm.Tool):
    """Read-only, idempotent, no side effects outside Home Assistant."""

    integration = DOMAIN

    def __init__(self) -> None:
        annotations = getattr(llm, "ToolAnnotations", None)
        if annotations is not None:
            self.annotations = annotations(
                read_only=True, destructive=False, idempotent=True, open_world=False
            )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> Any:
        args = tool_input.tool_args or {}
        students = _students(hass)
        if not students:
            return _result({"error": "No Librus Synergia data is available yet."}, error=True)
        wanted = args.get("student")
        if wanted:
            picked = [s for s in students if _name_matches(s[0], str(wanted))]
            if not picked:
                return _result(
                    {
                        "error": f"No student matching {wanted!r}.",
                        "students": [name for name, _ in students],
                    },
                    error=True,
                )
            students = picked
        today = dt_util.now().date()
        results = []
        for name, coordinator in students:
            body = await self._async_for_student(coordinator, coordinator.data, today, args)
            results.append({"student": name, **body})
        if len(results) == 1:
            return _result({"today": _dated(today), **results[0]})
        return _result({"today": _dated(today), "students": results})

    async def _async_for_student(
        self,
        coordinator: LibrusDataUpdateCoordinator,
        data: LibrusData,
        today: date,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError


def _students(hass: HomeAssistant) -> list[tuple[str, LibrusDataUpdateCoordinator]]:
    """(student name, coordinator) for every loaded entry with data."""
    found = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        coordinator = getattr(entry, "runtime_data", None)
        if coordinator is None or coordinator.data is None:
            continue
        name = coordinator.data.me.display_name or entry.title
        found.append((name, coordinator))
    return found


def _name_matches(name: str, wanted: str) -> bool:
    wanted = wanted.casefold().strip()
    return any(part.casefold().startswith(wanted) for part in name.split()) or (
        wanted in name.casefold()
    )


def _int_arg(args: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(args.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _weighted(coordinator: LibrusDataUpdateCoordinator) -> bool:
    entry = coordinator.config_entry
    mode = entry.options.get(CONF_AVERAGE_MODE, DEFAULT_AVERAGE_MODE) if entry else None
    return mode != AVERAGE_MODE_ARITHMETIC


def _subject_name(data: LibrusData, subject_id: Any) -> str | None:
    return data.subjects.get(subject_id) if subject_id is not None else None


def _teacher_name(data: LibrusData, teacher_id: Any) -> str | None:
    return data.teachers.get(teacher_id) if teacher_id is not None else None


def _matching_subject_ids(data: LibrusData, wanted: str | None) -> set[Any] | None:
    """Subject ids whose name contains `wanted` (case-insensitive); None
    when no filter was given."""
    if not wanted:
        return None
    needle = wanted.casefold().strip()
    return {sid for sid, name in data.subjects.items() if needle in str(name).casefold()}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class TimetableTool(_LibrusTool):
    name = "librus_get_timetable"
    description = (
        "Lessons for a day or a few days: lesson number, start and end time, subject, "
        "teacher, classroom, and whether a lesson is cancelled or a substitution. "
        "Also says when a day is a day off. Use it for 'what does he have tomorrow', "
        "'when does school start on Friday', 'is any lesson cancelled'."
    )
    parameters = _schema(
        {
            "date": (str, "First day, YYYY-MM-DD. Defaults to today."),
            "days": (int, "How many days from that date, 1-7. Defaults to 1."),
            "student": _STUDENT_FIELD,
        }
    )

    async def _async_for_student(self, coordinator, data, today, args):
        start = _day(str(args.get("date") or "")) or today
        days = _int_arg(args, "days", 1, 1, 7)
        timetable = dict(data.timetable)
        wanted = [start + timedelta(days=i) for i in range(days)]
        for week_start in sorted({d - timedelta(days=d.weekday()) for d in wanted}):
            if any(week_start + timedelta(days=i) in timetable for i in range(7)):
                continue
            try:
                timetable.update(merge_timetables(await coordinator.async_fetch_timetable_week(week_start)))
            except LibrusError as err:
                _LOGGER.debug("Timetable week %s not available for the LLM tool: %s", week_start, err)
        result_days = []
        for day in wanted:
            free = next(
                (
                    f.name
                    for f in data.free_days
                    if (_day(f.date_from) or date.max) <= day <= (_day(f.date_to) or date.min)
                ),
                None,
            )
            lessons = [
                _compact(
                    {
                        "no": lesson.lesson_no,
                        "from": lesson.hour_from,
                        "to": lesson.hour_to,
                        "subject": _subject_name(data, lesson.subject_id),
                        "teacher": _teacher_name(data, lesson.teacher_id),
                        "room": data.classrooms.get(lesson.classroom_id)
                        if lesson.classroom_id is not None
                        else None,
                        "cancelled": True if lesson.is_canceled else None,
                        "substitution": True if lesson.is_substitution else None,
                    }
                )
                for lesson in sorted(
                    timetable.get(day, []), key=lambda l: (l.lesson_no or 0, l.hour_from or "")
                )
            ]
            result_days.append(
                _compact(
                    {
                        "date": _dated(day),
                        "day_off": free,
                        "lessons": lessons,
                        "no_lessons": True if not lessons and not free else None,
                    }
                )
            )
        return {"days": result_days}


class GradesTool(_LibrusTool):
    name = "librus_get_grades"
    description = (
        "Grades with date, subject, value, category (e.g. sprawdzian, kartkówka), weight, "
        "teacher and comment, newest first, plus the current averages. Polish scale 1-6; "
        "'+' adds 0.5 and '-' takes 0.25; marks such as np, bz, + or - alone do not count "
        "towards the average. Also a forecast of the report-card grade per subject (from the "
        "average and the school's thresholds - the teacher decides the real grade) with how "
        "many 6s lift it and how many 1s drop it. Also grades a teacher gave as text "
        "(text_grades) and descriptive grades. Filter by subject and by how many days back."
    )
    parameters = _schema(
        {
            "subject": (str, "Part of a subject name, e.g. 'matem' or 'angiel'."),
            "days": (int, "Only grades from the last N days. Defaults to the whole school year."),
            "student": _STUDENT_FIELD,
        }
    )

    async def _async_for_student(self, coordinator, data, today, args):
        subject_ids = _matching_subject_ids(data, args.get("subject"))
        days = args.get("days")
        since = today - timedelta(days=_int_arg(args, "days", 0, 0, 400)) if days else None
        weighted = _weighted(coordinator)
        improves, improved = grade_improvements(data.grades)
        picked = [
            g
            for g in data.grades
            if (subject_ids is None or g.subject_id in subject_ids)
            and (since is None or (_day(g.add_date) or date.min) >= since)
        ]
        picked.sort(key=lambda g: g.add_date or "", reverse=True)
        grades = []
        for g in picked[:MAX_GRADES]:
            category = data.grade_categories.get(g.category_id) if g.category_id is not None else None
            grades.append(
                _compact(
                    {
                        "date": _dated(g.add_date),
                        "subject": _subject_name(data, g.subject_id),
                        "value": g.value,
                        "category": category.name if category else None,
                        "weight": category.weight if category else None,
                        "counts_to_average": False
                        if category and not category.count_to_average
                        else None,
                        "teacher": _teacher_name(data, g.teacher_id),
                        "comment": _cut("; ".join(g.comments)),
                        "semester_or_final": "proposed semester"
                        if g.is_semester_proposition
                        else "semester"
                        if g.is_semester
                        else "proposed final"
                        if g.is_final_proposition
                        else "final"
                        if g.is_final
                        else None,
                        "corrects_grade": improves.get(g.id),
                        "was_corrected": True if g.id in improved else None,
                    }
                )
            )
        subjects_for_avg = (
            subject_ids
            if subject_ids is not None
            else {g.subject_id for g in data.grades if g.subject_id is not None}
        )
        averages = {}
        for sid in sorted(subjects_for_avg, key=lambda s: str(_subject_name(data, s))):
            avg = calculate_average(data.grades, data.grade_categories, subject_id=sid, weighted=weighted,
                grading=data.grading_system)
            if avg is not None:
                averages[_subject_name(data, sid) or str(sid)] = avg
        return _compact(
            {
                "average_mode": "weighted" if weighted else "arithmetic",
                "overall_average": calculate_average(
                    data.grades, data.grade_categories, weighted=weighted,
                    grading=data.grading_system
                )
                if subject_ids is None
                else None,
                "subject_averages": averages,
                # What the averages point to on the report card, with what it
                # takes to move a grade (thresholds from the options).
                "forecast": [
                    _compact(
                        {
                            "subject": f.subject,
                            "forecast_grade": f.predicted,
                            "sixes_needed_for_next_grade": f.sixes_to_next,
                            "ones_until_grade_drops": f.ones_to_drop,
                            "at_risk_of_failing": True if f.at_risk else None,
                            "dropped_recently": True if f.declining else None,
                            "teacher_proposed": f.proposed,
                        }
                    )
                    for f in subject_forecasts(
                        data, today, coordinator.grade_thresholds, weighted=weighted
                    )
                    if subject_ids is None or f.subject_id in subject_ids
                ]
                or None,
                "grades": grades,
                "text_grades": [
                    _compact(
                        {
                            "date": _dated(t.date),
                            "subject": _subject_name(data, t.subject_id),
                            "value": _cut(t.value),
                            "category": t.category,
                            "teacher": _teacher_name(data, t.teacher_id),
                        }
                    )
                    for t in sorted(data.text_grades, key=lambda t: t.date or "", reverse=True)
                    if (subject_ids is None or t.subject_id in subject_ids)
                    and (since is None or (_day(t.date) or date.min) >= since)
                ][:20]
                or None,
                "descriptive_grades": [
                    _compact(
                        {
                            "date": _dated(d.date or d.add_date),
                            "subject": _subject_name(data, d.subject_id),
                            "value": _cut(d.value),
                            "skill": d.skill,
                            "teacher": _teacher_name(data, d.teacher_id if d.teacher_id is not None else d.teacher_lid),
                            "comment": _cut(" / ".join(d.comments)) if d.comments else None,
                        }
                    )
                    for d in sorted(
                        data.descriptive_grades,
                        key=lambda d: d.date or d.add_date or "",
                        reverse=True,
                    )
                    if subject_ids is None or d.subject_id in subject_ids
                ][:10]
                or None,
                "more_grades_not_shown": len(picked) - MAX_GRADES if len(picked) > MAX_GRADES else None,
                "no_grades": True if not picked else None,
            }
        )


class UpcomingTool(_LibrusTool):
    name = "librus_get_upcoming"
    description = (
        "What is coming up: tests, quizzes, trips and other agenda entries (terminarz) with "
        "their category, homework with its due date, school trips, how the timetable differs "
        "from the usual plan (cancelled lessons, substitutions, another room, extra or missing "
        "lessons), and days off. For each test it lists the topics to revise: the lessons "
        "taught in that subject since the previous test, marking the ones the student missed. "
        "Use it for 'any tests this week', 'what should she revise for maths', 'what homework "
        "is due', 'is Friday a day off', 'any changes in the timetable'."
    )
    parameters = _schema(
        {
            "days": (int, "How many days ahead, from today. Defaults to 14, at most 60."),
            "subject": (str, "Only entries for this subject (part of its name)."),
            "student": _STUDENT_FIELD,
        }
    )

    async def _async_for_student(self, coordinator, data, today, args):
        until = today + timedelta(days=_int_arg(args, "days", 14, 1, MAX_DAYS))
        subject_ids = _matching_subject_ids(data, args.get("subject"))

        def in_range(value: str | None) -> bool:
            day = _day(value)
            return day is not None and today <= day <= until

        by_teacher = teacher_subject_ids(data.timetable)
        revision = {prep.item.id: prep for prep in exam_prep(data, today, until=until)}
        agenda = []
        for item in sorted(data.homeworks, key=lambda h: h.date or ""):
            if not in_range(item.date) or (subject_ids is not None and item.subject_id not in subject_ids):
                continue
            prep = revision.get(item.id)
            agenda.append(
                _compact(
                    {
                        "date": _dated(item.date),
                        "time": item.time_from,
                        "subject": _subject_name(data, item.subject_id),
                        "category": data.homework_categories.get(item.category_id)
                        if item.category_id is not None
                        else None,
                        "content": _cut(item.content),
                        "is_test": True if is_exam(data, item) else None,
                        "topics_to_revise": [
                            _compact(
                                {
                                    "date": _dated(t.date),
                                    "topic": _cut(t.topic, 160),
                                    "student_was_absent": True if t.absent else None,
                                }
                            )
                            for t in prep.topics
                        ]
                        if prep and prep.topics
                        else None,
                        "revise_since_previous_test_on": _dated(prep.since)
                        if prep and prep.since
                        else None,
                    }
                )
            )
        homework = []
        for item in sorted(data.homework_assignments, key=lambda h: h.due_date or ""):
            if not in_range(item.due_date):
                continue
            sid = infer_subject_id(item.teacher_id, by_teacher)
            if subject_ids is not None and sid not in subject_ids:
                continue
            homework.append(
                _compact(
                    {
                        "due": _dated(item.due_date),
                        "subject": _subject_name(data, sid),
                        "teacher": _teacher_name(data, item.teacher_id),
                        "topic": _cut(item.topic, 120),
                        "text": _cut(item.text),
                        "attached_files": [f.filename or "file" for f in item.attachments]
                        or None,
                    }
                )
            )
        changes = [
            _compact(
                {
                    "date": _dated(day),
                    "no": lesson.lesson_no,
                    "subject": _subject_name(data, lesson.subject_id),
                    "change": "cancelled" if lesson.is_canceled else "substitution",
                    "teacher": _teacher_name(data, lesson.teacher_id)
                    if lesson.is_substitution
                    else None,
                }
            )
            for day, lessons in sorted(data.timetable.items())
            if today <= day <= until
            for lesson in lessons
            if (lesson.is_canceled or lesson.is_substitution)
            and (subject_ids is None or lesson.subject_id in subject_ids)
        ]
        # Against the standing weekly plan: also a lesson in another room,
        # an extra or a missing lesson, a weekday without lessons.
        plan = [
            _compact(
                {
                    "date": _dated(diff.date),
                    "no": diff.lesson_no,
                    "difference": diff.kind,
                    "subject": _subject_name(data, diff.subject_id),
                    "usual_subject": _subject_name(data, diff.planned_subject_id),
                    "room": data.classrooms.get(diff.classroom_id) if diff.classroom_id is not None else None,
                    "usual_room": diff.planned_classroom,
                    "day_off": diff.free_day,
                }
            )
            for diff in plan_differences(data.timetable, data.standing_timetable, data.free_days)
            if today <= diff.date <= until and diff.kind != "cancelled"
        ]
        trips = [
            _compact(
                {
                    "from": _dated(trip.date_from),
                    "to": _dated(trip.date_to) if trip.date_to != trip.date_from else None,
                    "destination": _cut(trip.destination, 160),
                    "transport": trip.transport or None,
                    "coordinator": trip.coordinator,
                }
            )
            for trip in data.school_trips
            if (_day(trip.date_from) or date.max) <= until and (_day(trip.date_to) or date.min) >= today
        ]
        free_days = [
            {"name": f.name, "from": _dated(f.date_from), "to": _dated(f.date_to)}
            for f in data.free_days
            if (_day(f.date_from) or date.max) <= until and (_day(f.date_to) or date.min) >= today
        ]
        return _compact(
            {
                "until": _dated(until),
                "agenda": agenda,
                "homework_due": homework,
                "timetable_changes": changes,
                "differences_from_usual_plan": plan or None,
                "school_trips": trips or None,
                "days_off": free_days,
                "nothing_planned": True
                if not (agenda or homework or changes or plan or trips or free_days)
                else None,
            }
        )


class AttendanceTool(_LibrusTool):
    name = "librus_get_attendance"
    description = (
        "Attendance: absences (excused and not yet excused), late arrivals, attendance "
        "percentage overall and per subject (below 50% in a subject risks not being "
        "classified), the dates of absences that still need an excuse, the "
        "justifications the parent sent with the school's decision, and what to catch "
        "up on after the latest absence (missed lessons with their topics, homework "
        "given meanwhile)."
    )
    parameters = _schema(
        {
            "days": (int, "Only the last N days. Defaults to the whole school year."),
            "student": _STUDENT_FIELD,
        }
    )

    async def _async_for_student(self, coordinator, data, today, args):
        # Imported here: sensor.py pulls in the whole entity platform.
        from .sensor import _SUBJECT_ATTENDANCE_MIN_RECORDS, _subject_attendance  # noqa: PLC0415

        since = today - timedelta(days=_int_arg(args, "days", 0, 0, 400)) if args.get("days") else None
        counts = {"present": 0, "late": 0, "absent_excused": 0, "absent_unexcused": 0}
        unexcused_dates: set[str] = set()
        recent = []
        for record in sorted(data.attendances, key=lambda a: a.date or "", reverse=True):
            kind = data.attendance_types.get(record.type_id) if record.type_id is not None else None
            day = _day(record.date)
            if kind is None or (since is not None and (day is None or day < since)):
                continue
            if kind.is_presence_kind:
                late = "późn" in kind.name.lower()
                counts["late" if late else "present"] += 1
                if not late:
                    continue
            elif kind.is_excused_absence:
                counts["absent_excused"] += 1
            else:
                counts["absent_unexcused"] += 1
                if day:
                    unexcused_dates.add(day.isoformat())
            if len(recent) < 15:
                sid = data.lesson_subjects.get(record.lesson_id) if record.lesson_id is not None else None
                recent.append(
                    _compact(
                        {
                            "date": _dated(day),
                            "lesson_no": record.lesson_no,
                            "subject": _subject_name(data, sid),
                            "type": kind.name,
                        }
                    )
                )
        total = sum(counts.values())
        per_subject = {
            name: s
            for name, s in _subject_attendance(data).items()
            if s["total"] >= _SUBJECT_ATTENDANCE_MIN_RECORDS
        }
        return _compact(
            {
                "counts": counts,
                "attendance_percentage": round(
                    100 * (counts["present"] + counts["late"]) / total, 1
                )
                if total
                else None,
                "unexcused_absence_dates": sorted(unexcused_dates),
                "recent_absences_and_lates": recent,
                "subjects_lowest_first": dict(list(per_subject.items())[:6]) if since is None else None,
                "days_without_absence": days_since_last_absence(
                    data.attendances, data.attendance_types, data.school_class, today
                ),
                "justifications_sent": [
                    _compact(
                        {
                            "sent": _dated(j.posted),
                            "from": _dated(j.date_from),
                            "to": _dated(j.date_to),
                            "status": "accepted"
                            if j.is_accepted
                            else "rejected"
                            if j.is_rejected
                            else "waiting for the school",
                            "lessons": j.justified_absences or None,
                        }
                    )
                    for j in data.justifications[:8]
                ]
                or None,
                "catch_up_after_latest_absence": _catch_up_summary(data, today),
            }
        )


def _catch_up_summary(data: LibrusData, today: date) -> dict[str, Any] | None:
    period = catch_up(data, today)
    if period is None:
        return None
    return _compact(
        {
            "absent_from": period["from"],
            "absent_to": period["to"],
            "school_days": period["days"],
            "back_on": period["back_on"],
            "missed_lessons": [
                _compact({**lesson, "topic": _cut(lesson["topic"], 160)})
                for lesson in period["lessons"][:25]
            ],
            "homework_given_meanwhile": [
                _compact(
                    {
                        "given": item["date"],
                        "due": item["due_date"],
                        "subject": item["subject"],
                        "topic": _cut(item["topic"], 120),
                    }
                )
                for item in period["homework"]
            ]
            or None,
        }
    )


class BehaviourTool(_LibrusTool):
    name = "librus_get_behaviour"
    description = (
        "Behaviour: teachers' notes (uwagi) with whether they are positive, negative or "
        "neutral, their category and text, and the formal behaviour grade (ocena z "
        "zachowania)."
    )
    parameters = _schema(
        {
            "days": (int, "Only notes from the last N days. Defaults to the whole school year."),
            "student": _STUDENT_FIELD,
        }
    )

    async def _async_for_student(self, coordinator, data, today, args):
        since = today - timedelta(days=_int_arg(args, "days", 0, 0, 400)) if args.get("days") else None
        notes = [
            _compact(
                {
                    "date": _dated(note.date),
                    "sentiment": note.sentiment,
                    "category": data.note_categories.get(note.category_id)
                    if note.category_id is not None
                    else None,
                    "teacher": _teacher_name(data, note.teacher_id),
                    "text": _cut(note.text),
                }
            )
            for note in sorted(data.notes, key=lambda n: n.date or "", reverse=True)
            if since is None or (_day(note.date) or date.min) >= since
        ][:20]
        behaviour = None
        if data.behaviour_grades:
            latest = max(data.behaviour_grades, key=lambda b: b.add_date or "")
            behaviour = _compact(
                {
                    "grade": latest.display or None,
                    "name": latest.name,
                    "date": _dated(latest.add_date),
                    "comment": _cut("; ".join(c.strip() for c in latest.comments)),
                }
            )
        return _compact(
            {
                "notes": notes,
                "no_notes": True if not notes else None,
                "behaviour_grade": behaviour,
                "days_without_negative_note": days_since_last_negative_note(
                    data.notes, data.school_class, today
                ),
            }
        )


class SchoolInfoTool(_LibrusTool):
    name = "librus_get_school_info"
    description = (
        "The student's school and class: class name, homeroom teacher, school name and "
        "address, the student's number in the class register, today's lucky number "
        "(szczęśliwy numerek: the student with that number is not asked to answer that "
        "day) and whether it is theirs, the semester and school-year end dates, and the "
        "documents the school shares with parents (forms, regulations)."
    )
    parameters = _schema({"student": _STUDENT_FIELD})

    async def _async_for_student(self, coordinator, data, today, args):
        from .sensor import _student_number  # noqa: PLC0415

        number, _source = _student_number(coordinator)
        cls = data.school_class
        school = data.school
        lucky = data.lucky_number
        return _compact(
            {
                "class": cls.display_name if cls else None,
                "homeroom_teacher": _teacher_name(data, cls.tutor_id) if cls else None,
                "school": _compact(
                    {
                        "name": school.name,
                        "town": school.town,
                        "street": school.street,
                    }
                )
                if school
                else None,
                "student_number": number,
                "lucky_number": _compact(
                    {
                        "number": lucky.number,
                        "for_day": _dated(lucky.day),
                        "is_the_students_number": (lucky.number == number)
                        if number is not None
                        else None,
                    }
                )
                if lucky
                else None,
                "first_semester_ends": _dated(cls.end_first_semester) if cls else None,
                "school_year_ends": _dated(cls.end_school_year) if cls else None,
                "school_documents": [
                    _compact({"name": _cut(f.name, 160), "added": _dated(f.added)})
                    for f in data.school_files[:15]
                ]
                or None,
            }
        )


class MessagesTool(_LibrusTool):
    name = "librus_get_messages"
    description = (
        "Recent private messages from teachers and the school (sender, subject line, the "
        "start of the text, whether it is unread), messages the parent sent (to whom) and "
        "school announcements. Listing does not mark anything as read."
    )
    parameters = _schema(
        {
            "days": (int, "Only the last N days. Defaults to 14."),
            "student": _STUDENT_FIELD,
        }
    )

    async def _async_for_student(self, coordinator, data, today, args):
        since = today - timedelta(days=_int_arg(args, "days", 14, 1, MAX_DAYS))
        messages = [
            _compact(
                {
                    "date": _dated(m.send_date),
                    "from": m.sender_name,
                    "topic": _cut(m.topic, 120),
                    "text": _cut(m.content, 400),
                    "unread": True if not m.read_date else None,
                    "attachment": True if m.has_attachment else None,
                }
            )
            for m in sorted(data.messages, key=lambda m: m.send_date or "", reverse=True)
            if (_day(m.send_date) or date.min) >= since
        ][:15]
        sent = [
            _compact(
                {
                    "date": _dated(m.send_date),
                    "to": m.receiver_name,
                    "topic": _cut(m.topic, 120),
                    "text": _cut(m.content, 200),
                }
            )
            for m in sorted(data.sent_messages, key=lambda m: m.send_date or "", reverse=True)
            if (_day(m.send_date) or date.min) >= since
        ][:10]
        announcements = [
            _compact(
                {
                    "date": _dated(n.start_date or n.creation_date),
                    "topic": _cut(n.subject, 120),
                    "text": _cut(n.content, 400),
                    "unread": True if not n.was_read else None,
                }
            )
            for n in sorted(
                data.school_notices, key=lambda n: n.creation_date or n.start_date or "", reverse=True
            )
            if (_day(n.creation_date or n.start_date) or date.min) >= since
        ][:10]
        return _compact(
            {
                "messages_available": True if data.messages_available else None,
                "unread_messages": data.unread_message_count,
                "messages": messages,
                "sent_messages": sent,
                "announcements": announcements,
            }
        )


class LessonTopicsTool(_LibrusTool):
    name = "librus_get_lesson_topics"
    description = (
        "What was taught: the topic of every lesson held, by date and lesson number, with "
        "whether the student missed it. Use it for 'what was in maths yesterday', 'what did "
        "she miss while ill', 'what have they covered in history this month'."
    )
    parameters = _schema(
        {
            "date": (str, "Only this day, YYYY-MM-DD."),
            "days": (int, "How many days back from today. Defaults to 7, at most 60."),
            "subject": (str, "Only this subject (part of its name)."),
            "student": _STUDENT_FIELD,
        }
    )

    async def _async_for_student(self, coordinator, data, today, args):
        only = _day(str(args.get("date") or ""))
        since = today - timedelta(days=_int_arg(args, "days", 7, 1, MAX_DAYS))
        subject_ids = _matching_subject_ids(data, args.get("subject"))
        missed = missed_lessons(data)
        lessons = []
        for t in sorted(data.lesson_topics, key=lambda t: (t.date or "", t.lesson_no or 0)):
            day = _day(t.date)
            if day is None or (only is not None and day != only) or (only is None and day < since):
                continue
            if subject_ids is not None and t.subject_id not in subject_ids:
                continue
            absent = (day.isoformat(), t.lesson_no) in missed
            lessons.append(
                _compact(
                    {
                        "date": _dated(day),
                        "no": t.lesson_no,
                        "subject": _subject_name(data, t.subject_id),
                        "topic": _cut(t.topic, 200),
                        "trip": True if t.is_trip else None,
                        "student_was_absent": True if absent else None,
                    }
                )
            )
        return _compact(
            {
                "lessons": lessons[-80:],
                "missed_by_student": sum(1 for l in lessons if l.get("student_was_absent")) or None,
                "no_topics": True if not lessons else None,
            }
        )


TOOLS: tuple[type[_LibrusTool], ...] = (
    TimetableTool,
    GradesTool,
    UpcomingTool,
    LessonTopicsTool,
    AttendanceTool,
    BehaviourTool,
    SchoolInfoTool,
    MessagesTool,
)


# ---------------------------------------------------------------------------
# The API and its registration
# ---------------------------------------------------------------------------


class LibrusLLMAPI(llm.API):
    """The tool set a conversation agent turns on as "Librus Synergia"."""

    async def async_get_api_instance(self, llm_context: llm.LLMContext) -> llm.APIInstance:
        students = _students(self.hass)
        if students:
            who = "; ".join(
                f"{name} (class {coordinator.data.school_class.display_name})"
                if coordinator.data.school_class
                else name
                for name, coordinator in students
            )
        else:
            who = "no student data loaded yet"
        prompt = (
            "Librus Synergia is the Polish school e-register. Use the librus_* tools to "
            "answer questions about school for: "
            f"{who}. Today is {_dated(dt_util.now().date())}. Every date in tool results "
            "comes with its weekday; trust those weekdays instead of working them out. "
            "For 'tomorrow' or a weekday, pass that date to librus_get_timetable. The "
            "tools only read data; nothing is changed or sent. Answer in the user's "
            "language and keep subject names as Librus writes them."
        )
        return llm.APIInstance(
            api=self,
            api_prompt=prompt,
            llm_context=llm_context,
            tools=[tool() for tool in TOOLS],
        )


@callback
def async_setup_llm_api(hass: HomeAssistant) -> None:
    """Register the API once per Home Assistant run (it looks up loaded
    entries on every call, so later entries need nothing more)."""
    if _DATA_UNREGISTER in hass.data:
        return
    try:
        unregister = llm.async_register_api(
            hass, LibrusLLMAPI(hass=hass, id=LLM_API_ID, name=LLM_API_NAME)
        )
    except Exception as err:  # noqa: BLE001 - never block the integration over this
        _LOGGER.warning("Could not register the Librus Synergia LLM API: %s", err)
        return
    # Home Assistant before 2025 returns None and cannot unregister; the
    # API then stays registered until restart and simply finds no students.
    hass.data[_DATA_UNREGISTER] = unregister


@callback
def async_unload_llm_api(hass: HomeAssistant) -> None:
    """Remove the API with the last entry, where Home Assistant allows it."""
    unregister = hass.data.get(_DATA_UNREGISTER)
    if callable(unregister):
        unregister()
        hass.data.pop(_DATA_UNREGISTER)
