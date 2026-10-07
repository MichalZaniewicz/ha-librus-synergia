"""Grade forecast: what each subject's average would give on the report card.

Pure functions over the already-fetched grades - no Librus request. The
grade a teacher actually gives is their decision (and the school's statute,
"WSO"), so this is a forecast from the average, nothing more: the thresholds
are an option because schools use different ones.

Basis: in the first semester the semester's own grades (that is what the
semester grade is given for), in the second semester the whole school year
(the year-end grade usually reflects both semesters).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from librus_synergia.models import GradeCategoryData, GradeData, LibrusData
from librus_synergia.parsers import parse_grade_value

# Minimum average for a 2, 3, 4, 5 and 6 - a common setup, but schools
# differ (see the option's description).
DEFAULT_GRADE_THRESHOLDS = "1.75, 2.75, 3.75, 4.75, 5.50"
# "Świadectwo z wyróżnieniem" needs an average of at least 4.75 (and at
# least a very good behaviour grade, which is not checked here).
HONOURS_AVERAGE = 4.75
# A forecast that fell by a whole grade within this many days counts as
# "declining".
DECLINE_WINDOW_DAYS = 14

BASIS_SEMESTER_1 = "semester_1"
BASIS_SCHOOL_YEAR = "school_year"


def parse_thresholds(text: str | None) -> tuple[float, ...]:
    """Five ascending averages (for a 2..6) from "1.75, 2.75, ..." -
    decimal commas allowed ("1,75; 2,75"). Anything unusable falls back to
    the default, so a typo never breaks the sensors."""
    default = (1.75, 2.75, 3.75, 4.75, 5.5)
    if not text:
        return default
    parts = [p for p in text.replace(";", " ").replace(", ", " ").split() if p]
    try:
        values = tuple(float(p.replace(",", ".")) for p in parts)
    except ValueError:
        return default
    if len(values) != 5 or any(
        b <= a for a, b in zip(values, values[1:], strict=False)
    ):
        return default
    if values[0] <= 1 or values[-1] >= 6:
        return default
    return values


def _counts(grade: GradeData) -> bool:
    return not (
        grade.is_semester_proposition
        or grade.is_final_proposition
        or grade.is_semester
        or grade.is_final
    )


def average_sums(
    grades: list[GradeData],
    categories: dict[int, GradeCategoryData],
    *,
    subject_id: int | None = None,
    semester: int | None = None,
    weighted: bool = True,
) -> tuple[float, float]:
    """(sum of value x weight, sum of weights) over the grades that count
    toward the average - the two numbers every average and forecast here
    is built from."""
    running = 0.0
    weight_total = 0.0
    for grade in grades:
        if not _counts(grade):
            continue
        if subject_id is not None and grade.subject_id != subject_id:
            continue
        if semester is not None and grade.semester != semester:
            continue
        category = (
            categories.get(grade.category_id) if grade.category_id is not None else None
        )
        if category is not None and not category.count_to_average:
            continue
        numeric = parse_grade_value(grade.value)
        if numeric is None:
            continue
        weight = (category.weight if category is not None else 1) if weighted else 1
        running += numeric * weight
        weight_total += weight
    return running, weight_total


def current_semester(data: LibrusData, today: date) -> int | None:
    """1 or 2 from the class's first-semester end date; without it, the
    latest semester any grade belongs to."""
    end_first = data.school_class.end_first_semester if data.school_class else None
    if end_first:
        try:
            return 1 if today <= date.fromisoformat(end_first[:10]) else 2
        except ValueError:
            pass
    semesters = [g.semester for g in data.grades if g.semester]
    return max(semesters) if semesters else None


def predicted_grade(average: float, thresholds: tuple[float, ...]) -> int:
    return 1 + sum(1 for t in thresholds if average >= t)


@dataclass(slots=True)
class SubjectForecast:
    subject_id: int
    subject: str
    average: float
    # Sum of the counted grades' weights on this basis (the count in the
    # arithmetic mode) - lets a card add a grade to the exact average.
    weight_total: float
    predicted: int
    # The average needed for the next grade up (None at 6).
    next_grade_at: float | None
    # How many 6s (weight 1) lift the forecast by one grade (None at 6).
    sixes_to_next: int | None
    # How many 1s (weight 1) drop it by one grade (None at 1).
    ones_to_drop: int | None
    declining: bool
    # The teacher's semester/year-end proposition, once there is one.
    proposed: str | None

    @property
    def at_risk(self) -> bool:
        return self.predicted == 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "subject": self.subject,
            "average": self.average,
            "predicted": self.predicted,
            "next_grade_at": self.next_grade_at,
            "sixes_to_next": self.sixes_to_next,
            "ones_to_drop": self.ones_to_drop,
            "declining": self.declining,
            "at_risk": self.at_risk,
            "proposed": self.proposed,
        }


def _sixes_to_next(total: float, weight: float, target: float) -> int:
    # (total + 6n) / (weight + n) >= target
    return max(1, math.ceil((target * weight - total) / (6 - target) - 1e-9))


def _ones_to_drop(total: float, weight: float, floor: float) -> int:
    # (total + n) / (weight + n) < floor
    return math.floor((total - floor * weight) / (floor - 1) + 1e-9) + 1


def forecast_basis(data: LibrusData, today: date) -> tuple[str, int | None]:
    """(basis name, the semester whose grades count - None for the whole
    year)."""
    if current_semester(data, today) == 2:
        return BASIS_SCHOOL_YEAR, None
    return BASIS_SEMESTER_1, 1


def subject_forecasts(
    data: LibrusData,
    today: date,
    thresholds: tuple[float, ...],
    *,
    weighted: bool = True,
) -> list[SubjectForecast]:
    """One forecast per subject with at least one counted grade, worst
    first."""
    basis, semester = forecast_basis(data, today)
    # A grade without a semester number counts in either basis.
    grades = [
        g for g in data.grades if semester is None or g.semester in (semester, None)
    ]
    cutoff = (today - timedelta(days=DECLINE_WINDOW_DAYS)).isoformat()
    older = [g for g in grades if (g.add_date or "")[:10] < cutoff]
    proposition = (
        (lambda g: g.is_semester_proposition)
        if basis == BASIS_SEMESTER_1
        else (lambda g: g.is_final_proposition)
    )
    subject_ids = {g.subject_id for g in data.grades if g.subject_id is not None}
    result: list[SubjectForecast] = []
    for subject_id in subject_ids:
        total, weight = average_sums(
            grades, data.grade_categories, subject_id=subject_id, weighted=weighted
        )
        if weight <= 0:
            continue
        average = total / weight
        predicted = predicted_grade(average, thresholds)
        old_total, old_weight = average_sums(
            older, data.grade_categories, subject_id=subject_id, weighted=weighted
        )
        declining = old_weight > 0 and predicted < predicted_grade(
            old_total / old_weight, thresholds
        )
        next_at = thresholds[predicted - 1] if predicted < 6 else None
        proposed = next(
            (
                g.value
                for g in data.grades
                if g.subject_id == subject_id and proposition(g)
            ),
            None,
        )
        result.append(
            SubjectForecast(
                subject_id=subject_id,
                subject=data.subjects.get(subject_id, str(subject_id)),
                average=round(average, 2),
                weight_total=weight,
                predicted=predicted,
                next_grade_at=next_at,
                sixes_to_next=_sixes_to_next(total, weight, next_at)
                if next_at
                else None,
                ones_to_drop=(
                    _ones_to_drop(total, weight, thresholds[predicted - 2])
                    if predicted > 1
                    else None
                ),
                declining=declining,
                proposed=proposed,
            )
        )
    result.sort(key=lambda f: (f.predicted, f.average, f.subject))
    return result


def report_average(forecasts: list[SubjectForecast]) -> float | None:
    """The plain mean of the forecast grades - what the report card average
    would be."""
    if not forecasts:
        return None
    return round(sum(f.predicted for f in forecasts) / len(forecasts), 2)
