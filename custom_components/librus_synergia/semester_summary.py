"""A semester in numbers: grades, attendance, behaviour and the rest of a
school semester, counted from what the integration already fetches - for
the Overall average sensor's `semester_summary` attribute and the
companion Semester in numbers card. Pure functions over `LibrusData`.

Both semesters are summed up separately; `current` says which one today
is in (the second one after the school year ends, so the card keeps the
year's last result over the summer). Before the school year starts there
is nothing to sum up.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import Any

from librus_synergia.models import GradeData, GradingSystemData, LibrusData
from librus_synergia.parsers import parse_grade_value

from .exam_prep import EXAM_RE, is_exam
from .forecast import average_sums

# How far back the average's change is measured.
TREND_DAYS = 30
# A week needs this many grades to count as the semester's best week.
BEST_WEEK_MIN_GRADES = 2
# A semester is never longer than this - a guard against a broken date.
_MAX_SEMESTER_DAYS = 250


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def semester_bounds(data: LibrusData) -> dict[int, tuple[date, date]] | None:
    """{1: (first day, last day), 2: (...)} from the Class sensor's dates,
    or None when they're missing or don't make sense."""
    school_class = data.school_class
    if school_class is None:
        return None
    start = _day(school_class.begin_school_year)
    first_end = _day(school_class.end_first_semester)
    year_end = _day(school_class.end_school_year)
    if start is None or first_end is None or year_end is None:
        return None
    if not start <= first_end < year_end:
        return None
    if (first_end - start).days > _MAX_SEMESTER_DAYS or (year_end - first_end).days > _MAX_SEMESTER_DAYS:
        return None
    return {1: (start, first_end), 2: (first_end + timedelta(days=1), year_end)}


def _in(day: date | None, bounds: tuple[date, date]) -> bool:
    return day is not None and bounds[0] <= day <= bounds[1]


def _ordinary(grade: GradeData) -> bool:
    return not (
        grade.is_semester_proposition
        or grade.is_final_proposition
        or grade.is_semester
        or grade.is_final
    )


def _grade_semester(grade: GradeData, bounds: dict[int, tuple[date, date]]) -> int | None:
    """The grade's own semester; from its date when Librus leaves it out."""
    if grade.semester in (1, 2):
        return grade.semester
    added = _day(grade.add_date)
    for number, span in bounds.items():
        if _in(added, span):
            return number
    return None


def _digit(value: str) -> int | None:
    """The grade digit 1-6 of a value like "5", "4+" or "6-"."""
    first = value.strip()[:1]
    return int(first) if first in "123456" and first else None


def _average(
    grades: list[GradeData],
    data: LibrusData,
    *,
    weighted: bool,
    grading: GradingSystemData | None,
) -> float | None:
    running, weight = average_sums(grades, data.grade_categories, weighted=weighted, grading=grading)
    return round(running / weight, 2) if weight > 0 else None


def _category_name(data: LibrusData, grade: GradeData) -> str:
    category = data.grade_categories.get(grade.category_id) if grade.category_id is not None else None
    return category.name if category is not None else ""


def _grades_section(
    data: LibrusData,
    grades: list[GradeData],
    bounds: tuple[date, date],
    until: date,
    *,
    weighted: bool,
    grading: GradingSystemData | None,
) -> dict[str, Any]:
    digits = [d for g in grades if (d := _digit(g.value)) is not None]
    distribution = Counter(digits)
    tests = [g for g in grades if EXAM_RE.search(_category_name(data, g))]
    test_values = [v for g in tests if (v := parse_grade_value(g.value, grading)) is not None]
    elapsed_days = max(1, (min(until, bounds[1]) - bounds[0]).days + 1)
    weeks = max(1.0, elapsed_days / 7)

    average = _average(grades, data, weighted=weighted, grading=grading)
    cutoff = min(until, bounds[1]) - timedelta(days=TREND_DAYS)
    before = [g for g in grades if (added := _day(g.add_date)) is not None and added <= cutoff]
    average_before = _average(before, data, weighted=weighted, grading=grading)

    # The average at the end of each week, for a sparkline.
    series: list[list[Any]] = []
    week_end = bounds[0] + timedelta(days=6 - bounds[0].weekday())
    last = min(until, bounds[1])
    while True:
        point = min(week_end, last)
        so_far = [g for g in grades if (added := _day(g.add_date)) is not None and added <= point]
        value = _average(so_far, data, weighted=weighted, grading=grading)
        if value is not None:
            series.append([point.isoformat(), value])
        if point >= last:
            break
        week_end += timedelta(days=7)

    # Records: the best week (by plain mean, at least two grades) and the
    # day with the most grades.
    by_week: dict[date, list[float]] = {}
    by_day: Counter[date] = Counter()
    for grade in grades:
        added = _day(grade.add_date)
        if added is None:
            continue
        by_day[added] += 1
        value = parse_grade_value(grade.value, grading)
        if value is not None:
            by_week.setdefault(added - timedelta(days=added.weekday()), []).append(value)
    best_week = None
    for monday, values in by_week.items():
        if len(values) < BEST_WEEK_MIN_GRADES:
            continue
        mean = round(sum(values) / len(values), 2)
        if best_week is None or mean > best_week["average"]:
            best_week = {"start": monday.isoformat(), "average": mean, "count": len(values)}
    busiest = max(by_day.items(), key=lambda item: (item[1], item[0]), default=None)

    return {
        "count": len(grades),
        "distribution": {str(d): distribution.get(d, 0) for d in range(1, 7)},
        "most_common": distribution.most_common(1)[0][0] if distribution else None,
        "sixes": distribution.get(6, 0),
        "ones": distribution.get(1, 0),
        "plus": sum(1 for g in grades if g.value.strip() == "+"),
        "minus": sum(1 for g in grades if g.value.strip() == "-"),
        "tests": len(tests),
        "tests_average": round(sum(test_values) / len(test_values), 2) if test_values else None,
        "corrections": sum(1 for g in grades if g.improves_id is not None),
        "with_comments": sum(1 for g in grades if g.comments),
        "per_week": round(len(grades) / weeks, 1),
        "average": average,
        "average_change": round(average - average_before, 2)
        if average is not None and average_before is not None
        else None,
        "average_series": series,
        "best_week": best_week,
        "busiest_day": {"date": busiest[0].isoformat(), "count": busiest[1]} if busiest else None,
    }


def _subjects_section(
    data: LibrusData,
    grades: list[GradeData],
    *,
    weighted: bool,
    grading: GradingSystemData | None,
) -> list[dict[str, Any]]:
    by_subject: dict[Any, list[GradeData]] = {}
    for grade in grades:
        if grade.subject_id is not None:
            by_subject.setdefault(grade.subject_id, []).append(grade)
    subjects = []
    for subject_id, subject_grades in by_subject.items():
        average = _average(subject_grades, data, weighted=weighted, grading=grading)
        if average is None:
            continue
        subjects.append(
            {
                "subject": data.subjects.get(subject_id, str(subject_id)),
                "average": average,
                "count": len(subject_grades),
            }
        )
    subjects.sort(key=lambda s: (-s["average"], s["subject"]))
    return subjects


def _attendance_section(data: LibrusData, number: int, bounds: tuple[date, date]) -> dict[str, Any]:
    total = present = excused = unexcused = late = 0
    weekdays = [0, 0, 0, 0, 0]
    last_absence: date | None = None
    for record in data.attendances:
        day = _day(record.date)
        if record.semester != number and not (record.semester is None and _in(day, bounds)):
            continue
        kind = data.attendance_types.get(record.type_id)  # type: ignore[arg-type]
        total += 1
        if kind is None:
            continue
        if kind.is_presence_kind:
            present += 1
            if "późn" in kind.name.lower():
                late += 1
            continue
        if kind.is_excused_absence:
            excused += 1
        else:
            unexcused += 1
        if day is not None:
            if day.weekday() < 5:
                weekdays[day.weekday()] += 1
            if last_absence is None or day > last_absence:
                last_absence = day
    return {
        "records": total,
        "present": present,
        "percentage": round(100 * present / total, 1) if total else None,
        "absences": excused + unexcused,
        "excused": excused,
        "unexcused": unexcused,
        "late": late,
        "absences_by_weekday": weekdays,
        "last_absence": last_absence.isoformat() if last_absence else None,
    }


def _summary(
    data: LibrusData,
    number: int,
    bounds: tuple[date, date],
    today: date,
    *,
    weighted: bool,
    grading: GradingSystemData | None,
    achievements: list[dict[str, str]],
) -> dict[str, Any]:
    start, end = bounds
    total_days = (end - start).days + 1
    elapsed = max(0, min(total_days, (today - start).days + 1))
    grades = [
        g for g in data.grades if _ordinary(g) and _grade_semester(g, {number: bounds}) == number
    ]
    notes = [n for n in data.notes if _in(_day(n.date), bounds)]
    sentiments = Counter(n.sentiment or "neutral" for n in notes)
    exams = [
        item for item in data.homeworks if _in(_day(item.date), bounds) and is_exam(data, item)
    ]
    earned = [a for a in achievements if _in(_day(a.get("date")), bounds)]
    changes = [c for c in data.plan_changes if _in(_day(c.original_date), bounds)]
    return {
        "semester": number,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": total_days,
        "days_elapsed": elapsed,
        "progress": round(elapsed / total_days, 3),
        "finished": today > end,
        "grades": _grades_section(data, grades, bounds, today, weighted=weighted, grading=grading),
        "subjects": _subjects_section(data, grades, weighted=weighted, grading=grading),
        "attendance": _attendance_section(data, number, bounds),
        "lessons": {
            "held": sum(1 for t in data.lesson_topics if _in(_day(t.date), bounds)),
            "cancelled": sum(1 for c in changes if c.cancelled),
            "substitutions": sum(1 for c in changes if not c.cancelled),
        },
        "notes": {
            "positive": sentiments.get("positive", 0),
            "negative": sentiments.get("negative", 0),
            "neutral": sentiments.get("neutral", 0),
        },
        "homework": sum(1 for h in data.homework_assignments if _in(_day(h.date), bounds)),
        "exams": {
            "done": sum(1 for item in exams if (d := _day(item.date)) is not None and d < today),
            "upcoming": sum(1 for item in exams if (d := _day(item.date)) is not None and d >= today),
        },
        "trips": sum(1 for trip in data.school_trips if _in(_day(trip.date_from), bounds)),
        "badges": len(earned),
        "latest_badge": earned[-1]["title"] if earned else None,
    }


def semester_summary(
    data: LibrusData,
    today: date,
    *,
    weighted: bool = True,
    grading: GradingSystemData | None = None,
    achievements: list[dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    """{"current": 1|2, "semesters": {"1": {...}, "2": {...}}, "library_books": n}
    - a semester not started yet is left out. None without the class's
    semester dates or before the school year starts."""
    bounds = semester_bounds(data)
    if bounds is None or today < bounds[1][0]:
        return None
    current = 1 if today <= bounds[1][1] else 2
    semesters = {
        str(number): _summary(
            data,
            number,
            span,
            today,
            weighted=weighted,
            grading=grading,
            achievements=achievements or [],
        )
        for number, span in bounds.items()
        if today >= span[0]
    }
    return {
        "current": current,
        "semesters": semesters,
        "library_books": len(data.library_lendings),
    }
