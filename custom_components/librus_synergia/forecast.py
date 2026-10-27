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
import re
import unicodedata
import weakref
from collections import OrderedDict
from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, TypeVar

from librus_synergia.models import (
    GradeCategoryData,
    GradeData,
    GradingSystemData,
    LibrusData,
    PointGradeData,
)
from librus_synergia.parsers import parse_grade_value, point_grades_percentage

_T = TypeVar("_T")


class DataMemo:
    """Results of a calculation over one `LibrusData`, per owner.

    Many entities read the same derived figures (forecasts, school days)
    from the same coordinator data - every subject sensor, every minute
    tick - and the data only changes when the coordinator builds a NEW
    `LibrusData` object (it never edits one in place). So results are kept
    per data object (compared with `is`; the object itself is held so a
    recycled `id()` can't match), each under a small key for anything else
    it depends on (today's date, the options).

    `owner` is the config entry the data belongs to: each owner keeps the
    results for its CURRENT data object only - new data replaces the old
    one's results, so an old snapshot isn't kept alive, and students don't
    evict each other (a list of the last few results shared by everyone
    did both once there were more students than entries). A call without
    an owner gets a slot of its own per data object. `size` caps the slots
    overall, least recently used out first.

    Callers share the returned object - they must not modify it."""

    # Results per data object (one per date/options key) - several only
    # when one data object outlives a day or the options differ per caller.
    _KEYS_PER_DATA = 8

    # Every memo, so an unloading entry can drop its slots from all of them
    # at once (`discard_all`).
    _ALL: weakref.WeakSet[DataMemo] = weakref.WeakSet()

    def __init__(self, size: int = 16) -> None:
        self._size = size
        self._slots: OrderedDict[Hashable, tuple[object, dict[Hashable, Any]]] = OrderedDict()
        DataMemo._ALL.add(self)

    def discard(self, owner: Hashable) -> None:
        """Drop `owner`'s slot - its data object and results - right away,
        instead of keeping a removed or reloading entry's last snapshot alive
        until other owners push it out."""
        self._slots.pop(owner, None)

    @classmethod
    def discard_all(cls, owner: Hashable) -> None:
        """`discard(owner)` on every memo (an entry unloading)."""
        for memo in list(cls._ALL):
            memo.discard(owner)

    def get(
        self,
        data: object,
        key: Hashable,
        compute: Callable[[], _T],
        *,
        owner: Hashable | None = None,
    ) -> _T:
        slot = owner if owner is not None else ("data", id(data))
        held = self._slots.get(slot)
        if held is None or held[0] is not data:
            held = (data, {})
            self._slots[slot] = held
        self._slots.move_to_end(slot)
        values = held[1]
        if key in values:
            return values[key]  # type: ignore[no-any-return]
        value = compute()
        if len(values) >= self._KEYS_PER_DATA:
            values.clear()
        values[key] = value
        while len(self._slots) > self._size:
            self._slots.popitem(last=False)
        return value

    def clear(self) -> None:
        self._slots.clear()

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


def parse_point_thresholds(text: str | None) -> tuple[float, ...] | None:
    """Five ascending percentages (for a 2..6) from "40, 55, 75, 90, 98" -
    decimal commas and "%" allowed. Empty (the default) or unusable -> None:
    point-graded subjects then stay out of the forecast, as before the
    option existed, since there is no common scale to fall back to (each
    school, often each subject, sets its own)."""
    if not text:
        return None
    parts = [p for p in text.replace("%", " ").replace(";", " ").replace(", ", " ").split() if p]
    try:
        values = tuple(float(p.replace(",", ".")) for p in parts)
    except ValueError:
        return None
    if len(values) != 5 or any(
        b <= a for a, b in zip(values, values[1:], strict=False)
    ):
        return None
    if values[0] <= 0 or values[-1] > 100:
        return None
    return values


@dataclass(frozen=True, slots=True)
class PointThresholds:
    """Percentages for a 2..6 in point-graded subjects: `default` for every
    subject, `by_subject` ((subject id, thresholds) pairs) overriding it for
    single subjects - teachers often set their own scale. Hashable, so it
    can be part of a DataMemo key."""

    default: tuple[float, ...] | None = None
    by_subject: tuple[tuple[int | str, tuple[float, ...]], ...] = ()

    def for_subject(self, subject_id: int | str | None) -> tuple[float, ...] | None:
        """The thresholds for one subject: its own, else the default."""
        for known, values in self.by_subject:
            if known == subject_id:
                return values
        return self.default

    def __bool__(self) -> bool:
        return bool(self.default or self.by_subject)


PointThresholdsArg = PointThresholds | tuple[float, ...] | None


def as_point_thresholds(value: PointThresholdsArg) -> PointThresholds | None:
    """A plain tuple (the default scale only) as `PointThresholds`; None
    when nothing is set."""
    if not value:
        return None
    if isinstance(value, PointThresholds):
        return value
    return PointThresholds(default=tuple(value))


def _subject_key(name: str) -> str:
    """A subject name for matching: case, Polish letters and spaces
    don't matter ("Język polski" == "jezyk  POLSKI")."""
    folded = unicodedata.normalize("NFKD", name.replace("ł", "l").replace("Ł", "L"))
    plain = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(plain.casefold().split())


def parse_point_thresholds_by_subject(
    text: str | None, subjects: Mapping[int | str, str]
) -> tuple[dict[int | str, tuple[float, ...]], list[str]]:
    """Per-subject thresholds from lines like "fizyka: 30, 50, 70, 85, 96"
    (one subject a line - ";" may separate the values, as in the general
    field), matched to `subjects` by name (see `_subject_key`) or by the
    subject id. Returns the matched subjects and the entries that didn't
    work - an unknown subject or unusable thresholds - so the user can see
    a typo."""
    matched: dict[int | str, tuple[float, ...]] = {}
    unmatched: list[str] = []
    if not text:
        return matched, unmatched
    names = {_subject_key(name): subject_id for subject_id, name in subjects.items()}
    for entry in re.split(r"[\r\n]+", text):
        entry = entry.strip()
        if not entry:
            continue
        name, separator, values = entry.partition(":")
        thresholds = parse_point_thresholds(values) if separator else None
        key = _subject_key(name)
        subject_id = names.get(key)
        if subject_id is None:
            subject_id = next((sid for sid in subjects if str(sid) == name.strip()), None)
        if thresholds is None or subject_id is None:
            unmatched.append(entry)
            continue
        matched[subject_id] = thresholds
    return matched, unmatched


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
    grading: GradingSystemData | None = None,
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
        numeric = parse_grade_value(grade.value, grading)
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
    # Point-graded subject (forecast from `point_grade_thresholds`): its
    # percentage. `average` and `next_grade_at` are then percentages too,
    # and the "how many 6s/1s" counts don't apply (None).
    percentage: float | None = None
    # ... and the weighted points earned / possible behind it, so a card can
    # add one more score to the exact percentage.
    points_earned: float | None = None
    points_possible: float | None = None

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
            "percentage": self.percentage,
            "points_earned": self.points_earned,
            "points_possible": self.points_possible,
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


# Every subject average sensor, the Grade forecast sensor, the grade-risk
# binary sensor and the coordinator ask for the same forecasts each update
# (~20 calls per cycle) - computed once per data object instead.
_FORECASTS = DataMemo()


def subject_forecasts(
    data: LibrusData,
    today: date,
    thresholds: tuple[float, ...],
    *,
    weighted: bool = True,
    owner: Hashable | None = None,
    point_thresholds: PointThresholdsArg = None,
) -> list[SubjectForecast]:
    """One forecast per subject with at least one counted grade, worst
    first. The list is shared between callers - don't modify it. `owner`
    is the config entry id (see DataMemo). With `point_thresholds`
    (percentages for a 2..6, a plain tuple or `PointThresholds` with
    per-subject scales), a subject graded only in points gets a forecast
    from its percentage as well."""
    points = as_point_thresholds(point_thresholds)
    return _FORECASTS.get(
        data,
        (today, tuple(thresholds), weighted, points),
        lambda: _subject_forecasts(
            data, today, tuple(thresholds), weighted=weighted, point_thresholds=points
        ),
        owner=owner,
    )


def _subject_forecasts(
    data: LibrusData,
    today: date,
    thresholds: tuple[float, ...],
    *,
    weighted: bool,
    point_thresholds: PointThresholds | None = None,
) -> list[SubjectForecast]:
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
            grades, data.grade_categories, subject_id=subject_id, weighted=weighted,
            grading=data.grading_system
        )
        if weight <= 0:
            continue
        average = total / weight
        predicted = predicted_grade(average, thresholds)
        old_total, old_weight = average_sums(
            older, data.grade_categories, subject_id=subject_id, weighted=weighted,
            grading=data.grading_system
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
    if point_thresholds:
        result.extend(
            _point_forecasts(
                data,
                today,
                point_thresholds,
                semester=semester,
                proposition=proposition,
                skip={f.subject_id for f in result},
            )
        )
    result.sort(key=lambda f: (f.predicted, f.average, f.subject))
    return result


def _point_forecasts(
    data: LibrusData,
    today: date,
    scales: PointThresholds,
    *,
    semester: int | None,
    proposition: Callable[[GradeData], bool],
    skip: set[int],
) -> list[SubjectForecast]:
    """Forecasts for point-graded subjects: the percentage of points (as
    `point_grades_percentage` counts it - what the grade log shows) against
    the percentage thresholds. Same basis as the 1-6 grades. A subject that
    already has a forecast from 1-6 grades (`skip`) keeps that one - the two
    scales aren't mixed."""
    grades = [
        g
        for g in data.point_grades
        if g.subject_id is not None
        and g.subject_id not in skip
        and (semester is None or g.semester in (semester, None))
    ]
    cutoff = (today - timedelta(days=DECLINE_WINDOW_DAYS)).isoformat()
    by_subject: dict[int, list[PointGradeData]] = {}
    for grade in grades:
        by_subject.setdefault(grade.subject_id, []).append(grade)  # type: ignore[arg-type]
    result: list[SubjectForecast] = []
    for subject_id, subject_grades in by_subject.items():
        thresholds = scales.for_subject(subject_id)
        percentage = point_grades_percentage(subject_grades)
        if thresholds is None or percentage is None:
            # No scale for this subject (no default, no own one): left out.
            continue
        predicted = predicted_grade(percentage, thresholds)
        older = point_grades_percentage(
            [g for g in subject_grades if (g.add_date or "")[:10] < cutoff]
        )
        declining = older is not None and predicted < predicted_grade(older, thresholds)
        result.append(
            SubjectForecast(
                subject_id=subject_id,
                subject=data.subjects.get(subject_id, str(subject_id)),
                average=percentage,
                weight_total=0.0,
                predicted=predicted,
                next_grade_at=thresholds[predicted - 1] if predicted < 6 else None,
                sixes_to_next=None,
                ones_to_drop=None,
                declining=declining,
                proposed=next(
                    (
                        g.value
                        for g in data.grades
                        if g.subject_id == subject_id and proposition(g)
                    ),
                    None,
                ),
                percentage=percentage,
                points_earned=(sums := point_sums(subject_grades))[0],
                points_possible=sums[1],
            )
        )
    return result


def point_sums(grades: list[PointGradeData]) -> tuple[float, float]:
    """(earned, possible) weighted points - the sums `point_grades_percentage`
    divides (counted categories with a known maximum, weight 1 when
    unknown)."""
    earned = possible = 0.0
    for grade in grades:
        if not grade.counts_to_average or grade.points is None or not grade.max_points:
            continue
        weight = grade.weight if grade.weight else 1.0
        earned += grade.points * weight
        possible += grade.max_points * weight
    return round(earned, 2), round(possible, 2)


def point_grade_value(grade: PointGradeData, scales: PointThresholdsArg) -> int | None:
    """One point grade as a 1-6 grade on its subject's percentage scale - the
    thresholds the forecast uses. None without a scale for the subject (no
    `point_grade_thresholds`) or without a known maximum."""
    resolved = as_point_thresholds(scales)
    thresholds = resolved.for_subject(grade.subject_id) if resolved else None
    percentage = grade.percentage
    if thresholds is None or percentage is None:
        return None
    return predicted_grade(percentage, thresholds)


def overall_with_points(
    overall: float | None, data: LibrusData, forecasts: list[SubjectForecast]
) -> float | None:
    """The 1-6 grade average with the point-graded subjects added: each one
    at its forecast grade, weighted by its number of point grades against the
    number of 1-6 grades behind `overall` - what the Hero card shows since
    point grades count there. `overall` unchanged without point-graded
    forecasts (no `point_grade_thresholds`)."""
    total = count = 0.0
    for forecast in forecasts:
        if forecast.percentage is None:
            continue
        grades = sum(
            1
            for g in data.point_grades
            if g.subject_id == forecast.subject_id and g.percentage is not None
        )
        total += forecast.predicted * grades
        count += grades
    if not count:
        return overall
    graded = sum(
        1
        for g in data.grades
        if not g.is_semester_proposition
        and not g.is_final_proposition
        and not g.is_semester
        and not g.is_final
        and parse_grade_value(g.value, data.grading_system) is not None
    )
    if overall is not None and graded:
        total += overall * graded
        count += graded
    return round(total / count, 2)


def report_average(forecasts: list[SubjectForecast]) -> float | None:
    """The plain mean of the forecast grades - what the report card average
    would be."""
    if not forecasts:
        return None
    return round(sum(f.predicted for f in forecasts) / len(forecasts), 2)
