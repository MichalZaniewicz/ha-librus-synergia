"""The Test within 24 hours binary sensor."""

from __future__ import annotations

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.librus_synergia.const import DOMAIN

from .conftest import build_mock_client, setup_integration

# Tuesday 2026-10-06, maths at 08:00 (tests run in US/Pacific, UTC-7).
_LESSON = {
    "LessonNo": "1",
    "HourFrom": "08:00",
    "HourTo": "08:45",
    "Subject": {"Id": "42005"},
    "Teacher": {"Id": "1000"},
    "Classroom": {"Id": "2000"},
    "IsCanceled": False,
    "IsSubstitutionClass": False,
}


def _client(homeworks: list[dict]):
    return build_mock_client(
        async_get_subjects={"Subjects": [{"Id": 42005, "Name": "Matematyka"}]},
        async_get_timetable={"Timetable": {"2026-10-06": [[_LESSON]]}},
        async_get_homework_categories={
            "Categories": [{"Id": 1, "Name": "Sprawdzian"}, {"Id": 2, "Name": "Wycieczka"}]
        },
        async_get_homeworks={"HomeWorks": homeworks},
    )


def _entity_id(hass, entry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_exam_soon"
    )
    assert entity_id is not None
    return entity_id


async def _at(hass, freezer, moment: str) -> None:
    freezer.move_to(moment)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_on_from_24_hours_before_the_test_until_it_starts(hass, freezer) -> None:
    # Sunday 12:00 local: the test is 44 hours away.
    freezer.move_to("2026-10-04T19:00:00+00:00")
    entry = await setup_integration(
        hass,
        _client(
            [
                {
                    "Id": 1,
                    "Content": "Dział 5",
                    "Date": "2026-10-06",
                    "Category": {"Id": 1},
                    "Subject": {"Id": 42005},
                },
                # Not a test - never switches it on.
                {
                    "Id": 2,
                    "Content": "Muzeum",
                    "Date": "2026-10-05",
                    "Category": {"Id": 2},
                    "Subject": None,
                },
            ]
        ),
    )
    entity_id = _entity_id(hass, entry)
    assert hass.states.get(entity_id).state == "off"
    assert hass.states.get(entity_id).attributes["exams"] == []

    # Monday 08:00 local: 24 hours before the maths lesson.
    await _at(hass, freezer, "2026-10-05T15:00:02+00:00")
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["exams"] == [
        {
            "start": "2026-10-06T08:00:00-07:00",
            "subject": "Matematyka",
            "kind": "test",
            "category": "Sprawdzian",
            "text": "Dział 5",
        }
    ]
    assert state.attributes["tests"] == 1
    assert state.attributes["quizzes"] == 0

    # Tuesday 08:00 local: the test has started.
    await _at(hass, freezer, "2026-10-06T15:00:02+00:00")
    assert hass.states.get(entity_id).state == "off"


async def test_entry_time_wins_over_the_lesson(hass, freezer) -> None:
    # Monday 13:00 local; the entry says 12:30 on Tuesday - 23.5 hours away,
    # while the 08:00 lesson would already be in the window too.
    freezer.move_to("2026-10-05T20:00:00+00:00")
    entry = await setup_integration(
        hass,
        _client(
            [
                {
                    "Id": 1,
                    "Content": "Kartkówka z ułamków",
                    "Date": "2026-10-06",
                    "Category": {"Id": 2},
                    "Subject": {"Id": 42005},
                    "TimeFrom": "12:30:00",
                }
            ]
        ),
    )
    state = hass.states.get(_entity_id(hass, entry))
    assert state.state == "on"
    assert state.attributes["exams"][0]["start"] == "2026-10-06T12:30:00-07:00"
    assert state.attributes["exams"][0]["kind"] == "quiz"
