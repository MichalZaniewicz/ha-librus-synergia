"""A semester in numbers (semester_summary.py) - pure calculations, no
Home Assistant."""

from __future__ import annotations

from datetime import date

from librus_synergia.models import (
    AttendanceData,
    AttendanceTypeData,
    ClassData,
    GradeCategoryData,
    GradeData,
    HomeworkAssignmentData,
    HomeworkEventData,
    LessonTopicData,
    LibrusData,
    MeData,
    NoteData,
    PlanChangeData,
)

from custom_components.librus_synergia.semester_summary import semester_summary

# Tuesday, in the first semester (1 Sep - 31 Jan).
_TODAY = date(2026, 10, 20)
_MATH, _BIO = 10, 11
_CATS = {
    1: GradeCategoryData(id=1, name="Sprawdzian", count_to_average=True, weight=3),
    2: GradeCategoryData(id=2, name="Aktywność", count_to_average=True, weight=1),
}
_TYPES = {
    1: AttendanceTypeData(id=1, name="Nieobecność", is_presence_kind=False),
    3: AttendanceTypeData(id=3, name="Nieobecność uspr.", is_presence_kind=False),
    2: AttendanceTypeData(id=2, name="Spóźnienie", is_presence_kind=True),
    100: AttendanceTypeData(id=100, name="Obecność", is_presence_kind=True),
}


def _grade(gid: int, value: str, day: str, subject: int, category: int = 2, **kw) -> GradeData:
    return GradeData(
        id=gid,
        value=value,
        category_id=category,
        subject_id=subject,
        semester=kw.pop("semester", 1),
        add_date=f"{day} 10:00:00",
        is_semester_proposition=False,
        is_final_proposition=False,
        **kw,
    )


def _record(aid: int, day: str, type_id: int) -> AttendanceData:
    return AttendanceData(id=aid, lesson_id=None, lesson_no=1, date=day, semester=1, type_id=type_id)


def _data() -> LibrusData:
    return LibrusData(
        me=MeData(account_id=1, first_name="Ola", last_name="Kowalska"),
        grades=[
            # Maths: a 3 on a test early on, then a 4 last week.
            _grade(1, "3", "2026-09-08", _MATH, category=1),
            _grade(2, "4", "2026-10-14", _MATH),
            # Biology: two 6s in one week, a 5+ and a bare "+".
            _grade(3, "6", "2026-09-15", _BIO),
            _grade(4, "6", "2026-09-16", _BIO),
            _grade(5, "5+", "2026-09-16", _BIO, comments=["Brawo"]),
            _grade(6, "+", "2026-10-01", _BIO),
            # A correction of the maths test.
            _grade(7, "5", "2026-10-15", _MATH, category=1, improves_id=1),
            # Left out: a proposition, and a second-semester grade.
            GradeData(
                id=8, value="5", category_id=None, subject_id=_MATH, semester=1,
                add_date="2026-10-18", is_semester_proposition=True, is_final_proposition=False,
            ),
            _grade(9, "1", "2026-10-10", _MATH, semester=2),
        ],
        grade_categories=_CATS,
        notes=[
            NoteData(id=1, text="Pomoc koledze", category_id=None, teacher_id=None, date="2026-09-20", positive=1),
            NoteData(id=2, text="Rozmowy", category_id=None, teacher_id=None, date="2026-10-02", positive=0),
            NoteData(id=3, text="Stara", category_id=None, teacher_id=None, date="2026-06-02", positive=0),
        ],
        attendances=[
            _record(1, "2026-09-07", 1),  # Monday, unexcused
            _record(2, "2026-09-14", 3),  # Monday, excused
            _record(3, "2026-09-16", 2),  # late
            *(_record(10 + i, "2026-09-17", 100) for i in range(7)),
        ],
        attendance_types=_TYPES,
        timetable={},
        homeworks=[
            HomeworkEventData(id=1, date="2026-10-05", content="Ułamki", category_id=5, subject_id=_MATH, time_from=None),
            HomeworkEventData(id=2, date="2026-10-27", content="Komórka", category_id=5, subject_id=_BIO, time_from=None),
            HomeworkEventData(id=3, date="2026-10-28", content="Wycieczka", category_id=6, subject_id=None, time_from=None),
        ],
        homework_categories={5: "Sprawdzian", 6: "Wycieczka"},
        school_notices=[],
        lucky_number=None,
        subjects={_MATH: "Matematyka", _BIO: "Biologia"},
        teachers={},
        classrooms={},
        homework_assignments=[
            HomeworkAssignmentData(id=1, topic="Zad. 1", text="", teacher_id=None, date="2026-10-01", due_date="2026-10-03"),
        ],
        lesson_topics=[
            LessonTopicData(id="a", date="2026-09-02", lesson_no=1, lesson_id=None, topic="Wstęp", is_trip=False, teacher_id=None),
            LessonTopicData(id="b", date="2026-09-03", lesson_no=1, lesson_id=None, topic="Ułamki", is_trip=False, teacher_id=None),
        ],
        plan_changes=[
            PlanChangeData(id=1, original_date="2026-09-10", original_lesson_no=1, original_subject_id=_MATH,
                           original_teacher_id=None, original_classroom=None, cancelled=True, shifted=False),
            PlanChangeData(id=2, original_date="2026-09-11", original_lesson_no=2, original_subject_id=_BIO,
                           original_teacher_id=None, original_classroom=None, cancelled=False, shifted=False,
                           date="2026-09-11", lesson_no=2, subject_id=_MATH),
        ],
        school_class=ClassData(
            number=7, symbol="a", tutor_id=None,
            begin_school_year="2026-09-01", end_first_semester="2027-01-31", end_school_year="2027-06-25",
        ),
    )


def test_first_semester_in_numbers() -> None:
    summary = semester_summary(
        _data(),
        _TODAY,
        achievements=[{"key": "first_six", "title": "Pierwsza szóstka", "date": "2026-09-15"}],
    )
    assert summary is not None
    assert summary["current"] == 1
    assert set(summary["semesters"]) == {"1"}  # semester 2 hasn't started
    s = summary["semesters"]["1"]
    assert s["days"] == 153 and s["days_elapsed"] == 50 and not s["finished"]

    grades = s["grades"]
    assert grades["count"] == 7  # the proposition and semester 2's grade left out
    assert grades["distribution"] == {"1": 0, "2": 0, "3": 1, "4": 1, "5": 2, "6": 2}
    assert grades["sixes"] == 2 and grades["plus"] == 1 and grades["corrections"] == 1
    assert grades["tests"] == 2 and grades["tests_average"] == 4.0
    assert grades["with_comments"] == 1
    # Weighted: (3*3 + 4 + 6 + 6 + 5.5 + 5*3) / 10 = 4.55; 30 days ago only
    # the September grades counted: (9 + 6 + 6 + 5.5) / 6 = 4.42.
    assert grades["average"] == 4.55
    assert grades["average_change"] == 0.13
    assert grades["average_series"][-1] == ["2026-10-20", 4.55]
    assert grades["best_week"] == {"start": "2026-09-14", "average": 5.83, "count": 3}
    assert grades["busiest_day"] == {"date": "2026-09-16", "count": 2}

    assert [x["subject"] for x in s["subjects"]] == ["Biologia", "Matematyka"]

    attendance = s["attendance"]
    assert attendance["records"] == 10 and attendance["percentage"] == 80.0
    assert (attendance["excused"], attendance["unexcused"], attendance["late"]) == (1, 1, 1)
    assert attendance["absences_by_weekday"] == [2, 0, 0, 0, 0]
    assert attendance["last_absence"] == "2026-09-14"

    assert s["lessons"] == {"held": 2, "cancelled": 1, "substitutions": 1}
    assert s["notes"] == {"positive": 1, "negative": 1, "neutral": 0}
    assert s["homework"] == 1
    assert s["exams"] == {"done": 1, "upcoming": 1}
    assert s["badges"] == 1 and s["latest_badge"] == "Pierwsza szóstka"


def test_after_the_year_ends_both_semesters_stay() -> None:
    summary = semester_summary(_data(), date(2027, 7, 10))
    assert summary is not None
    assert summary["current"] == 2
    assert summary["semesters"]["1"]["finished"]
    assert summary["semesters"]["2"]["finished"]
    assert summary["semesters"]["1"]["progress"] == 1.0


def test_nothing_before_the_school_year_or_without_dates() -> None:
    assert semester_summary(_data(), date(2026, 8, 20)) is None
    data = _data()
    data.school_class = None
    assert semester_summary(data, _TODAY) is None
