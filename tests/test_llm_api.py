"""Tests for the "Librus Synergia" LLM API (tools for conversation agents)."""

from __future__ import annotations

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
