"""Kindergarten (przedszkole) account support - issue #5 / PR #8."""

from __future__ import annotations

from datetime import date, timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.util import dt as dt_util

from librus_synergia import LibrusSessionExpiredError
from librus_synergia.parsers import (
    merge_timetables,
    parse_kindergarten_classrooms,
    parse_kindergarten_group,
    parse_kindergarten_teachers,
)

from custom_components.librus_synergia.coordinator import (
    LibrusDataUpdateCoordinator,
    summarize_kindergarten_entries,
)

from .conftest import build_mock_client, make_config_entry

CHILD_LID = "LID-AUTH-USER-5241-CHILD"
PARENT_LID = "LID-AUTH-USER-5241-PARENT"
TEACHER_LID = "LID-AUTH-USER-5241-TEACHER"


def _entries_payload(day: date) -> dict:
    return {
        "timetableEntries": [
            {
                "identifier": "L1",
                "activityTypeIdentifier": "ACT1",
                "classroomIdentifier": "ROOM1",
                "type": "planned",
                "date": day.isoformat(),
                "startTime": "07:00",
                "endTime": "12:00",
                "teachers": [TEACHER_LID],
            }
        ]
    }


def _forbidden(endpoint: str) -> LibrusSessionExpiredError:
    return LibrusSessionExpiredError(
        f"Session rejected on .../{endpoint} (HTTP 403).", status_code=403
    )


def _make_coordinator(hass, client) -> LibrusDataUpdateCoordinator:
    entry = make_config_entry()
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


def _kindergarten_client(today: date):
    """A kindergarten account as reported: standard Timetables 403s, the
    parent's LID comes back empty from the kindergarten timetable, the
    child's (found via Auth/UserInfo) has entries."""
    client = build_mock_client()
    client.async_get_timetable.side_effect = _forbidden("Timetables")
    client.async_get_token_info.return_value = {"UserIdentifier": PARENT_LID}
    client.async_get_user_info.return_value = {"children": [{"identifier": CHILD_LID}]}

    async def timetable(lid, date_from, date_to):
        return _entries_payload(today) if lid == CHILD_LID else {"timetableEntries": []}

    client.async_get_kindergarten_timetable.side_effect = timetable
    client.async_get_kindergartener.return_value = {"data": {"groupIdentifier": "GROUP1"}}
    client.async_get_kindergarten_group.return_value = {"name": "Motylki", "tutors": [TEACHER_LID]}
    client.async_get_kindergarten_activity_types.return_value = {
        "activitiesTypes": [{"identifier": "ACT1", "name": "Edukacja przedszkolna"}]
    }
    client.async_get_kindergarten_classrooms.return_value = {
        "data": [{"identifier": "ROOM1", "symbol": "s. 1"}]
    }
    client.async_get_teachers.return_value = {
        "Users": [{"Id": 5, "AccountId": TEACHER_LID, "FirstName": "Anna", "LastName": "Nowak"}]
    }
    return client


def test_merge_timetables_understands_kindergarten_entries() -> None:
    day = date(2026, 9, 28)
    lesson = merge_timetables(_entries_payload(day))[day][0]
    assert (lesson.hour_from, lesson.hour_to) == ("07:00", "12:00")
    assert lesson.lesson_no is None
    assert lesson.subject_id == "ACT1"
    assert lesson.classroom_id == "ROOM1"
    assert lesson.teacher_id == TEACHER_LID
    assert lesson.teacher_ids == (TEACHER_LID,)
    assert not lesson.is_canceled
    assert not lesson.is_substitution


def test_kindergarten_lookup_parsers() -> None:
    group = parse_kindergarten_group({"name": "0B", "tutors": [TEACHER_LID]})
    assert group is not None
    assert (group.symbol, group.tutor_id, group.number) == ("0B", TEACHER_LID, None)
    assert parse_kindergarten_group({}) is None
    assert parse_kindergarten_teachers(
        {"Users": [{"AccountId": "T1", "FirstName": None, "LastName": "Gigiel"}]}
    ) == {"T1": "Gigiel"}
    assert parse_kindergarten_classrooms(
        {"data": [{"identifier": "ROOM1", "symbol": "1", "name": "sala 1"}]}
    ) == {"ROOM1": "sala 1"}
    # No name -> a bare numeric symbol gets the "sala" prefix.
    assert parse_kindergarten_classrooms(
        {"data": [{"identifier": "ROOM2", "symbol": "2"}]}
    ) == {"ROOM2": "sala 2"}

    # Existing abbreviated room names must remain unchanged.
    assert parse_kindergarten_classrooms(
        {"data": [{"identifier": "ROOM3", "symbol": "s. 1"}]}
    ) == {"ROOM3": "s. 1"}


async def test_regular_account_makes_no_discovery_requests(hass) -> None:
    """The whole point of gating discovery on a Timetables 403: an ordinary
    student account must not pay a single extra request for this. (Auth/
    TokenInfo is asked once a day for the child's LID, for the new
    descriptive grading - not for kindergarten discovery.)"""
    client = build_mock_client()
    coordinator = _make_coordinator(hass, client)

    await coordinator._async_update_data()

    assert client.async_get_token_info.call_count == 1
    client.async_get_user.assert_not_called()
    client.async_get_kindergarten_timetable.assert_not_called()
    client.async_get_kindergarten_activity_types.assert_not_called()
    assert not coordinator.is_kindergarten


async def test_kindergarten_discovered_and_resolved_in_first_cycle(hass) -> None:
    today = dt_util.now().date()
    client = _kindergarten_client(today)
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert coordinator.is_kindergarten
    assert coordinator.kindergarten_diagnostics["source"] == "Auth/UserInfo"
    lesson = data.timetable[today][0]
    assert data.subjects[lesson.subject_id] == "Edukacja przedszkolna"
    assert data.classrooms[lesson.classroom_id] == "s. 1"
    assert data.teachers[lesson.teacher_id] == "Anna Nowak"
    assert data.school_class is not None
    assert data.school_class.symbol == "Motylki"
    # The regular Timetables endpoint isn't asked again once the child's
    # LID is known.
    calls_before = client.async_get_timetable.call_count
    await coordinator._async_update_data()
    assert client.async_get_timetable.call_count == calls_before
    assert "Timetable" not in coordinator.degraded_endpoints
    # Diagnostics describe the polled weeks' entries by type, without ids.
    weeks = coordinator.kindergarten_diagnostics["entries_by_week"]
    assert weeks
    assert all(week["planned"]["count"] >= 1 for week in weeks.values())
    assert "LID-" not in str(weeks)
    assert TEACHER_LID not in str(weeks)


def test_kindergarten_entry_summary_shows_shapes_not_ids() -> None:
    """Issue #14: a substitution's entries described by type - the link to
    the replaced block shows as `ref:<type>`, names never appear."""
    summary = summarize_kindergarten_entries(
        [
            {
                "identifier": "LID-E1",
                "type": "substituted",
                "date": "2026-10-05",
                "startTime": "07:00",
                "endTime": "09:00",
                "teachers": [TEACHER_LID],
                "note": "Pani Kowalska chora",
                "isVisible": True,
            },
            {
                "identifier": "LID-E2",
                "type": "substitution",
                "date": "2026-10-05",
                "startTime": "07:00",
                "endTime": "09:00",
                "substitutedEntryIdentifier": "LID-E1",
                "status": "active",
            },
        ]
    )
    original, substitute = summary["substituted"], summary["substitution"]
    assert original["count"] == 1
    assert original["fields"]["identifier"] == ["lid"]
    assert original["fields"]["teachers"] == ["list[lid]"]
    assert original["fields"]["note"] == ["text"]
    assert original["fields"]["isVisible"] == ["true"]
    assert original["fields"]["startTime"] == ["time"]
    assert substitute["fields"]["substitutedEntryIdentifier"] == ["ref:substituted"]
    assert substitute["fields"]["status"] == ["'active'"]
    assert substitute["examples"] == [
        {"date": "2026-10-05", "startTime": "07:00", "endTime": "09:00"}
    ]
    assert "Kowalska" not in str(summary)
    assert "LID-" not in str(summary)
    assert summarize_kindergarten_entries(None) == {}


async def test_forbidden_discovery_probes_never_fail_the_update(hass) -> None:
    """The original PR re-raised a 403 from these auxiliary endpoints - that
    would have been taken for a dead session and ended in reauth."""
    client = build_mock_client()
    client.async_get_timetable.side_effect = _forbidden("Timetables")
    client.async_get_token_info.side_effect = _forbidden("Auth/TokenInfo")
    client.async_get_user.side_effect = _forbidden("Users/1234567")
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert data.timetable == {}
    assert not coordinator.is_kindergarten
    force_calls = [
        c for c in client.async_ensure_session_valid.call_args_list if c.kwargs.get("force")
    ]
    assert force_calls == []


async def test_failed_discovery_is_not_retried_every_cycle(hass) -> None:
    """A regular school with an unpublished timetable (issue #4) also 403s -
    discovery must back off rather than probe on every poll."""
    client = build_mock_client()
    client.async_get_timetable.side_effect = _forbidden("Timetables")
    coordinator = _make_coordinator(hass, client)

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    # Once for discovery, once for the child's LID (new descriptive
    # grading) - neither repeated on the second poll.
    assert client.async_get_token_info.call_count == 2
