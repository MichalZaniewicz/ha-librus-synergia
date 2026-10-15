"""Absences left before missing half of a subject's lessons (absence_budget.py)
- pure calculations, no Home Assistant."""

from __future__ import annotations

from datetime import date

from librus_synergia.models import (
    AttendanceData,
    AttendanceTypeData,
    ClassData,
    FreeDayData,
    LessonData,
    LibrusData,
    MeData,
    StandingLessonData,
)

from custom_components.librus_synergia.absence_budget import absence_budget, current_semester

# Tuesday. The (short) first semester ends on Friday 2026-10-30.
_TODAY = date(2026, 10, 20)
_MATH, _RELIGION = 10, 11
# Lessons -> subjects (Attendances point at a lesson, not a subject).
_LESSONS = {7: _MATH, 8: _RELIGION}
_TYPES = {
    1: AttendanceTypeData(id=1, name="Nieobecność", is_presence_kind=False),
    100: AttendanceTypeData(id=100, name="Obecność", is_presence_kind=True),
}
# Maths on Monday, Wednesday and Friday (lesson 1), religion on Thursday (lesson 2).
_PLAN = [
    StandingLessonData(id=1, lesson_id=7, day_of_week=1, lesson_no=1, date_from="2026-09-01", date_to=None, subject_id=_MATH),
    StandingLessonData(id=2, lesson_id=7, day_of_week=3, lesson_no=1, date_from="2026-09-01", date_to=None, subject_id=_MATH),
    StandingLessonData(id=3, lesson_id=7, day_of_week=5, lesson_no=1, date_from="2026-09-01", date_to=None, subject_id=_MATH),
    StandingLessonData(id=4, lesson_id=8, day_of_week=4, lesson_no=2, date_from="2026-09-01", date_to=None, subject_id=_RELIGION),
]


def _record(aid: int, day: str, lesson: int, absent: bool = False) -> AttendanceData:
    return AttendanceData(
        id=aid, lesson_id=lesson, lesson_no=1, date=day, semester=1, type_id=1 if absent else 100
    )


def _lesson(subject_id: int | str, lesson_no: int, cancelled: bool = False) -> LessonData:
    return LessonData(
        lesson_no=lesson_no,
        hour_from="08:00",
        hour_to="08:45",
        subject_id=subject_id,
        teacher_id=None,
        classroom_id=None,
        is_canceled=cancelled,
        is_substitution=False,
    )


def _data(**kw) -> LibrusData:
    records = [
        # Maths: 9 lessons so far this semester, 3 missed.
        *(_record(i, f"2026-09-{10 + i:02d}", 7, absent=i < 3) for i in range(9)),
        # Religion: 2 lessons so far, both missed.
        _record(20, "2026-10-01", 8, absent=True),
        _record(21, "2026-10-08", 8, absent=True),
        # Left out: before the school year, and not yet happened.
        _record(30, "2026-08-31", 7, absent=True),
        _record(31, "2026-10-25", 7, absent=True),
    ]
    return LibrusData(
        me=MeData(account_id=1, first_name="Ola", last_name="Kowalska"),
        grades=[],
        grade_categories={},
        notes=[],
        attendances=kw.pop("attendances", records),
        attendance_types=_TYPES,
        timetable=kw.pop(
            "timetable",
            {
                # The known week: Wednesday's maths is cancelled, Thursday's
                # religion comes with a string id, as Timetables sends it.
                date(2026, 10, 21): [_lesson(_MATH, 1, cancelled=True)],
                date(2026, 10, 22): [_lesson(str(_RELIGION), 2)],
            },
        ),
        homeworks=[],
        school_notices=[],
        lucky_number=None,
        subjects={_MATH: "Matematyka", _RELIGION: "Religia"},
        teachers={},
        classrooms={},
        lesson_subjects=_LESSONS,
        standing_timetable=kw.pop("standing_timetable", _PLAN),
        free_days=kw.pop(
            "free_days",
            [
                # A whole day off on Friday the 23rd, and lessons 2-3 off on
                # Thursday the 29th (religion's slot).
                FreeDayData(id=1, name="Dzień wolny", date_from="2026-10-23", date_to="2026-10-23"),
                FreeDayData(
                    id=2,
                    name="Rekolekcje",
                    date_from="2026-10-29",
                    date_to="2026-10-29",
                    lesson_no_from=2,
                    lesson_no_to=3,
                ),
            ],
        ),
        school_class=ClassData(
            number=7,
            symbol="a",
            tutor_id=None,
            begin_school_year="2026-09-01",
            end_first_semester="2026-10-30",
            end_school_year="2027-06-25",
        ),
        **kw,
    )


def test_budget_counts_held_lessons_and_the_rest_of_the_semester() -> None:
    budget = absence_budget(_data(), _TODAY)
    assert budget is not None
    assert budget.semester == 1
    assert budget.end == date(2026, 10, 30)
    by_name = {b.subject: b for b in budget.subjects}

    math = by_name["Matematyka"]
    # Still to come: Mon 26, Wed 28, Fri 30 (Wed 21 cancelled, Fri 23 free).
    assert (math.held, math.absent, math.remaining) == (9, 3, 3)
    assert (math.expected, math.allowed, math.left) == (12, 6, 3)

    religion = by_name["Religia"]
    # Still to come: Thu 22 only (Thu 29's lesson 2 is freed).
    assert (religion.held, religion.absent, religion.remaining) == (2, 2, 1)
    # 3 lessons in the semester, at most 1 may be missed: already one over.
    assert religion.left == -1

    # Fewest left first.
    assert [b.subject for b in budget.subjects] == ["Religia", "Matematyka"]


def test_no_estimate_without_the_standing_plan() -> None:
    # Only the next two weeks would be counted - far too few lessons.
    assert absence_budget(_data(standing_timetable=[]), _TODAY) is None


def test_second_semester_starts_counting_again() -> None:
    today = date(2026, 11, 3)
    assert current_semester(_data(), today) == (2, date(2026, 10, 31), date(2027, 6, 25))
    budget = absence_budget(_data(), today)
    assert budget is not None
    assert budget.semester == 2
    # Every record is from the first semester.
    assert all(b.held == 0 and b.absent == 0 for b in budget.subjects)
    assert all(b.remaining > 0 for b in budget.subjects)


def test_outside_the_school_year() -> None:
    assert current_semester(_data(), date(2027, 7, 10)) is None
    assert absence_budget(_data(), date(2027, 7, 10)) is None
