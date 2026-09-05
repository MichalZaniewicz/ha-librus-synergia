"""Tests for the Librus Synergia calendar platform."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.librus_synergia.const import DOMAIN

from .conftest import build_mock_client, setup_integration


def _entity_id(hass, entry, key: str) -> str | None:
    registry = er.async_get(hass)
    return registry.async_get_entity_id("calendar", DOMAIN, f"{entry.entry_id}_{key}")


# Lessons/homework are dated "tomorrow" throughout so `event.end >= now`
# checks in calendar.py always pick them up as "upcoming", regardless of
# what time of day the suite happens to run.
_TOMORROW = (dt_util.now().date() + timedelta(days=1)).isoformat()


async def test_all_calendars_are_created(hass) -> None:
    client = build_mock_client()
    entry = await setup_integration(hass, client)

    assert _entity_id(hass, entry, "timetable") is not None
    assert _entity_id(hass, entry, "agenda") is not None
    assert _entity_id(hass, entry, "free_days") is not None


async def test_agenda_calendar_next_event_from_homeworks(hass) -> None:
    client = build_mock_client(
        async_get_homeworks={
            "HomeWorks": [
                {
                    "Id": 1,
                    "Content": "Zebranie z rodzicami",
                    "Date": _TOMORROW,
                    "Category": {"Id": 1},
                    "Subject": None,
                }
            ]
        },
    )
    entry = await setup_integration(hass, client)

    entity_id = _entity_id(hass, entry, "agenda")
    state = hass.states.get(entity_id)
    assert state.state == "off"  # tomorrow, not happening right now
    assert "Zebranie z rodzicami" in state.attributes["message"]


async def test_agenda_event_prefixed_with_category_name(hass) -> None:
    """CONFIRMED live: HomeWorks/Categories resolves category ids to names
    like "Sprawdzian" - the agenda summary should surface that up front."""
    client = build_mock_client(
        async_get_homeworks={
            "HomeWorks": [
                {
                    "Id": 1,
                    "Content": "Dział 5",
                    "Date": _TOMORROW,
                    "Category": {"Id": 9368},
                    "Subject": {"Id": 42005},
                }
            ]
        },
        async_get_subjects={"Subjects": [{"Id": 42005, "Name": "Matematyka"}]},
        async_get_homework_categories={"Categories": [{"Id": 9368, "Name": "Sprawdzian"}]},
    )
    entry = await setup_integration(hass, client)

    state = hass.states.get(_entity_id(hass, entry, "agenda"))
    assert state.attributes["message"].startswith("[Sprawdzian]")
    assert "Matematyka" in state.attributes["message"]


async def test_free_days_calendar_event(hass) -> None:
    client = build_mock_client(
        async_get_school_free_days={
            "SchoolFreeDays": [
                {
                    "Id": 1,
                    "Name": "Ferie zimowe",
                    "DateFrom": _TOMORROW,
                    "DateTo": _TOMORROW,
                }
            ]
        },
    )
    entry = await setup_integration(hass, client)

    state = hass.states.get(_entity_id(hass, entry, "free_days"))
    assert state.attributes["message"] == "Ferie zimowe"


async def test_timetable_calendar_resolves_subject_and_teacher_names(hass) -> None:
    """CONFIRMED live: Timetables returns Subject/Teacher/Classroom ids as
    STRINGS, unlike every other endpoint - this must still resolve against
    the (int-keyed) subjects/teachers/classrooms lookups."""
    timetable_payload = {
        "Timetable": {
            _TOMORROW: [
                [
                    {
                        "LessonNo": "1",
                        "HourFrom": "08:00",
                        "HourTo": "08:45",
                        "Subject": {"Id": "42005", "Name": "Matematyka"},
                        "Teacher": {"Id": "1000"},
                        "Classroom": {"Id": "2000"},
                        "IsCanceled": False,
                        "IsSubstitutionClass": False,
                    }
                ]
            ]
        }
    }
    client = build_mock_client(
        async_get_subjects={"Subjects": [{"Id": 42005, "Name": "Matematyka"}]},
        async_get_teachers={"Users": [{"Id": 1000, "FirstName": "Anna", "LastName": "Kowalska"}]},
        async_get_classrooms={"Classrooms": [{"Id": 2000, "Name": "12"}]},
        # Both the current- and next-week fetches return the same payload -
        # the lesson's own date key decides which day it lands on, so which
        # of the two calls "contains" it doesn't matter for this test.
        async_get_timetable=timetable_payload,
    )
    entry = await setup_integration(hass, client)

    entity_id = _entity_id(hass, entry, "timetable")
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["message"] == "Matematyka"
    assert state.attributes["location"] == "12"
    assert state.attributes["description"] == "Anna Kowalska"
