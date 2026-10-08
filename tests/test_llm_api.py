"""Tests for the "Librus Synergia" LLM API (tools for conversation agents)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util

from custom_components.librus_synergia.llm_api import LLM_API_ID

from .conftest import build_mock_client, setup_integration

_FROZEN = "2026-09-09T12:00:00+00:00"  # ~05:00 US/Pacific, clear of midnight

_SUBJECTS = {
    "Subjects": [
        {"Id": 100, "Name": "Matematyka"},
        {"Id": 200, "Name": "Język polski"},
    ]
}
_TEACHERS = {"Users": [{"Id": 500, "FirstName": "Anna", "LastName": "Nowak"}]}


def _context() -> llm.LLMContext:
    return llm.LLMContext(
        platform="test", context=None, language="pl", assistant="conversation", device_id=None
    )


async def _call(hass, tool_name: str, **args):
    instance = await llm.async_get_api(hass, LLM_API_ID, _context())
    tool = next(t for t in instance.tools if t.name == tool_name)
    result = await tool.async_call(
        hass, llm.ToolInput(tool_name=tool_name, tool_args=args), _context()
    )
    # Newer Home Assistant wraps the payload in ToolResult.
    return getattr(result, "data", result)


async def test_api_registered_with_read_only_tools(hass) -> None:
    await setup_integration(hass, build_mock_client())

    assert LLM_API_ID in {api.id for api in llm.async_get_apis(hass)}
    instance = await llm.async_get_api(hass, LLM_API_ID, _context())
    assert {t.name for t in instance.tools} == {
        "librus_get_timetable",
        "librus_get_grades",
        "librus_get_upcoming",
        "librus_get_lesson_topics",
        "librus_get_attendance",
        "librus_get_behaviour",
        "librus_get_school_info",
        "librus_get_messages",
    }
    assert "Librus Synergia" in instance.api_prompt


async def test_timetable_tool_returns_todays_lessons(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    today = dt_util.now().date().isoformat()
    lesson = {
        "LessonNo": "1",
        "HourFrom": "08:00",
        "HourTo": "08:45",
        "Subject": {"Id": "100"},
        "Teacher": {"Id": "500"},
        "Classroom": {"Id": "12"},
        "IsCanceled": False,
        "IsSubstitutionClass": True,
    }
    client = build_mock_client(
        async_get_timetable={"Timetable": {today: [[lesson]]}},
        async_get_subjects=_SUBJECTS,
        async_get_teachers=_TEACHERS,
        async_get_classrooms={"Classrooms": [{"Id": 12, "Name": "sala 12"}]},
    )
    await setup_integration(hass, client)

    data = await _call(hass, "librus_get_timetable")
    (day,) = data["days"]
    assert day["date"].startswith(today)
    assert day["lessons"] == [
        {
            "no": 1,
            "from": "08:00",
            "to": "08:45",
            "subject": "Matematyka",
            "teacher": "Anna Nowak",
            "room": "sala 12",
            "substitution": True,
        }
    ]


async def test_grades_tool_filters_by_subject_and_marks_corrections(hass) -> None:
    client = build_mock_client(
        async_get_subjects=_SUBJECTS,
        async_get_teachers=_TEACHERS,
        async_get_grades={
            "Grades": [
                {"Id": 1, "Grade": "2", "Subject": {"Id": 100}, "AddDate": "2026-09-01",
                 "Category": {"Id": 10}, "AddedBy": {"Id": 500}},
                {"Id": 2, "Grade": "5", "Subject": {"Id": 100}, "AddDate": "2026-09-08",
                 "Category": {"Id": 10}, "Improvement": {"Id": 1}},
                {"Id": 3, "Grade": "4", "Subject": {"Id": 200}, "AddDate": "2026-09-05",
                 "Category": {"Id": 10}},
            ]
        },
        async_get_grade_categories={
            "Categories": [{"Id": 10, "Name": "sprawdzian", "CountToTheAverage": True, "Weight": 3}]
        },
    )
    await setup_integration(hass, client)

    data = await _call(hass, "librus_get_grades", subject="matem")
    assert [g["value"] for g in data["grades"]] == ["5", "2"]
    newest, oldest = data["grades"]
    assert newest["corrects_grade"] == "2"
    assert newest["category"] == "sprawdzian"
    assert oldest["was_corrected"] is True
    assert oldest["teacher"] == "Anna Nowak"
    assert set(data["subject_averages"]) == {"Matematyka"}
    assert "overall_average" not in data  # a subject filter leaves it out
    # (2*3 + 5*3) / 6 = 3.5 -> 3 on the default thresholds.
    assert data["forecast"] == [
        {
            "subject": "Matematyka",
            "forecast_grade": 3,
            "sixes_needed_for_next_grade": 1,
            "ones_until_grade_drops": 3,
        }
    ]


def _shifted(days: int) -> str:
    return (dt_util.now().date() + timedelta(days=days)).isoformat()


def _topics_client(**extra):
    return build_mock_client(
        async_get_subjects=_SUBJECTS,
        async_get_lessons={"Lessons": [{"Id": 501, "Subject": {"Id": 100}}, {"Id": 502, "Subject": {"Id": 200}}]},
        async_get_realizations={
            "Realizations": [
                {"Id": "t1", "Lesson": {"Id": 501}, "LessonNo": 1, "Date": _shifted(-3), "Topic": "Ułamki"},
                {"Id": "t2", "Lesson": {"Id": 502}, "LessonNo": 2, "Date": _shifted(-2), "Topic": "Lektura"},
                {"Id": "t3", "Lesson": {"Id": 501}, "LessonNo": 3, "Date": _shifted(-1), "Topic": "Procenty"},
            ]
        },
        async_get_attendances={
            "Attendances": [{"Id": 1, "Date": _shifted(-1), "LessonNo": "3", "Semester": 1, "Type": {"Id": 1}}]
        },
        async_get_attendance_types={"Types": [{"Id": 1, "Name": "Nieobecność", "IsPresenceKind": False}]},
        **extra,
    )


async def test_lesson_topics_tool_marks_missed_lessons(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    await setup_integration(hass, _topics_client())

    data = await _call(hass, "librus_get_lesson_topics", subject="matem")
    assert [(l["topic"], l.get("student_was_absent")) for l in data["lessons"]] == [
        ("Ułamki", None),
        ("Procenty", True),
    ]
    assert data["missed_by_student"] == 1


async def test_upcoming_tool_lists_topics_to_revise(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    client = _topics_client(
        async_get_homework_categories={"Categories": [{"Id": 1, "Name": "Sprawdzian"}]},
        async_get_homeworks={
            "HomeWorks": [
                {"Id": 7, "Content": "Dział 2", "Date": _shifted(4), "Category": {"Id": 1}, "Subject": {"Id": 100}}
            ]
        },
    )
    await setup_integration(hass, client)

    data = await _call(hass, "librus_get_upcoming")
    (test,) = data["agenda"]
    assert test["is_test"] is True
    assert [t["topic"] for t in test["topics_to_revise"]] == ["Ułamki", "Procenty"]
    assert test["topics_to_revise"][1]["student_was_absent"] is True


async def test_unknown_student_is_an_error(hass) -> None:
    await setup_integration(hass, build_mock_client())

    data = await _call(hass, "librus_get_school_info", student="Zuzia")
    assert "error" in data
    assert data["students"]


async def test_api_unregistered_with_last_entry(hass) -> None:
    entry = await setup_integration(hass, build_mock_client())
    assert LLM_API_ID in {api.id for api in llm.async_get_apis(hass)}

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert LLM_API_ID not in {api.id for api in llm.async_get_apis(hass)}
