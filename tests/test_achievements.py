"""Badges (achievements.py) - pure calculations, no Home Assistant."""

from __future__ import annotations

from datetime import date

from librus_synergia.models import (
    AttendanceData,
    AttendanceTypeData,
    BehaviourGradeData,
    ClassData,
    GradeCategoryData,
    GradeData,
    LibrusData,
    LuckyNumberData,
    MeData,
    NoteData,
)

from custom_components.librus_synergia.achievements import compute_badges, key_title

_TODAY = date(2026, 10, 20)
_THRESHOLDS = (1.75, 2.75, 3.75, 4.75, 5.5)
_CATS = {
    1: GradeCategoryData(id=1, name="Sprawdzian", count_to_average=True, weight=3),
    2: GradeCategoryData(id=2, name="Aktywność", count_to_average=True, weight=1),
}
_TYPES = {
    1: AttendanceTypeData(id=1, name="Nieobecność", is_presence_kind=False),
    2: AttendanceTypeData(id=2, name="Spóźnienie", is_presence_kind=True),
    100: AttendanceTypeData(id=100, name="Obecność", is_presence_kind=True),
}


def _grade(gid: int, value: str, day: str, subject: int = 10, cat: int = 2, **kw) -> GradeData:
    return GradeData(
        id=gid,
        value=value,
        category_id=cat,
        subject_id=subject,
        semester=1,
        add_date=f"{day} 10:00:00",
        is_semester_proposition=False,
        is_final_proposition=False,
        **kw,
    )


def _attendance(aid: int, day: str, type_id: int = 100, lesson: int = 7) -> AttendanceData:
    return AttendanceData(
        id=aid, lesson_id=lesson, lesson_no=1, date=day, semester=1, type_id=type_id
    )


def _note(nid: int, day: str, positive: int) -> NoteData:
    return NoteData(
        id=nid, text="", category_id=None, teacher_id=None, date=day, positive=positive
    )


def _data(**kw) -> LibrusData:
    return LibrusData(
        me=MeData(account_id=1, first_name="Ola", last_name="Kowalska"),
        grades=kw.pop("grades", []),
        grade_categories=_CATS,
        notes=kw.pop("notes", []),
        attendances=kw.pop("attendances", []),
        attendance_types=_TYPES,
        timetable={},
        homeworks=[],
        school_notices=[],
        lucky_number=kw.pop("lucky_number", None),
        subjects={10: "Matematyka", 20: "Historia"},
        teachers={},
        classrooms={},
        school_class=ClassData(
            number=7,
            symbol="a",
            tutor_id=None,
            begin_school_year="2026-09-01",
            end_first_semester="2027-01-31",
            end_school_year="2027-06-25",
        ),
        **kw,
    )


def _badges(data: LibrusData, today: date = _TODAY, **kw):
    return {b.key: b for b in compute_badges(data, today, thresholds=_THRESHOLDS, **kw)}


def test_grade_badges_are_dated_from_history() -> None:
    sixes = [_grade(i, "6", f"2026-09-{i + 10:02d}") for i in range(1, 6)]
    badges = _badges(_data(grades=[_grade(100, "3", "2026-09-02", subject=20), *sixes]))

    assert badges["first_six"].earned == {"first_six": "2026-09-11"}
    # The 5th six earns the first tier on its own date.
    assert badges["sixes"].earned == {"sixes_5": "2026-09-15"}
    assert badges["sixes"].value == 5
    # 3 sixes within 7 days.
    assert badges["hat_trick"].earned == {"hat_trick": "2026-09-13"}
    # A grade below 4 resets the streak; five sixes after it earn tier 5.
    assert badges["good_grade_streak"].earned == {"good_grade_streak_5": "2026-09-15"}
    assert badges["good_grade_streak"].value == 5
    # Five sixes in one subject: average 6.0 reached on the 3rd grade.
    assert badges["subject_star"].earned == {"subject_star": "2026-09-13"}


def test_test_ace_and_comeback() -> None:
    grades = [
        _grade(1, "2", "2026-09-05"),
        _grade(2, "5", "2026-09-12", cat=1),
        _grade(3, "4", "2026-09-19", improves_id=1),
    ]
    badges = _badges(_data(grades=grades))
    assert badges["test_ace"].earned == {"test_ace": "2026-09-12"}
    assert badges["comeback"].earned == {"comeback": "2026-09-19"}


def test_attendance_streak_uses_longest_gap() -> None:
    attendances = [
        _attendance(1, "2026-09-02"),
        # The 9-day gap after the year start earned "7 days".
        _attendance(2, "2026-09-10", type_id=1),
        _attendance(3, "2026-10-15", type_id=1),
    ]
    badges = _badges(_data(attendances=attendances))
    streak = badges["attendance_streak"]
    assert streak.earned == {
        "attendance_streak_7": "2026-09-08",
        "attendance_streak_30": "2026-10-10",
    }
    assert streak.value == 5


def test_full_month_and_punctual() -> None:
    september = [_attendance(i, f"2026-09-{i:02d}") for i in range(1, 31)]
    badges = _badges(_data(attendances=september))
    assert badges["full_month"].earned == {"full_month": "2026-09-30"}
    assert badges["punctual"].earned == {"punctual": "2026-09-30"}

    late = [*september[:29], _attendance(99, "2026-09-30", type_id=2)]
    badges = _badges(_data(attendances=late))
    assert badges["full_month"].earned == {}
    assert badges["punctual"].earned == {}


def test_subject_attendance_needs_twenty_clean_lessons() -> None:
    records = [_attendance(i, f"2026-09-{i:02d}") for i in range(1, 21)]
    badges = _badges(_data(attendances=records, lesson_subjects={7: 10}))
    assert badges["subject_attendance"].earned == {"subject_attendance": "2026-09-20"}

    spoiled = [_attendance(0, "2026-09-01", type_id=1), *records]
    badges = _badges(_data(attendances=spoiled, lesson_subjects={7: 10}))
    assert badges["subject_attendance"].earned == {}


def test_behaviour_badges() -> None:
    notes = [_note(i, f"2026-09-{i + 1:02d}", 1) for i in range(1, 4)]
    behaviour = BehaviourGradeData(
        id=1,
        value=None,
        short_name="",
        semester=1,
        category_id=None,
        teacher_id=None,
        add_date="2026-09-30",
        text="",
        grade_id=1,
    )
    badges = _badges(_data(notes=notes, behaviour_grades=[behaviour]))
    assert badges["praise"].earned == {"praise": "2026-09-02"}
    assert badges["praises"].earned == {"praises_3": "2026-09-04"}
    assert badges["exemplary_behaviour"].earned == {"exemplary_behaviour": "2026-09-30"}
    # No negative note: the gap runs from the school year start.
    assert set(badges["behaviour_streak"].earned) == {
        "behaviour_streak_7",
        "behaviour_streak_30",
    }


def test_lucky_homework_and_year() -> None:
    data = _data(lucky_number=LuckyNumberData(day="2026-10-20", number=12))
    badges = _badges(data, student_number=12, homework_done=11)
    assert badges["lucky"].earned == {"lucky": "2026-10-20"}
    assert badges["homework"].earned == {"homework_10": None}
    assert badges["school_year"].earned == {}
    assert badges["school_year"].value == 49

    badges = _badges(_data(), today=date(2027, 6, 26))
    assert badges["school_year"].earned == {"school_year": "2027-06-25"}


def test_no_ones_after_the_semester() -> None:
    grades = [_grade(1, "4", "2026-10-01")]
    badges = _badges(_data(grades=grades), today=date(2027, 2, 2))
    assert badges["no_ones"].earned == {"no_ones": "2027-01-31"}

    grades.append(_grade(2, "1", "2026-11-01"))
    badges = _badges(_data(grades=grades), today=date(2027, 2, 2))
    assert badges["no_ones"].earned == {}


def test_key_titles() -> None:
    assert key_title("good_grade_streak_10") == "10 dobrych ocen z rzędu"
    assert key_title("sixes_25") == "25 szóstek"
    assert key_title("comeback") == "Comeback"
