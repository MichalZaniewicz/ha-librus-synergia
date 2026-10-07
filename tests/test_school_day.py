"""School-day binary sensors, School start/end sensors, homework to-do list
and the grade-average history."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.librus_synergia.average_history import (
    LibrusAverageHistory,
    daily_averages,
)
from custom_components.librus_synergia.const import DOMAIN
from librus_synergia.parsers import parse_grades

from .conftest import build_mock_client, setup_integration

# ~05:00 US/Pacific (the harness's time zone): lessons an hour or two ahead
# stay on the same local date.
_FROZEN = "2026-09-09T12:00:00+00:00"


def _entity_id(hass, entry, domain: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{key}")


def _hhmm(offset_minutes: int) -> str:
    return (dt_util.now() + timedelta(minutes=offset_minutes)).strftime("%H:%M")


def _lesson(no: int, start: str, end: str, **flags) -> dict:
    return {
        "LessonNo": str(no),
        "HourFrom": start,
        "HourTo": end,
        "Subject": {"Id": "100"},
        "Teacher": {"Id": "500"},
        "Classroom": {"Id": "12"},
        "IsCanceled": flags.get("canceled", False),
        "IsSubstitutionClass": False,
    }


def _timetable() -> dict:
    today = dt_util.now().date()
    tomorrow = today + timedelta(days=1)
    return {
        "Timetable": {
            today.isoformat(): [
                [_lesson(1, _hhmm(60), _hhmm(105))],
                [_lesson(2, _hhmm(115), _hhmm(160))],
                [_lesson(3, _hhmm(170), _hhmm(215), canceled=True)],
            ],
            tomorrow.isoformat(): [[], [_lesson(2, "08:55", "09:40")]],
        }
    }


async def test_school_day_sensors_follow_the_clock(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    client = build_mock_client(
        async_get_timetable=_timetable(),
        async_get_subjects={"Subjects": [{"Id": 100, "Name": "Matematyka"}]},
    )
    entry = await setup_integration(hass, client)
    today = dt_util.now().date()

    assert hass.states.get(_entity_id(hass, entry, "binary_sensor", "school_day_today")).state == "on"
    assert hass.states.get(_entity_id(hass, entry, "binary_sensor", "school_day_tomorrow")).state == "on"
    in_school = _entity_id(hass, entry, "binary_sensor", "in_school")
    assert hass.states.get(in_school).state == "off"

    start = hass.states.get(_entity_id(hass, entry, "sensor", "school_start"))
    assert dt_util.parse_datetime(start.state).strftime("%H:%M") == _hhmm(60)
    assert start.attributes["is_today"] is True
    assert start.attributes["subject"] == "Matematyka"
    end = hass.states.get(_entity_id(hass, entry, "sensor", "school_end"))
    # Lesson 3 is cancelled, so school ends with lesson 2.
    assert dt_util.parse_datetime(end.state).strftime("%H:%M") == _hhmm(160)

    # During the break between lessons 1 and 2: at school, and the next
    # start is tomorrow's first lesson (lesson 2, 08:55).
    freezer.tick(timedelta(minutes=108))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(in_school).state == "on"
    start = hass.states.get(_entity_id(hass, entry, "sensor", "school_start"))
    assert start.attributes["date"] == (today + timedelta(days=1)).isoformat()
    assert start.attributes["lesson_no"] == 2
    assert dt_util.parse_datetime(start.state).strftime("%H:%M") == "08:55"


async def test_free_day_is_not_a_school_day(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    tomorrow = (dt_util.now().date() + timedelta(days=1)).isoformat()
    client = build_mock_client(
        async_get_timetable=_timetable(),
        async_get_school_free_days={
            "SchoolFreeDays": [{"Id": 1, "Name": "Dzień Edukacji", "DateFrom": tomorrow, "DateTo": tomorrow}]
        },
    )
    entry = await setup_integration(hass, client)

    assert hass.states.get(_entity_id(hass, entry, "binary_sensor", "school_day_tomorrow")).state == "off"


async def test_homework_todo_list_keeps_ticks(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    due = (dt_util.now().date() + timedelta(days=3)).isoformat()
    client = build_mock_client(
        async_get_homework_assignments={
            "HomeWorkAssignments": [
                {"Id": 7, "Topic": "Zadanie 5", "Text": "Strona 42", "Teacher": {"Id": 200},
                 "Date": "2026-09-08", "DueDate": due},
                {"Id": 8, "Topic": "Wypracowanie", "Text": "", "Teacher": {"Id": 200},
                 "Date": "2026-09-08", "DueDate": due},
            ]
        },
    )
    entry = await setup_integration(hass, client)
    todo_id = _entity_id(hass, entry, "todo", "homework")
    assert hass.states.get(todo_id).state == "2"

    result = await hass.services.async_call(
        "todo", "get_items", {}, target={"entity_id": todo_id}, blocking=True, return_response=True
    )
    items = result[todo_id]["items"]
    assert {i["summary"] for i in items} == {"Zadanie 5", "Wypracowanie"}
    assert items[0]["due"] == due

    await hass.services.async_call(
        "todo",
        "update_item",
        {"item": "Zadanie 5", "status": "completed"},
        target={"entity_id": todo_id},
        blocking=True,
    )
    assert hass.states.get(todo_id).state == "1"

    # The tick survives a reload (it is stored in Home Assistant).
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(todo_id).state == "1"


def test_daily_averages_step_with_new_grades() -> None:
    grades = parse_grades(
        {
            "Grades": [
                {"Id": 1, "Grade": "4", "Subject": {"Id": 1}, "AddDate": "2026-09-02 10:00:00"},
                {"Id": 2, "Grade": "6", "Subject": {"Id": 1}, "AddDate": "2026-09-04 10:00:00"},
                {"Id": 3, "Grade": "2", "Subject": {"Id": 2}, "AddDate": "2026-09-03 10:00:00"},
            ]
        }
    )
    points = daily_averages(grades, {}, date(2026, 9, 5), subject_id=1)
    assert points == [
        (date(2026, 9, 2), 4.0),
        (date(2026, 9, 3), 4.0),
        (date(2026, 9, 4), 5.0),
        (date(2026, 9, 5), 5.0),
    ]
    overall = dict(daily_averages(grades, {}, date(2026, 9, 5)))
    assert overall[date(2026, 9, 3)] == 3.0


async def test_average_history_writes_statistics(hass) -> None:
    client = build_mock_client(
        async_get_subjects={"Subjects": [{"Id": 100, "Name": "Matematyka"}]},
        async_get_grades={
            "Grades": [{"Id": 1, "Grade": "5", "Subject": {"Id": 100}, "AddDate": "2026-09-02 10:00:00"}]
        },
    )
    entry = await setup_integration(hass, client)
    hass.config.components.add("recorder")
    with patch(
        "homeassistant.components.recorder.statistics.async_add_external_statistics"
    ) as add:
        history = LibrusAverageHistory(hass, entry.runtime_data)
        history.async_start()
        history.async_stop()
    ids = {call.args[1]["statistic_id"] for call in add.call_args_list}
    prefix = f"{DOMAIN}:{entry.entry_id.lower()}_average"
    assert ids == {prefix, f"{prefix}_100"}
    meta = add.call_args_list[0].args[1]
    assert meta["source"] == DOMAIN
    assert "Matematyka" in add.call_args_list[1].args[1]["name"]
    rows = add.call_args_list[1].args[2]
    assert rows[0]["mean"] == 5.0
