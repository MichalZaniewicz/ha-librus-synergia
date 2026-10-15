"""How many more lessons a student can still miss in each subject this
semester before missing more than half of them - past that the school can
leave the subject unclassified ("nieklasyfikowany").

An ESTIMATE: the lessons held so far are the subject's attendance records
this semester, the lessons still to come are counted from today's known
timetable weeks (cancelled lessons left out) and, beyond them, from the
standing weekly plan (`TimetableEntries`), skipping school and class free
days. A teacher who doesn't take attendance in Librus makes the held count
too low, which only makes the estimate stricter. Without the standing plan
there's no estimate at all - counting only the next two weeks would make
the allowance look far too small. Used by the Lowest subject attendance
sensor, Assist and the weekly AI summary. Pure functions over `LibrusData`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from librus_synergia.models import LibrusData

from .coordinator import free_day_lesson_range

# A semester is never longer than this - a guard against a broken date.
_MAX_SEMESTER_DAYS = 250

# Headline figures (the sensor's `fewest_absences_left`, the AI summary) skip
# subjects with fewer attendance records than this this semester: a teacher
# who doesn't take attendance would make them look alarming. Same number as
# the Lowest subject attendance sensor's own minimum.
MIN_HELD = 5


@dataclass(frozen=True, slots=True)
class SubjectBudget:
    subject_id: int | str
    subject: str
    held: int
    absent: int
    remaining: int

    @property
    def expected(self) -> int:
        """Lessons in the whole semester: held so far plus still to come."""
        return self.held + self.remaining

    @property
    def allowed(self) -> int:
        """Absences that still keep it at no more than half."""
        return self.expected // 2

    @property
    def left(self) -> int:
        """Absences still allowed; negative once over half already."""
        return self.allowed - self.absent

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "left": self.left,
            "absent": self.absent,
            "allowed": self.allowed,
            "held": self.held,
            "remaining": self.remaining,
        }


@dataclass(frozen=True, slots=True)
class AbsenceBudget:
    semester: int
    end: date
    # Fewest absences left first.
    subjects: list[SubjectBudget]


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def _as_id(value: Any) -> int | str | None:
    """Timetable ids can come as strings ("41999"); everything else is int."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def current_semester(data: LibrusData, today: date) -> tuple[int, date | None, date] | None:
    """(semester, first day or None, last day) of the semester `today` is
    in, from the Class sensor's dates; None outside the school year or when
    the dates are missing."""
    school_class = data.school_class
    if school_class is None:
        return None
    year_start = _day(school_class.begin_school_year)
    first_end = _day(school_class.end_first_semester)
    year_end = _day(school_class.end_school_year)
    if first_end is None or year_end is None:
        return None
    if year_start is not None and today < year_start:
        return None
    if today <= first_end:
        return 1, year_start, first_end
    if today <= year_end:
        return 2, first_end + timedelta(days=1), year_end
    return None


def _free_lessons(data: LibrusData) -> tuple[set[date], dict[date, list[tuple[int, int]]]]:
    """Whole free days, and the freed lesson ranges of partly free days."""
    whole: set[date] = set()
    partial: dict[date, list[tuple[int, int]]] = {}
    for free in data.free_days:
        start, end = _day(free.date_from), _day(free.date_to)
        if start is None or end is None or end < start or (end - start).days > 400:
            continue
        first, last = free_day_lesson_range(free)
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            if free.whole_day or first is None or last is None:
                whole.add(day)
            else:
                partial.setdefault(day, []).append((first, last))
    return whole, partial


def _freed(ranges: list[tuple[int, int]] | None, lesson_no: int | None) -> bool:
    if not ranges or lesson_no is None:
        return False
    return any(first <= lesson_no <= last for first, last in ranges)


def absence_budget(data: LibrusData, today: date) -> AbsenceBudget | None:
    """Every subject's allowance for the current semester, or None when it
    can't be estimated (outside the school year, no semester dates, no
    standing plan)."""
    semester = current_semester(data, today)
    if semester is None or not data.standing_timetable:
        return None
    number, start, end = semester
    if (end - today).days > _MAX_SEMESTER_DAYS:
        return None

    held: dict[int | str, int] = {}
    absent: dict[int | str, int] = {}
    for record in data.attendances:
        day = _day(record.date)
        if day is None or day > today or day > end or (start is not None and day < start):
            continue
        if record.lesson_id is None or record.type_id is None:
            continue
        subject_id = data.lesson_subjects.get(record.lesson_id)
        kind = data.attendance_types.get(record.type_id)
        if subject_id is None or kind is None:
            continue
        held[subject_id] = held.get(subject_id, 0) + 1
        if not kind.is_presence_kind:
            absent[subject_id] = absent.get(subject_id, 0) + 1

    # Only subjects still in the student's plan: one that's gone from it
    # (FOUND LIVE: a student who doesn't attend religion, with two old
    # records) has nothing to estimate - it would read "over half" forever.
    iso_today = today.isoformat()
    in_plan: set[int | str] = {
        slot.subject_id
        for slot in data.standing_timetable
        if slot.subject_id is not None and (slot.date_to is None or slot.date_to[:10] >= iso_today)
    }
    in_plan.update(
        subject_id
        for lessons in data.timetable.values()
        for lesson in lessons
        if (subject_id := _as_id(lesson.subject_id)) is not None
    )

    whole_free, partly_free = _free_lessons(data)
    remaining: dict[int | str, int] = {}
    day = today + timedelta(days=1)
    while day <= end:
        if day not in whole_free:
            freed = partly_free.get(day)
            if day in data.timetable:
                # A known week: the real lessons, cancellations left out.
                for lesson in data.timetable[day]:
                    subject_id = _as_id(lesson.subject_id)
                    if lesson.is_canceled or subject_id is None or _freed(freed, lesson.lesson_no):
                        continue
                    remaining[subject_id] = remaining.get(subject_id, 0) + 1
            else:
                weekday = day.isoweekday()
                for slot in data.standing_timetable:
                    if (
                        slot.day_of_week != weekday
                        or slot.subject_id is None
                        or not slot.valid_on(day)
                        or _freed(freed, slot.lesson_no)
                    ):
                        continue
                    remaining[slot.subject_id] = remaining.get(slot.subject_id, 0) + 1
        day += timedelta(days=1)

    subjects = [
        SubjectBudget(
            subject_id=subject_id,
            subject=name,
            held=held.get(subject_id, 0),
            absent=absent.get(subject_id, 0),
            remaining=remaining.get(subject_id, 0),
        )
        for subject_id in set(held) | set(remaining)
        if subject_id in in_plan and (name := data.subjects.get(subject_id))
    ]
    subjects.sort(key=lambda b: (b.left, b.subject))
    return AbsenceBudget(semester=number, end=end, subjects=subjects)
