"""How many lessons of a subject fall in a date range - the `count_lessons`
action ("how many maths lessons before the test?").

A week Librus has a timetable for counts its real lessons (cancelled ones
left out unless asked for, partly free days' lessons left out). Any other
day - a week further than the integration fetches, or a week Librus hasn't
published yet - is counted from the standing weekly plan (`TimetableEntries`)
and marked `source: "plan"`. Whole days off are skipped either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from librus_synergia.models import LessonData, LibrusData

from .absence_budget import _as_id, _free_lessons, _freed
from .subject_emoji import fold

MAX_RANGE_DAYS = 366


@dataclass(frozen=True, slots=True)
class CountedLesson:
    day: date
    lesson_no: int | None
    subject_id: int | str
    start: str | None
    end: str | None
    source: str  # "timetable" or "plan"
    cancelled: bool = False


def match_subjects(data: LibrusData, query: str) -> dict[int | str, str]:
    """The student's subjects the text points at: the id itself, or a name
    containing any of the comma-separated words (case and Polish letters
    ignored) - "matem", "polski, angielski"."""
    words = [fold(part.strip()) for part in str(query).split(",") if part.strip()]
    found: dict[int | str, str] = {}
    for subject_id, name in data.subjects.items():
        if any(word == str(subject_id) or word in fold(name) for word in words):
            found[subject_id] = name
    return found


def lessons_before(
    data: LibrusData, subject_id: int | str | None, today: date, day: date
) -> int | None:
    """How many lessons of the subject fall after today and before `day`
    (a test's date) - the polled timetable, then the standing plan. Today
    is left out on purpose: the number then stays the same all day. None
    without a subject; 0 when the test is tomorrow or today."""
    if subject_id is None:
        return None
    try:
        subject = int(subject_id)
    except (TypeError, ValueError):
        subject = subject_id
    start, end = today + timedelta(days=1), day - timedelta(days=1)
    if end < start:
        return 0
    return len(count_lessons(data, data.timetable, {subject}, start, end, today))


def _week_is_empty(timetable: dict[date, list[LessonData]], day: date) -> bool:
    monday = day - timedelta(days=day.weekday())
    return not any(timetable.get(monday + timedelta(days=i)) for i in range(7))


def count_lessons(
    data: LibrusData,
    timetable: dict[date, list[LessonData]],
    subject_ids: set[int | str],
    start: date,
    end: date,
    today: date,
    *,
    include_cancelled: bool = False,
) -> list[CountedLesson]:
    """The lessons of `subject_ids` from `start` to `end` (both included).
    `timetable` holds the weeks fetched for the range (the coordinator's
    polled ones included)."""
    whole_free, partly_free = _free_lessons(data)
    counted: list[CountedLesson] = []
    day = start
    while day <= end:
        if day in whole_free:
            day += timedelta(days=1)
            continue
        freed = partly_free.get(day)
        # A future week with no lesson at all is one Librus hasn't published
        # yet - the standing plan says more about it than an empty week.
        known = day in timetable and not (day > today and _week_is_empty(timetable, day))
        if known:
            for lesson in timetable[day]:
                subject_id = _as_id(lesson.subject_id)
                if subject_id not in subject_ids or _freed(freed, lesson.lesson_no):
                    continue
                if lesson.is_canceled and not include_cancelled:
                    continue
                counted.append(
                    CountedLesson(
                        day=day,
                        lesson_no=lesson.lesson_no,
                        subject_id=subject_id,
                        start=lesson.hour_from,
                        end=lesson.hour_to,
                        source="timetable",
                        cancelled=lesson.is_canceled,
                    )
                )
        else:
            weekday = day.isoweekday()
            for slot in data.standing_timetable:
                if (
                    slot.day_of_week != weekday
                    or slot.subject_id not in subject_ids
                    or not slot.valid_on(day)
                    or _freed(freed, slot.lesson_no)
                ):
                    continue
                counted.append(
                    CountedLesson(
                        day=day,
                        lesson_no=slot.lesson_no,
                        subject_id=slot.subject_id,
                        start=None,
                        end=None,
                        source="plan",
                    )
                )
        day += timedelta(days=1)
    counted.sort(key=lambda lesson: (lesson.day, lesson.lesson_no or 0))
    return counted
