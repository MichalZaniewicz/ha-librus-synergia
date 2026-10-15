"""Tests in a week of the Agenda (exam_prep.week_load) - pure, no Home Assistant."""

from __future__ import annotations

from datetime import date

from librus_synergia.models import HomeworkEventData, LibrusData, MeData

from custom_components.librus_synergia.exam_prep import week_load

_MONDAY = date(2026, 10, 19)


def _entry(entry_id: int, day: str, category: int, subject: int = 10, content: str = "") -> HomeworkEventData:
    return HomeworkEventData(
        id=entry_id,
        content=content,
        date=day,
        category_id=category,
        subject_id=subject,
        time_from=None,
    )


def _data(homeworks: list[HomeworkEventData]) -> LibrusData:
    return LibrusData(
        me=MeData(account_id=1, first_name="Ola", last_name="Kowalska"),
        grades=[],
        grade_categories={},
        notes=[],
        attendances=[],
        attendance_types={},
        timetable={},
        homeworks=homeworks,
        school_notices=[],
        lucky_number=None,
        subjects={10: "Matematyka", 11: "Biologia"},
        teachers={},
        classrooms={},
        homework_categories={1: "Sprawdzian", 2: "Kartkówka", 3: "Inne", 4: "Wycieczka"},
    )


def test_counts_tests_and_quizzes_in_the_week() -> None:
    data = _data(
        [
            _entry(1, "2026-10-19", 1),
            _entry(2, "2026-10-21", 2, subject=11),
            # Filed under "Inne", named in the text (issue #1).
            _entry(3, "2026-10-23", 3, content="Kartkówka z ułamków"),
            # Not a test, and outside the week.
            _entry(4, "2026-10-22", 4),
            _entry(5, "2026-10-26", 1),
        ]
    )
    load = week_load(data, _MONDAY)
    assert (load["from"], load["to"]) == ("2026-10-19", "2026-10-25")
    assert (load["tests"], load["quizzes"], load["total"]) == (1, 2, 3)
    assert [(i["date"], i["subject"], i["kind"]) for i in load["items"]] == [
        ("2026-10-19", "Matematyka", "test"),
        ("2026-10-21", "Biologia", "quiz"),
        ("2026-10-23", "Matematyka", "quiz"),
    ]


def test_empty_week() -> None:
    load = week_load(_data([]), _MONDAY)
    assert load["total"] == 0 and load["items"] == []
