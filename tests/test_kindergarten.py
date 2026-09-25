"""Tests for kindergarten timetable support."""

from __future__ import annotations

from datetime import date

from custom_components.librus_synergia.coordinator import (
    _parse_kindergarten_classrooms,
    _parse_kindergarten_group,
    _parse_kindergarten_teachers,
    merge_kindergarten_timetables,
)


def test_merge_kindergarten_timetable_entry() -> None:
    payload = {
        "timetableEntries": [
            {
                "identifier": "L1",
                "activityTypeIdentifier": "ACT1",
                "classroomIdentifier": "ROOM1",
                "type": "planned",
                "date": "2026-09-28",
                "startTime": "07:00",
                "endTime": "09:00",
                "teachers": ["TEACHER1"],
            }
        ]
    }

    merged = merge_kindergarten_timetables(payload)

    assert list(merged) == [date(2026, 9, 28)]
    lesson = merged[date(2026, 9, 28)][0]
    assert lesson.hour_from == "07:00"
    assert lesson.hour_to == "09:00"
    assert lesson.subject_id == "ACT1"
    assert lesson.classroom_id == "ROOM1"
    assert lesson.teacher_id == "TEACHER1"
    assert lesson.teacher_ids == ("TEACHER1",)
    assert lesson.is_canceled is False
    assert lesson.is_substitution is False


def test_parse_kindergarten_group_into_existing_class_model() -> None:
    group = _parse_kindergarten_group(
        {
            "identifier": "GROUP1",
            "name": "0B",
            "tutors": ["TEACHER1"],
        }
    )

    assert group is not None
    assert group.symbol == "0B"
    assert group.tutor_id == "TEACHER1"
    assert group.number is None


def test_parse_kindergarten_teachers_uses_account_id() -> None:
    teachers = _parse_kindergarten_teachers(
        {
            "Users": [
                {
                    "AccountId": "TEACHER1",
                    "FirstName": "Magdalena",
                    "LastName": "Gigiel-Wanionek",
                }
            ]
        }
    )

    assert teachers == {"TEACHER1": "Magdalena Gigiel-Wanionek"}


def test_parse_kindergarten_classrooms() -> None:
    classrooms = _parse_kindergarten_classrooms(
        {
            "data": [
                {
                    "identifier": "ROOM1",
                    "symbol": "1",
                    "name": "sala 1",
                }
            ]
        }
    )

    assert classrooms == {"ROOM1": "1"}
