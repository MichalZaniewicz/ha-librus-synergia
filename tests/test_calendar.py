"""Tests for the Librus Synergia calendar platform."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.librus_synergia.const import DOMAIN
from custom_components.librus_synergia.librus_api import (
    LibrusAuthError,
    LibrusSessionExpiredError,
)

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


_TODAY = dt_util.now().date().isoformat()


async def _get_timetable_events(hass, entity_id: str) -> list[dict]:
    """Calls the real `calendar.get_events` service - the same code path
    Home Assistant's own calendar REST API (and therefore any dashboard
    card's `hass.callApi('calendars/...')`) exercises - to invoke
    `CalendarEntity.async_get_events` end to end, for "today" only.

    Deliberately a single calendar day, not "this ISO week": a wider
    window can straddle an ISO-week boundary depending on which real
    weekday the suite happens to run on (e.g. Sat-Wed spans two weeks),
    which would make `LibrusTimetableCalendar.async_get_events` fetch TWO
    weeks and call the mocked client twice as often as this test's
    `side_effect` list expects - the exact class of weekday-boundary bug
    this whole test file exists to catch, almost caught in the test
    itself instead of the product code. One calendar day can never span
    two ISO weeks, so this is safe regardless of which day CI runs on."""
    start_of_today = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": entity_id,
            "start_date_time": start_of_today,
            "end_date_time": start_of_today + timedelta(days=1),
        },
        blocking=True,
        return_response=True,
    )
    return response[entity_id]["events"]


async def test_timetable_calendar_recovers_from_mid_cycle_session_expiry(hass) -> None:
    """BUG FIX (2026-09-06, found live): `async_get_events` fetching a week
    outside the coordinator's own current+next-week cache used to call
    `client.async_get_timetable` directly, with none of `_async_update_
    data`'s forced-relogin-and-retry-once recovery for a session that died
    since the coordinator's last successful poll. The raw
    LibrusSessionExpiredError crashed the whole `/api/calendars/<entity>`
    request with an unhandled 500 - confirmed live via
    `ha_config_get_calendar_events` before this fix, traced to this
    exact exception via the error log. Must now recover silently."""
    good_payload = {
        "Timetable": {
            _TODAY: [
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
    )
    entry = await setup_integration(hass, client)
    entity_id = _entity_id(hass, entry, "timetable")

    # Only now (after the coordinator's own successful setup poll already
    # consumed the default return_value) make the NEXT call - the on-demand
    # one from async_get_events - fail once with a session expiry, then
    # succeed once force-relogin has run.
    client.async_get_timetable.side_effect = [
        LibrusSessionExpiredError("Session rejected on .../Timetables (HTTP 401)."),
        good_payload,
    ]

    events = await _get_timetable_events(hass, entity_id)

    assert len(events) == 1
    assert events[0]["summary"] == "Matematyka"
    # Confirms the recovery path actually ran a FORCED re-login, not just
    # a retry against the same (still-expired) session.
    force_calls = [
        c for c in client.async_ensure_session_valid.call_args_list if c.kwargs.get("force")
    ]
    assert len(force_calls) == 1


async def test_timetable_calendar_degrades_gracefully_when_recovery_fails(hass) -> None:
    """If the forced re-login retry ALSO fails, the calendar must degrade
    to "no lessons known for this week" rather than propagate the raw
    error and 500 the whole request - same non-fatal-degradation
    philosophy as every other optional data source in this integration."""
    client = build_mock_client()
    entry = await setup_integration(hass, client)
    entity_id = _entity_id(hass, entry, "timetable")

    client.async_get_timetable.side_effect = [
        LibrusSessionExpiredError("Session rejected on .../Timetables (HTTP 401)."),
        LibrusAuthError("Still rejected after forced re-login."),
    ]

    events = await _get_timetable_events(hass, entity_id)

    assert events == []


async def test_parent_teacher_conference_merged_into_agenda(hass) -> None:
    """Defensive extra merge - ParentTeacherConferences events must show
    up in the Agenda calendar too, even though HomeWorks already covers
    this on the real account (see ParentTeacherConferenceData's
    docstring)."""
    client = build_mock_client(
        async_get_parent_teacher_conferences={
            "ParentTeacherConferences": [
                {
                    "Id": 1,
                    "Topic": "Organizacja roku szkolnego",
                    "Teacher": {"Id": 200},
                    "Date": _TOMORROW,
                    "Time": "17:00:00",
                }
            ]
        },
        async_get_teachers={"Users": [{"Id": 200, "FirstName": "Amelia", "LastName": "Marciszak"}]},
    )
    entry = await setup_integration(hass, client)

    state = hass.states.get(_entity_id(hass, entry, "agenda"))
    assert state.state == "off"
    assert "[Zebranie z Rodzicami]" in state.attributes["message"]
    assert "Organizacja roku szkolnego" in state.attributes["message"]
