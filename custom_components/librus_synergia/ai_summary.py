"""Weekly AI summary of the school week, through Home Assistant's AI Task.

Same model as ha-suunto's daily AI insight: no API key lives in this
integration. The user sets up any AI provider (Google Gemini, OpenAI,
Anthropic, a local Ollama...) as a normal Home Assistant integration and
picks its ``ai_task`` entity in our options; we only call the
``ai_task.generate_data`` action with a compact JSON snapshot of data the
coordinator already has. Nothing extra is fetched from Librus for this.

What it covers: the 7 days ending on the run day (grades, how the averages
moved, attendance, behaviour notes, optionally messages/announcements) and
the 7 days after it (agenda, homework due, timetable changes, free days).

When it runs:
- on the chosen weekday at the chosen time;
- on a coordinator update within 2 days after that moment, if the scheduled
  run was missed (HA down) or failed (provider down, quota) - at most
  ``MAX_ATTEMPTS`` tries per scheduled run;
- on demand, from the "Generate weekly summary" button.
A scheduled run is skipped (and counted as done) when the whole window is
empty - summer holidays should not cost a model call every week. The result
is kept in a Store, so a restart never pays for the same summary twice.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from librus_synergia.models import LibrusData

from .const import (
    AI_AUDIENCE_STUDENT,
    AVERAGE_MODE_ARITHMETIC,
    CONF_AVERAGE_MODE,
    DEFAULT_AVERAGE_MODE,
    DOMAIN,
    EVENT_WEEKLY_SUMMARY,
)
from .coordinator import (
    calculate_average,
    days_since_last_absence,
    days_since_last_negative_note,
    good_grade_streak,
    infer_subject_id,
    teacher_subject_ids,
)
from .exam_prep import exam_prep, missed_lessons
from .forecast import subject_forecasts

if TYPE_CHECKING:
    from .coordinator import LibrusDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# A sensor state is capped at 255 characters by Home Assistant.
MAX_STATE_LENGTH = 255
# Tries per scheduled run (the first one plus retries on later updates).
MAX_ATTEMPTS = 3
# How long after the scheduled moment a missed/failed run is still made up.
CATCH_UP = timedelta(days=2)
WINDOW_DAYS = 7
# Free text from Librus is cut to this many characters in the snapshot.
TEXT_LIMIT = 300
STATUSES = ("good", "ok", "caution")

_LANGUAGE_NAMES = {
    "en": "English",
    "pl": "Polish",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "nl": "Dutch",
    "pt": "Portuguese",
    "uk": "Ukrainian",
}

# Sections of the summary, in display order: key -> what it covers. Each
# comes back as `<key>` (text) and `<key>_status`, and is exposed on the
# sensor as ``sections[key] = {"status", "text"}`` for the cards.
SECTIONS: dict[str, str] = {
    "grades": "Grades: every grade from this week (subject, value, category, weight,"
    " teacher comment), what went well and what did not, and how the averages moved"
    " (averages[].now against averages[].week_ago).",
    "attendance": "Attendance: this week's lessons, absences (excused or not), lates,"
    " which subjects were missed, and what is still waiting to be excused overall.",
    "behaviour": "Behaviour: notes (uwagi) from this week - positive, negative,"
    " neutral - the behaviour grade if there is one, and the streaks.",
    "next_week": "Next week: tests and other agenda entries, homework due, cancelled"
    " lessons and substitutions, free days. Concrete dates and subjects.",
}
# Only when the user opted in to sending messages/announcements.
NEWS_SECTION = (
    "school_news",
    "From the school: the important points of this week's private messages and"
    " announcements (meetings, trips, payments, deadlines). Skip newsletters and noise.",
)


def _sections(include_news: bool) -> dict[str, str]:
    return {**SECTIONS, NEWS_SECTION[0]: NEWS_SECTION[1]} if include_news else dict(SECTIONS)


def build_structure(include_news: bool) -> dict[str, dict[str, Any]]:
    """Schema of the answer, in the format the ai_task.generate_data action takes."""
    fields: dict[str, dict[str, Any]] = {
        "headline": {
            "description": "One sentence, at most 120 characters: the key message of the week.",
            "required": True,
            "selector": {"text": {}},
        },
        "status": {
            "description": "Overall call for the week.",
            "required": True,
            "selector": {"select": {"options": list(STATUSES)}},
        },
    }
    for key, covers in _sections(include_news).items():
        fields[key] = {
            "description": (
                "60-130 words, one or two paragraphs separated by a blank line: what"
                f" happened, then what it means. {covers}"
            ),
            "required": True,
            "selector": {"text": {"multiline": True}},
        }
        fields[f"{key}_status"] = {
            "description": f"How the {key} section looks.",
            "required": True,
            "selector": {"select": {"options": list(STATUSES)}},
        }
    fields["advice"] = {
        "description": "2-4 short, concrete to-dos for the coming week.",
        "required": True,
        "selector": {"text": {"multiple": True}},
    }
    fields["warning"] = {
        "description": "Only if something genuinely needs attention; otherwise an empty string.",
        "required": False,
        "selector": {"text": {}},
    }
    return fields


_AUDIENCE = {
    "parent": (
        "You write to the PARENT of {name}. Refer to the student in the third person by"
        " first name. Calm, practical, like a good homeroom teacher at a parent-teacher"
        " meeting. Advice is what the parent can do: talk about something, help prepare"
        " for a test, excuse an absence, contact a teacher."
    ),
    "student": (
        "You write directly to {name}, the student (second person, informal - in Polish"
        " use \"ty\"). Friendly and encouraging, never preachy or patronising, suited to a"
        " school-age reader. Praise real progress specifically. Advice is what the student"
        " can do this week; things only a parent can do (excusing absences) are phrased"
        " as \"remind your parents\"."
    ),
}

_INSTRUCTIONS = """You summarise one student's school week from the Polish e-gradebook
Librus Synergia. Below is a JSON snapshot as of {today}.
{audience}

Rules:
- Write every field in {language}.
- Base every statement on the data given. Never invent grades, tests or events;
  ignore what is missing. An empty list means nothing happened, which is worth
  saying briefly (e.g. no absences this week).
- Be specific: name subjects, values, dates.
- Keep each section to its own topic; do not repeat a point in two sections.
- If school_days_this_week and school_days_next_week are 0 (holidays), keep every
  section to one or two sentences.
- Use plain hyphens; never long dashes. Write dates as day.month (e.g. 14.10).
- Every date in the data carries its weekday (e.g. "2026-10-06 Tue"). When you name
  a weekday, use exactly that one; never work a weekday out yourself.
- Write numbers the way {language} does (decimal comma where that is the norm).
- No diagnoses, no judging the student's character, no comparisons with other
  students. A single weak grade is not a trend.
- warning: only for something that genuinely needs attention (several unexcused
  absences, a clear drop in a subject, a test tomorrow with recent weak grades in
  it); otherwise leave it empty.

Field notes: Polish grades go 1 (worst) to 6 (best); "+" adds 0.5 and "-" takes
0.25 off (4- = 3.75). Non-numeric marks like "np" (unprepared), "bz" (missing
work), a bare "+" or "-" (activity plus/minus), "nb" (absent) do not count toward
the average. weight is the category weight in the weighted average;
counts_to_average false means it does not count. averages[].week_ago is the
average without this week's grades (null = the subject had no grades before).
average_mode says which average the family uses. forecast_on_the_edge is the
report-card grade the averages point to (a forecast - the teacher decides), only for
subjects at risk of a 1, that fell recently, or where one 6 / one 1 moves the grade. attendance.this_week counts
lesson records; open_unexcused is the whole school year. Streaks: good_grade_streak
= grades 4 or better in a row, days_without_absence / days_without_negative_note
are calendar days. attendance.absences lists this week's absences and lates;
attendance.missed_lessons_topics is what was taught in the lessons the student
missed this week (worth catching up on); attendance.justifications counts
justifications still waiting for the school's decision and recent rejected ones.
A grade with kind "text" is a teacher's written grade, not a number.
next_week.agenda holds tests, trips and other entries from the class calendar;
next_week.tests_to_revise lists the topics taught since the previous test in that
subject ("(missed)" = the student was absent) - a good source for concrete to-dos;
homework_due is real homework with its due date. new_school_documents are forms or
regulations the school shared this week.
{extra}
Data:
{data}"""


# Labels for a report built from the summary (the event carries them), in
# the HA language: blueprints have no access to the integration's
# translations.
_LABELS: dict[str, dict[str, Any]] = {
    "en": {
        "title": "Weekly summary", "advice": "For the coming week", "warning": "Warning",
        "statuses": {"good": "Good", "ok": "OK", "caution": "Needs attention"},
        "sections": {"grades": "Grades", "attendance": "Attendance",
                     "behaviour": "Behaviour", "next_week": "Next week",
                     "school_news": "From the school"},
    },
    "pl": {
        "title": "Podsumowanie tygodnia", "advice": "Na ten tydzień", "warning": "Uwaga",
        "statuses": {"good": "Dobrze", "ok": "OK", "caution": "Do uwagi"},
        "sections": {"grades": "Oceny", "attendance": "Frekwencja",
                     "behaviour": "Zachowanie", "next_week": "Następny tydzień",
                     "school_news": "Szkoła pisze"},
    },
}


def labels_for(language: str | None) -> dict[str, Any]:
    """Report labels in ``language``, English when it is not one of ours."""
    return _LABELS.get((language or "en").split("-")[0].lower(), _LABELS["en"])


def _is_schema_error(err: BaseException) -> bool:
    """Whether a service call was rejected by its schema, before running.

    Checked by class name on purpose: current cores raise voluptuous'
    ``Invalid``, the newest ones probatio's, and this must work on both.
    """
    return isinstance(err, ServiceValidationError) or any(
        cls.__name__ == "Invalid" for cls in type(err).__mro__
    )


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    """Drop unknown values so the model is not told about fields with no data."""
    return {key: value for key, value in values.items() if value not in (None, "", [], {})}


def _day(value: str | None) -> date | None:
    """The date part of a Librus "YYYY-MM-DD[ HH:MM:SS]" string."""
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _dated(value: str | date | None) -> str | None:
    """"2026-10-06 Tue": the weekday spelled out next to every date.

    Found live: given bare dates, the model worked weekdays out by itself
    and got them wrong ("na poniedziałek 6.10" for a Tuesday)."""
    day = value if isinstance(value, date) else _day(value)
    return f"{day.isoformat()} {_WEEKDAYS[day.weekday()]}" if day else None


def _cut(text: str | None, limit: int = TEXT_LIMIT) -> str | None:
    text = " ".join((text or "").split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _in(day: date | None, start: date, end: date) -> bool:
    return day is not None and start <= day <= end


def build_context(
    data: LibrusData,
    today: date,
    *,
    weighted: bool,
    include_news: bool,
    student: str | None,
    thresholds: tuple[float, ...] | None = None,
) -> dict[str, Any]:
    """Compact, JSON-ready snapshot of the week before and after ``today``."""
    week_from = today - timedelta(days=WINDOW_DAYS - 1)
    next_from, next_to = today + timedelta(days=1), today + timedelta(days=WINDOW_DAYS)

    def subject(subject_id: Any) -> str | None:
        return data.subjects.get(subject_id) if subject_id is not None else None

    def teacher(teacher_id: Any) -> str | None:
        return data.teachers.get(teacher_id) if teacher_id is not None else None

    # Grades added this week, and every subject's average with and without them.
    new_grades = [g for g in data.grades if _in(_day(g.add_date), week_from, today)]
    new_ids = {id(g) for g in new_grades}
    older = [g for g in data.grades if id(g) not in new_ids]
    grades = []
    for grade in sorted(new_grades, key=lambda g: g.add_date or ""):
        category = (
            data.grade_categories.get(grade.category_id)
            if grade.category_id is not None
            else None
        )
        if grade.is_final:
            kind = "final"
        elif grade.is_final_proposition:
            kind = "final_proposition"
        elif grade.is_semester:
            kind = "semester"
        elif grade.is_semester_proposition:
            kind = "semester_proposition"
        else:
            kind = None
        grades.append(
            _compact(
                {
                    "date": _dated(grade.add_date),
                    "subject": subject(grade.subject_id),
                    "value": grade.value,
                    "category": category.name if category else None,
                    "weight": category.weight if category else None,
                    "counts_to_average": False if category and not category.count_to_average else None,
                    "teacher": teacher(grade.teacher_id),
                    "comment": _cut("; ".join(grade.comments)),
                    "kind": kind,
                }
            )
        )
    for text_grade in sorted(data.text_grades, key=lambda t: t.date or ""):
        if not _in(_day(text_grade.date or text_grade.add_date), week_from, today):
            continue
        grades.append(
            _compact(
                {
                    "date": _dated(text_grade.date or text_grade.add_date),
                    "subject": subject(text_grade.subject_id),
                    "value": _cut(text_grade.value),
                    "category": text_grade.category,
                    "teacher": teacher(text_grade.teacher_id),
                    "kind": "text",
                }
            )
        )
    averages = []
    for subject_id in sorted(
        {g.subject_id for g in new_grades if g.subject_id is not None}, key=str
    ):
        now = calculate_average(
            data.grades, data.grade_categories, subject_id=subject_id, weighted=weighted
        )
        if now is None:
            continue
        averages.append(
            {
                "subject": subject(subject_id) or str(subject_id),
                "now": now,
                "week_ago": calculate_average(
                    older, data.grade_categories, subject_id=subject_id, weighted=weighted
                ),
            }
        )

    # Attendance this week, plus whatever is still unexcused all year.
    this_week = {"present": 0, "late": 0, "absent_excused": 0, "absent_unexcused": 0}
    absences = []
    open_unexcused: list[str] = []
    for record in data.attendances:
        kind = data.attendance_types.get(record.type_id) if record.type_id is not None else None
        day = _day(record.date)
        if kind is not None and not kind.is_presence_kind and not kind.is_excused_absence:
            open_unexcused.append(day.isoformat() if day else "?")
        if not _in(day, week_from, today) or kind is None:
            continue
        if kind.is_presence_kind:
            late = "późn" in kind.name.lower()
            this_week["late" if late else "present"] += 1
            if not late:
                continue
        else:
            this_week["absent_excused" if kind.is_excused_absence else "absent_unexcused"] += 1
        subject_id = (
            data.lesson_subjects.get(record.lesson_id) if record.lesson_id is not None else None
        )
        absences.append(
            _compact(
                {
                    "date": _dated(day),
                    "lesson_no": record.lesson_no,
                    "subject": subject(subject_id),
                    "type": kind.name,
                }
            )
        )

    notes = [
        _compact(
            {
                "date": _dated(note.date),
                "sentiment": note.sentiment,
                "category": data.note_categories.get(note.category_id)
                if note.category_id is not None
                else None,
                "teacher": teacher(note.teacher_id),
                "text": _cut(note.text),
            }
        )
        for note in data.notes
        if _in(_day(note.date), week_from, today)
    ]
    behaviour_grade = None
    if data.behaviour_grades:
        latest = max(data.behaviour_grades, key=lambda b: b.add_date or "")
        behaviour_grade = _compact(
            {
                "grade": latest.display or None,
                "name": latest.name,
                "date": _dated(latest.add_date),
                "comment": _cut("; ".join(c.strip() for c in latest.comments)),
            }
        )

    # The coming week.
    by_teacher = teacher_subject_ids(data.timetable)
    agenda = [
        _compact(
            {
                "date": _dated(item.date),
                "time": item.time_from,
                "subject": subject(item.subject_id),
                "category": data.homework_categories.get(item.category_id)
                if item.category_id is not None
                else None,
                "content": _cut(item.content),
            }
        )
        for item in sorted(data.homeworks, key=lambda h: h.date or "")
        if _in(_day(item.date), next_from, next_to)
    ]
    homework_due = [
        _compact(
            {
                "due": _dated(item.due_date),
                "subject": subject(infer_subject_id(item.teacher_id, by_teacher)),
                "teacher": teacher(item.teacher_id),
                "topic": _cut(item.topic, 120),
                "text": _cut(item.text),
                "attached_files": [f.filename or "file" for f in item.attachments] or None,
            }
        )
        for item in sorted(data.homework_assignments, key=lambda h: h.due_date or "")
        if _in(_day(item.due_date), next_from, next_to)
    ]
    timetable_changes = []
    school_days_this_week = school_days_next_week = 0
    for day, lessons in sorted(data.timetable.items()):
        held = [lesson for lesson in lessons if not lesson.is_canceled]
        if held and week_from <= day <= today:
            school_days_this_week += 1
        if not next_from <= day <= next_to:
            continue
        if held:
            school_days_next_week += 1
        for lesson in lessons:
            if lesson.is_canceled or lesson.is_substitution:
                timetable_changes.append(
                    _compact(
                        {
                            "date": _dated(day),
                            "lesson_no": lesson.lesson_no,
                            "subject": subject(lesson.subject_id),
                            "change": "cancelled" if lesson.is_canceled else "substitution",
                            "teacher": teacher(lesson.teacher_id)
                            if lesson.is_substitution
                            else None,
                        }
                    )
                )
    free_days = [
        {"name": free.name, "from": free.date_from, "to": free.date_to}
        for free in data.free_days
        if (_day(free.date_from) or date.max) <= next_to
        and (_day(free.date_to) or date.min) >= week_from
    ]

    # Lessons the student missed this week, with what was taught - what to
    # catch up on.
    missed = missed_lessons(data)
    missed_topics = [
        _compact(
            {
                "date": _dated(lesson.date),
                "lesson_no": lesson.lesson_no,
                "subject": subject(lesson.subject_id),
                "topic": _cut(lesson.topic, 160),
            }
        )
        for lesson in sorted(data.lesson_topics, key=lambda t: (t.date or "", t.lesson_no or 0))
        if _in(_day(lesson.date), week_from, today)
        and ((lesson.date or "")[:10], lesson.lesson_no) in missed
    ]
    # Tests next week with the topics to revise for each.
    tests_to_revise = [
        _compact(
            {
                "date": _dated(prep.day),
                "subject": prep.subject,
                "category": prep.category,
                "topics": [
                    _cut(t.topic, 120) + (" (missed)" if t.absent else "") for t in prep.topics[-10:]
                ],
                "missed_lessons": prep.missed or None,
            }
        )
        for prep in exam_prep(data, today, until=next_to)
        if next_from <= prep.day and prep.topics
    ]
    trips = [
        _compact(
            {
                "from": _dated(trip.date_from),
                "to": _dated(trip.date_to) if trip.date_to != trip.date_from else None,
                "destination": _cut(trip.destination, 160),
                "transport": trip.transport or None,
            }
        )
        for trip in data.school_trips
        if (_day(trip.date_from) or date.max) <= next_to
        and (_day(trip.date_to) or date.min) >= week_from
    ]
    documents = [
        _cut(item.name, 120)
        for item in data.school_files
        if _in(_day(item.added), week_from, today)
    ]
    justifications = _compact(
        {
            "waiting_for_school": sum(1 for j in data.justifications if j.is_pending) or None,
            "rejected_recently": [
                _compact({"from": _dated(j.date_from), "to": _dated(j.date_to)})
                for j in data.justifications
                if j.is_rejected and _in(_day(j.posted), today - timedelta(days=30), today)
            ]
            or None,
        }
    )

    school_class = data.school_class
    context: dict[str, Any] = {
        "student": student,
        "class": f"{school_class.number or ''}{school_class.symbol}".strip()
        if school_class
        else None,
        "today": _dated(today),
        "this_week": {"from": week_from.isoformat(), "to": today.isoformat()},
        "school_days_this_week": school_days_this_week,
        "average_mode": "weighted" if weighted else "arithmetic",
        "grades": grades,
        "averages": averages,
        # Only the subjects worth a word: a forecast 1, a forecast that fell
        # recently, or one grade away from moving.
        "forecast_on_the_edge": [
            _compact(
                {
                    "subject": f.subject,
                    "forecast_grade": f.predicted,
                    "at_risk_of_failing": True if f.at_risk else None,
                    "dropped_recently": True if f.declining else None,
                    "one_six_lifts_it": True if f.sixes_to_next == 1 else None,
                    "one_one_drops_it": True if f.ones_to_drop == 1 else None,
                }
            )
            for f in subject_forecasts(data, today, thresholds, weighted=weighted)
            if f.at_risk or f.declining or f.sixes_to_next == 1 or f.ones_to_drop == 1
        ]
        if thresholds
        else [],
        "overall_average": _compact(
            {
                "now": calculate_average(data.grades, data.grade_categories, weighted=weighted),
                "week_ago": calculate_average(older, data.grade_categories, weighted=weighted),
            }
        ),
        "attendance": _compact(
            {
                "this_week": this_week,
                "absences": absences,
                "open_unexcused": len(open_unexcused),
                "open_unexcused_dates": sorted(set(open_unexcused))[-10:],
                "missed_lessons_topics": missed_topics[:20],
                "justifications": justifications,
            }
        ),
        "new_school_documents": documents,
        "notes": notes,
        "behaviour_grade": behaviour_grade,
        "streaks": _compact(
            {
                "good_grade_streak": good_grade_streak(data.grades),
                "days_without_absence": days_since_last_absence(
                    data.attendances, data.attendance_types, school_class, today
                ),
                "days_without_negative_note": days_since_last_negative_note(
                    data.notes, school_class, today
                ),
            }
        ),
        "next_week": _compact(
            {
                "from": next_from.isoformat(),
                "to": next_to.isoformat(),
                "agenda": agenda,
                "homework_due": homework_due,
                "timetable_changes": timetable_changes[:20],
                "tests_to_revise": tests_to_revise,
                "school_trips": trips,
                "free_days": free_days,
            }
        ),
        "school_days_next_week": school_days_next_week,
    }
    if include_news:
        news = [
            _compact(
                {
                    "date": _dated(message.send_date),
                    "kind": "message",
                    "from": message.sender_name,
                    "topic": _cut(message.topic, 120),
                    "text": _cut(message.content, 500),
                }
            )
            for message in data.messages
            if _in(_day(message.send_date), week_from, today)
        ] + [
            _compact(
                {
                    "date": _dated(notice.start_date or notice.creation_date),
                    "kind": "announcement",
                    "topic": _cut(notice.subject, 120),
                    "text": _cut(notice.content, 500),
                }
            )
            for notice in data.school_notices
            if _in(_day(notice.creation_date or notice.start_date), week_from, today)
        ]
        context["school_news"] = news[:15]
    return {key: value for key, value in context.items() if value not in (None, [], {})}


def is_empty_week(context: dict[str, Any]) -> bool:
    """Nothing happened and nothing is coming (holidays): skip the model call."""
    attendance = context.get("attendance") or {}
    return not (
        context.get("school_days_this_week")
        or context.get("school_days_next_week")
        or context.get("grades")
        or context.get("notes")
        or attendance.get("absences")
        or set(context.get("next_week") or {}) - {"from", "to"}
        or context.get("school_news")
    )


def build_instructions(
    context: dict[str, Any],
    today: date,
    language: str | None,
    audience: str,
    extra: str | None,
) -> str:
    """The full prompt: persona, rules, field notes, the user's notes and the data."""
    code = (language or "en").split("-")[0].lower()
    extra = (extra or "").strip()
    name = (context.get("student") or "").split(" ")[0] or "the student"
    persona = _AUDIENCE["student" if audience == AI_AUDIENCE_STUDENT else "parent"]
    return _INSTRUCTIONS.format(
        today=today.isoformat(),
        audience=persona.format(name=name),
        language=_LANGUAGE_NAMES.get(code, f"the language with code '{code}'"),
        extra=f"\nNotes from the family (take them into account):\n{extra}\n" if extra else "",
        data=json.dumps(context, ensure_ascii=False, default=str, separators=(",", ":")),
    )


def _text(value: Any) -> str | None:
    return str(value or "").strip() or None


def parse_result(data: Any, include_news: bool) -> dict[str, Any]:
    """Normalize the model's answer; tolerate plain text and loose types.

    ``summary`` is only filled when the answer came back as plain text (a
    core without structured output); otherwise the text is in ``sections``.
    """
    if not isinstance(data, dict):
        text = str(data or "").strip()
        headline = text.split("\n", 1)[0].split(". ", 1)[0]
        return {
            "headline": headline or None, "status": None, "sections": {},
            "summary": text or None, "advice": [], "warning": None,
        }
    advice = data.get("advice") or []
    if isinstance(advice, str):
        advice = [line.strip(" -*\t") for line in advice.splitlines()]
    status = str(data.get("status") or "").strip().lower()
    sections: dict[str, dict[str, Any]] = {}
    for key in _sections(include_news):
        if text := _text(data.get(key)):
            section_status = str(data.get(f"{key}_status") or "").strip().lower()
            sections[key] = {
                "status": section_status if section_status in STATUSES else None,
                "text": text,
            }
    return {
        "headline": _text(data.get("headline")),
        "status": status if status in STATUSES else None,
        "sections": sections,
        "summary": _text(data.get("summary")),
        "advice": [str(item).strip() for item in advice if str(item).strip()],
        "warning": _text(data.get("warning")),
    }


def last_due(now: datetime, weekday: int, at: time) -> datetime:
    """The most recent scheduled moment at or before ``now`` (local time)."""
    days_back = (now.isoweekday() - weekday) % 7
    candidate = datetime.combine(now.date() - timedelta(days=days_back), at, tzinfo=now.tzinfo)
    return candidate if candidate <= now else candidate - timedelta(days=7)


class LibrusWeeklySummary:
    """Schedules, runs and stores the weekly AI summary for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: LibrusDataUpdateCoordinator,
        *,
        ai_task_entity: str,
        audience: str,
        weekday: int,
        at: time,
        extra_context: str | None,
        include_news: bool,
    ) -> None:
        """Initialize; nothing runs until async_start."""
        self.hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self.ai_task_entity = ai_task_entity
        self.audience = audience
        self.weekday = weekday
        self.at = at
        self._extra = extra_context
        self.include_news = include_news
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.weekly_summary.{entry.entry_id}"
        )
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []
        # (scheduled moment, tries for it), for the retry cap.
        self._attempts: tuple[str, int] = ("", 0)
        # The scheduled moment the last automatic run (or skip) was for.
        self._done_due: str | None = None
        self.result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.running = False
        # The "Automatic weekly summary" switch: off pauses scheduled runs
        # and catch-ups; the button still works.
        self.enabled = True

    async def async_start(self) -> None:
        """Load the stored state and hook up the triggers."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            result = stored.get("result")
            self.result = result if isinstance(result, dict) else None
            self.enabled = stored.get("enabled", True) is not False
            self._done_due = stored.get("done_due")
        self._unsubs += [
            self._coordinator.async_add_listener(self._on_update),
            async_track_time_change(
                self.hass,
                self._on_time,
                hour=self.at.hour,
                minute=self.at.minute,
                second=0,
            ),
        ]

    @callback
    def async_stop(self) -> None:
        """Remove every trigger (entry unload)."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    async def async_set_enabled(self, enabled: bool) -> None:
        """Switch automatic runs on or off."""
        if enabled == self.enabled:
            return
        self.enabled = enabled
        await self._async_save()
        self._notify()

    async def _async_save(self) -> None:
        await self._store.async_save(
            {"enabled": self.enabled, "result": self.result, "done_due": self._done_due}
        )

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        """Call ``update`` whenever the result or the running flag changes."""
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    def _due(self) -> datetime:
        return last_due(dt_util.now(), self.weekday, self.at)

    @property
    def next_run(self) -> datetime:
        """The next scheduled moment."""
        return self._due() + timedelta(days=7)

    @callback
    def _on_time(self, now: datetime) -> None:
        if now.isoweekday() == self.weekday:
            self._maybe_run()

    @callback
    def _on_update(self) -> None:
        # A missed (HA down) or failed scheduled run, made up shortly after.
        if dt_util.now() - self._due() <= CATCH_UP:
            self._maybe_run()

    @callback
    def _maybe_run(self) -> None:
        """Start the scheduled run unless it is done, paused or out of tries."""
        due = self._due().isoformat()
        if not self.enabled or self.running or self._done_due == due:
            return
        if self._coordinator.data is None:
            return
        key, count = self._attempts
        count = count if key == due else 0
        if count >= MAX_ATTEMPTS:
            return
        self._attempts = (due, count + 1)
        self._entry.async_create_background_task(
            self.hass, self._async_scheduled(due), f"{DOMAIN} weekly summary"
        )

    async def _async_scheduled(self, due: str) -> None:
        if not self.enabled:
            return  # switched off while this run was queued
        try:
            await self.async_generate(wait=True, scheduled_for=due)
        except HomeAssistantError:
            pass  # already logged and kept in last_error

    async def async_generate(self, wait: bool = False, scheduled_for: str | None = None) -> None:
        """Run the summary now. Raises HomeAssistantError on failure.

        A press while a run is going is refused; an automatic run (``wait``)
        queues behind it instead.
        """
        if self._lock.locked() and not wait:
            raise HomeAssistantError("The weekly summary is already being generated")
        async with self._lock:
            if scheduled_for is not None and self._done_due == scheduled_for:
                return
            self.running = True
            self._notify()
            try:
                result = await self._async_call(skip_empty=scheduled_for is not None)
                if result is not None:
                    self.result = result
                self.last_error = None
                if scheduled_for is not None:
                    self._done_due = scheduled_for
                await self._async_save()
                if result is not None:
                    self._fire_event(manual=scheduled_for is None)
            except Exception as err:  # noqa: BLE001 - any provider error ends up here
                self.last_error = str(err) or type(err).__name__
                _LOGGER.warning("Weekly summary failed: %s", self.last_error)
                raise HomeAssistantError(f"Weekly summary failed: {self.last_error}") from err
            finally:
                self.running = False
                self._notify()

    @callback
    def _fire_event(self, manual: bool) -> None:
        """Announce a fresh summary; ``manual`` = started from the button."""
        result = self.result or {}
        self.hass.bus.async_fire(
            EVENT_WEEKLY_SUMMARY,
            {
                "entry_id": self._entry.entry_id,
                "student": result.get("student"),
                "manual": manual,
                **{
                    key: result.get(key)
                    for key in (
                        "headline", "status", "sections", "advice", "warning",
                        "summary", "week_from", "week_to", "audience",
                    )
                },
                "labels": labels_for(self.hass.config.language),
            },
        )

    async def _async_call(self, skip_empty: bool) -> dict[str, Any] | None:
        """Ask the model; None when an automatic run found an empty week."""
        data = self._coordinator.data
        if data is None:
            raise HomeAssistantError("No Librus data yet")
        if not self.hass.services.has_service("ai_task", "generate_data"):
            raise HomeAssistantError(
                "The AI Task integration is not available (Home Assistant 2025.8 or newer is needed)"
            )
        today = dt_util.now().date()
        weighted = (
            self._entry.options.get(CONF_AVERAGE_MODE, DEFAULT_AVERAGE_MODE)
            != AVERAGE_MODE_ARITHMETIC
        )
        student = f"{data.me.first_name} {data.me.last_name}".strip() or None
        context = build_context(
            data,
            today,
            weighted=weighted,
            include_news=self.include_news,
            student=student,
            thresholds=self._coordinator.grade_thresholds,
        )
        if skip_empty and is_empty_week(context):
            _LOGGER.debug("Weekly summary skipped: nothing happened this week or next")
            return None
        service_data: dict[str, Any] = {
            "task_name": "Librus weekly summary",
            "entity_id": self.ai_task_entity,
            "instructions": build_instructions(
                context, today, self.hass.config.language, self.audience, self._extra
            ),
        }
        try:
            response = await self.hass.services.async_call(
                "ai_task",
                "generate_data",
                {**service_data, "structure": build_structure(self.include_news)},
                blocking=True,
                return_response=True,
            )
        except Exception as err:
            if not _is_schema_error(err):
                raise
            # A core whose ai_task has no `structure` support rejects the call
            # before it reaches the model, so retrying as plain text is free.
            _LOGGER.debug("Structured AI task rejected (%s), retrying as text", err)
            response = await self.hass.services.async_call(
                "ai_task", "generate_data", service_data, blocking=True, return_response=True
            )
        parsed = parse_result((response or {}).get("data"), self.include_news)
        if not parsed["headline"] and not parsed["sections"] and not parsed["summary"]:
            raise HomeAssistantError("The AI returned an empty answer")
        return {
            **parsed,
            "student": student,
            "week_from": context["this_week"]["from"],
            "week_to": context["this_week"]["to"],
            "audience": self.audience,
            "generated_at": dt_util.utcnow().isoformat(),
            "ai_task_entity": self.ai_task_entity,
        }
