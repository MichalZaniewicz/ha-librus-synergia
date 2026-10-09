"""Grade forecast (forecast.py) - pure calculations, no Home Assistant."""

from __future__ import annotations

from datetime import date

from librus_synergia.models import (
    ClassData,
    GradeCategoryData,
    GradeData,
    LibrusData,
    MeData,
)

from custom_components.librus_synergia.forecast import (
    BASIS_SCHOOL_YEAR,
    BASIS_SEMESTER_1,
    forecast_basis,
    parse_thresholds,
    predicted_grade,
    report_average,
    subject_forecasts,
)

_TODAY = date(2026, 10, 20)
_THRESHOLDS = (1.75, 2.75, 3.75, 4.75, 5.5)
_CATS = {
    1: GradeCategoryData(id=1, name="Sprawdzian", count_to_average=True, weight=3),
    2: GradeCategoryData(id=2, name="Aktywność", count_to_average=True, weight=1),
    3: GradeCategoryData(id=3, name="Bez wagi", count_to_average=False, weight=1),
}


def _grade(
    gid: int, value: str, subject: int, cat: int = 2, day: str = "2026-09-15", **kw
):
    return GradeData(
        id=gid,
        value=value,
        category_id=cat,
        subject_id=subject,
        semester=kw.pop("semester", 1),
        add_date=f"{day} 10:00:00",
        is_semester_proposition=kw.pop("proposition", False),
        is_final_proposition=False,
        **kw,
    )


def _data(grades: list[GradeData], end_first: str = "2027-01-31") -> LibrusData:
    return LibrusData(
        me=MeData(account_id=1, first_name="Ola", last_name="Kowalska"),
        grades=grades,
        grade_categories=_CATS,
        notes=[],
        attendances=[],
        attendance_types={},
        timetable={},
        homeworks=[],
        school_notices=[],
        lucky_number=None,
        subjects={10: "Matematyka", 20: "Historia"},
        teachers={},
        classrooms={},
        school_class=ClassData(
            number=7,
            symbol="a",
            tutor_id=None,
            begin_school_year="2026-09-01",
            end_first_semester=end_first,
            end_school_year="2027-06-25",
        ),
    )


def test_parse_thresholds() -> None:
    assert parse_thresholds("1.6, 2.6, 3.6, 4.6, 5.3") == (1.6, 2.6, 3.6, 4.6, 5.3)
    assert parse_thresholds("1,6; 2,6; 3,6; 4,6; 5,3") == (1.6, 2.6, 3.6, 4.6, 5.3)
    # Unusable -> default.
    assert parse_thresholds("abc") == _THRESHOLDS
    assert parse_thresholds("2, 1, 3, 4, 5") == _THRESHOLDS
    assert parse_thresholds("1.5, 2.5") == _THRESHOLDS
    assert parse_thresholds(None) == _THRESHOLDS


def test_predicted_grade_boundaries() -> None:
    assert predicted_grade(1.74, _THRESHOLDS) == 1
    assert predicted_grade(1.75, _THRESHOLDS) == 2
    assert predicted_grade(4.74, _THRESHOLDS) == 4
    assert predicted_grade(4.75, _THRESHOLDS) == 5
    assert predicted_grade(6.0, _THRESHOLDS) == 6


def test_weighted_forecast_and_what_it_takes() -> None:
    # Matematyka: 5 (weight 3) + 3 (weight 1) = 18/4 = 4.5 -> 4.
    data = _data([_grade(1, "5", 10, cat=1), _grade(2, "3", 10)])
    (math,) = subject_forecasts(data, _TODAY, _THRESHOLDS)
    assert math.average == 4.5
    assert math.predicted == 4
    assert math.next_grade_at == 4.75
    # (18 + 6n) / (4 + n) >= 4.75 -> n >= 0.8 -> one 6.
    assert math.sixes_to_next == 1
    # (18 + n) / (4 + n) < 3.75 -> n > 1.11 -> two 1s.
    assert math.ones_to_drop == 2
    assert not math.at_risk
    # Arithmetic: (5 + 3) / 2 = 4.0.
    (plain,) = subject_forecasts(data, _TODAY, _THRESHOLDS, weighted=False)
    assert plain.average == 4.0


def test_non_counting_and_non_numeric_grades_are_ignored() -> None:
    data = _data(
        [
            _grade(1, "2", 20),
            _grade(2, "6", 20, cat=3),
            _grade(3, "np", 20),
            _grade(4, "+", 20),
        ]
    )
    (history,) = subject_forecasts(data, _TODAY, _THRESHOLDS)
    assert history.average == 2.0
    assert history.predicted == 2


def test_at_risk_declining_and_order() -> None:
    data = _data(
        [
            # Historia: a 3 a month ago, then two 1s this week -> 1.67, at risk;
            # without the recent ones it was a 3 -> declining.
            _grade(1, "3", 20, day="2026-09-20"),
            _grade(2, "1", 20, day="2026-10-18"),
            _grade(3, "1", 20, day="2026-10-19"),
            _grade(4, "6", 10),
        ]
    )
    forecasts = subject_forecasts(data, _TODAY, _THRESHOLDS)
    assert [f.subject for f in forecasts] == ["Historia", "Matematyka"]
    history = forecasts[0]
    assert history.at_risk
    assert history.declining
    assert history.ones_to_drop is None
    assert forecasts[1].predicted == 6
    assert forecasts[1].sixes_to_next is None
    assert report_average(forecasts) == 3.5


def test_basis_switches_to_the_school_year_in_semester_two() -> None:
    grades = [
        _grade(1, "2", 10, semester=1),
        _grade(2, "6", 10, semester=2, day="2027-02-10"),
        _grade(3, "4", 10, proposition=True),
    ]
    first = _data(grades)
    assert forecast_basis(first, _TODAY)[0] == BASIS_SEMESTER_1
    (math,) = subject_forecasts(first, _TODAY, _THRESHOLDS)
    assert math.average == 2.0
    assert math.proposed == "4"

    spring = date(2027, 3, 1)
    assert forecast_basis(first, spring)[0] == BASIS_SCHOOL_YEAR
    (math,) = subject_forecasts(first, spring, _THRESHOLDS)
    assert math.average == 4.0
    # The semester proposition doesn't count as the year-end one.
    assert math.proposed is None


def test_no_semester_dates_uses_latest_grade_semester() -> None:
    data = _data([_grade(1, "5", 10, semester=2)], end_first="")
    assert forecast_basis(data, _TODAY)[0] == BASIS_SCHOOL_YEAR


def test_forecasts_are_worked_out_once_per_data_object() -> None:
    """~20 entities ask for the same forecasts each update; one calculation
    per data object, day, thresholds and mode."""
    data = _data([_grade(1, "5", 10, cat=1), _grade(2, "3", 10)])
    first = subject_forecasts(data, _TODAY, _THRESHOLDS)
    assert subject_forecasts(data, _TODAY, _THRESHOLDS) is first
    # Thresholds as a list (any sequence) hit the same entry.
    assert subject_forecasts(data, _TODAY, list(_THRESHOLDS)) is first
    # Another mode, day or thresholds is a result of its own.
    assert subject_forecasts(data, _TODAY, _THRESHOLDS, weighted=False)[0].average == 4.0
    assert subject_forecasts(data, date(2026, 10, 21), _THRESHOLDS) is not first
    assert subject_forecasts(data, _TODAY, (1.5, 2.5, 3.5, 4.5, 5.0))[0].predicted == 5
    # A new data object - the coordinator's next refresh - gets its own
    # result (the memo holds the objects and compares them with `is`).
    changed = _data([_grade(1, "2", 10)])
    assert subject_forecasts(changed, _TODAY, _THRESHOLDS)[0].average == 2.0
    assert subject_forecasts(data, _TODAY, _THRESHOLDS)[0].average == 4.5
