"""Saved state and outage handling: seen ids across restarts, the last good
data while Librus is down, per-section fallbacks and retry backoff."""

from __future__ import annotations

from datetime import date, timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.librus_synergia.const import (
    EVENT_AGENDA_CHANGED,
    EVENT_NEW_GRADE,
    STATE_STORE_VERSION,
    STATUS_DEGRADED,
    STATUS_OK,
    STATUS_STALE,
)
from custom_components.librus_synergia.coordinator import (
    LibrusDataUpdateCoordinator,
    state_store_key,
)
from librus_synergia import LibrusConnectionError, LibrusUnexpectedResponseError

from .conftest import build_mock_client, make_config_entry, saved_state


def _grade(grade_id: int, value: str = "5") -> dict:
    return {
        "Id": grade_id,
        "Grade": value,
        "Category": {"Id": 10},
        "Subject": {"Id": 100},
        "Semester": 1,
        "AddDate": "2026-10-01",
    }


def _coordinator(hass, client, entry=None) -> LibrusDataUpdateCoordinator:
    entry = entry or make_config_entry()
    if entry.state is not ConfigEntryState.SETUP_IN_PROGRESS:
        entry.add_to_hass(hass)
        entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


def _store(hass_storage, entry_id: str, data: dict) -> None:
    hass_storage[state_store_key(entry_id)] = {
        "version": STATE_STORE_VERSION,
        "minor_version": 1,
        "key": state_store_key(entry_id),
        "data": data,
    }


async def test_saved_state_is_json_serializable(hass) -> None:
    client = build_mock_client(async_get_grades={"Grades": [_grade(1)]})
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    saved = saved_state(coordinator)

    assert saved["seen"]["grades"] == ["1"]
    assert saved["payloads"]["Grades"] == {"Grades": [_grade(1)]}
    assert saved["last_success_at"] is not None


async def test_grade_added_while_ha_was_off_fires_after_restart(hass, hass_storage) -> None:
    """Before: a restart re-seeded the seen ids silently, so a grade added
    while HA was off never produced an event."""
    first = _coordinator(hass, build_mock_client(async_get_grades={"Grades": [_grade(1)]}))
    await first._async_update_data()
    saved = saved_state(first)

    events = async_capture_events(hass, EVENT_NEW_GRADE)
    entry = make_config_entry()
    _store(hass_storage, entry.entry_id, saved)
    client = build_mock_client(async_get_grades={"Grades": [_grade(1), _grade(2, "4")]})
    second = _coordinator(hass, client, entry)
    await second.async_restore_state()
    await second._async_update_data()
    await hass.async_block_till_done()

    assert [e.data["id"] for e in events] == [2]


async def test_without_saved_state_first_poll_stays_silent(hass) -> None:
    events = async_capture_events(hass, EVENT_NEW_GRADE)
    coordinator = _coordinator(hass, build_mock_client(async_get_grades={"Grades": [_grade(1)]}))
    await coordinator.async_restore_state()
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events == []


async def test_librus_down_keeps_last_data_and_backs_off(hass, freezer) -> None:
    freezer.move_to("2026-10-07T10:00:00+00:00")
    client = build_mock_client(async_get_grades={"Grades": [_grade(1)]})
    coordinator = _coordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()
    assert coordinator.status == STATUS_OK

    client.async_ensure_session_valid.side_effect = LibrusConnectionError("down")
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert len(coordinator.data.grades) == 1
    assert coordinator.data_source == "stale"
    assert coordinator.status == STATUS_STALE
    assert coordinator.failures == 1
    assert coordinator.next_attempt_at is None

    await coordinator.async_refresh()
    assert coordinator.failures == 2
    assert coordinator.next_attempt_at == dt_util.utcnow() + timedelta(minutes=40)

    # Inside the backoff window no request is made at all.
    calls = client.async_ensure_session_valid.call_count
    freezer.tick(timedelta(minutes=20))
    await coordinator.async_refresh()
    assert client.async_ensure_session_valid.call_count == calls

    # Librus is back after the window: everything resets.
    client.async_ensure_session_valid.side_effect = None
    freezer.tick(timedelta(minutes=21))
    await coordinator.async_refresh()
    assert coordinator.failures == 0
    assert coordinator.next_attempt_at is None
    assert coordinator.data_source == "live"
    assert coordinator.status == STATUS_OK


async def test_manual_refresh_ignores_backoff(hass, freezer) -> None:
    freezer.move_to("2026-10-07T10:00:00+00:00")
    client = build_mock_client()
    coordinator = _coordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()
    client.async_ensure_session_valid.side_effect = LibrusConnectionError("down")
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    calls = client.async_ensure_session_valid.call_count

    await coordinator.async_force_refresh()
    await hass.async_block_till_done()

    assert client.async_ensure_session_valid.call_count == calls + 1
    await coordinator.async_shutdown()  # the refresh request's debounce timer


async def test_last_data_expires_after_max_age(hass, freezer) -> None:
    freezer.move_to("2026-10-07T10:00:00+00:00")
    client = build_mock_client()
    coordinator = _coordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    client.async_ensure_session_valid.side_effect = LibrusConnectionError("down")
    freezer.tick(timedelta(days=4))
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False


async def test_ha_start_during_outage_uses_saved_responses(hass, hass_storage) -> None:
    """Librus down while HA starts: the data is rebuilt from the saved
    responses instead of the whole entry failing to load."""
    subjects = {"Subjects": [{"Id": 100, "Name": "Matematyka"}]}
    first = _coordinator(
        hass, build_mock_client(async_get_grades={"Grades": [_grade(1)]}, async_get_subjects=subjects)
    )
    await first._async_update_data()
    saved = saved_state(first)

    events = async_capture_events(hass, EVENT_NEW_GRADE)
    entry = make_config_entry()
    _store(hass_storage, entry.entry_id, saved)
    client = build_mock_client()
    client.async_ensure_session_valid.side_effect = LibrusConnectionError("down")
    second = _coordinator(hass, client, entry)
    await second.async_restore_state()
    await second.async_config_entry_first_refresh()
    await hass.async_block_till_done()

    assert second.data_source == "cache"
    assert second.status == STATUS_STALE
    assert len(second.data.grades) == 1
    assert second.data.subjects == {100: "Matematyka"}
    assert second.data.messages_available is False
    assert second.last_success_at is not None
    assert events == []


async def test_failed_section_keeps_its_last_good_copy(hass) -> None:
    client = build_mock_client(async_get_grades={"Grades": [_grade(1)]})
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    client.async_get_grades.side_effect = LibrusUnexpectedResponseError("HTTP 500")
    data = await coordinator._async_update_data()

    assert len(data.grades) == 1
    assert coordinator.fallback_sections == {"Grades"}
    assert coordinator.data_source == "live"


async def test_failed_reference_endpoint_keeps_names(hass) -> None:
    """A failed daily reference refresh used to blank every subject name
    until the next day."""
    client = build_mock_client(async_get_subjects={"Subjects": [{"Id": 100, "Name": "Matematyka"}]})
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    client.async_get_subjects.side_effect = LibrusUnexpectedResponseError("HTTP 500")
    coordinator._reference_data_fetched_at = None
    data = await coordinator._async_update_data()

    assert data.subjects == {100: "Matematyka"}
    assert "Subjects" in coordinator.fallback_sections


async def test_failed_timetable_week_keeps_its_last_good_copy(hass, freezer) -> None:
    freezer.move_to("2026-10-07T10:00:00+00:00")  # a Wednesday
    timetable = {
        "Timetable": {
            "2026-10-07": [
                [
                    {
                        "LessonNo": "1",
                        "HourFrom": "08:00",
                        "HourTo": "08:45",
                        "Subject": {"Id": "100"},
                        "IsCanceled": False,
                        "IsSubstitutionClass": False,
                    }
                ]
            ]
        }
    }
    client = build_mock_client()
    client.async_get_timetable.side_effect = lambda week_start: (
        timetable if week_start == date(2026, 10, 5) else {}
    )
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    client.async_get_timetable.side_effect = LibrusConnectionError("timeout")
    data = await coordinator._async_update_data()

    assert sum(len(lessons) for lessons in data.timetable.values()) == 1
    assert coordinator.fallback_sections == {"Timetable"}
    assert coordinator.status == STATUS_DEGRADED


# ----------------------------------------------------------------------
# Agenda entries changed or removed (EVENT_AGENDA_CHANGED)
# ----------------------------------------------------------------------


def _agenda(*entries: tuple[int, str, str]) -> dict:
    return {
        "HomeWorks": [
            {"Id": item_id, "Date": day, "Content": text, "Category": {"Id": 5}, "Subject": {"Id": 100}}
            for item_id, day, text in entries
        ]
    }


async def test_agenda_entry_moved_and_removed_fire_events(hass, freezer) -> None:
    freezer.move_to("2026-10-07T10:00:00+00:00")
    events = async_capture_events(hass, EVENT_AGENDA_CHANGED)
    client = build_mock_client(
        async_get_homeworks=_agenda(
            (1, "2026-10-12", "Ułamki"), (2, "2026-10-15", "Komórka"), (3, "2026-10-01", "Stary")
        ),
        async_get_homework_categories={"Categories": [{"Id": 5, "Name": "Sprawdzian"}]},
        async_get_subjects={"Subjects": [{"Id": 100, "Name": "Matematyka"}]},
    )
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert events == []  # first sync only seeds

    # 1 moved to the 14th, 2 removed, 3 (in the past) removed - ignored.
    client.async_get_homeworks.return_value = _agenda((1, "2026-10-14", "Ułamki"))
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    by_kind = {e.data["kind"]: e.data for e in events}
    assert len(events) == 2
    assert by_kind["changed"]["id"] == 1
    assert by_kind["changed"]["changed_fields"] == ["date"]
    assert by_kind["changed"]["previous"] == {"date": "2026-10-12"}
    assert by_kind["changed"]["category"] == "Sprawdzian"
    assert by_kind["changed"]["subject"] == "Matematyka"
    assert by_kind["removed"]["id"] == 2
    assert by_kind["removed"]["content"] == "Komórka"


async def test_agenda_emptied_at_once_is_not_announced(hass, freezer) -> None:
    freezer.move_to("2026-10-07T10:00:00+00:00")
    events = async_capture_events(hass, EVENT_AGENDA_CHANGED)
    client = build_mock_client(async_get_homeworks=_agenda((1, "2026-10-12", "Ułamki")))
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    client.async_get_homeworks.return_value = {"HomeWorks": []}
    await coordinator._async_update_data()
    client.async_get_homeworks.return_value = _agenda((1, "2026-10-12", "Ułamki"))
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events == []


async def test_agenda_change_survives_restart(hass, hass_storage, freezer) -> None:
    freezer.move_to("2026-10-07T10:00:00+00:00")
    first = _coordinator(hass, build_mock_client(async_get_homeworks=_agenda((1, "2026-10-12", "Ułamki"))))
    await first._async_update_data()
    saved = saved_state(first)

    events = async_capture_events(hass, EVENT_AGENDA_CHANGED)
    entry = make_config_entry()
    _store(hass_storage, entry.entry_id, saved)
    second = _coordinator(
        hass, build_mock_client(async_get_homeworks=_agenda((1, "2026-10-13", "Ułamki"))), entry
    )
    await second.async_restore_state()
    await second._async_update_data()
    await hass.async_block_till_done()

    assert [e.data["previous"] for e in events] == [{"date": "2026-10-12"}]


def _message(message_id: str) -> dict:
    return {
        "messageId": message_id,
        "senderName": "Anna Nowak",
        "topic": f"Wiadomość {message_id}",
        "content": "",
        "sendDate": "2026-10-01T10:00:00",
        "readDate": None,
        "isAnyFileAttached": False,
    }


def _assignment(assignment_id: int) -> dict:
    return {
        "Id": assignment_id,
        "Topic": f"Zadanie {assignment_id}",
        "Text": "",
        "Teacher": {"Id": 200},
        "Date": "2026-10-01",
        "DueDate": "2026-10-09",
    }


async def test_messages_missing_at_first_poll_are_not_announced_later(hass) -> None:
    """Wiadomości didn't answer on the very first poll (the tracker was
    seeded without them): the inbox that shows up later is recorded
    silently, and only a message arriving after that is announced."""
    from custom_components.librus_synergia.const import EVENT_NEW_MESSAGE  # noqa: PLC0415

    events = async_capture_events(hass, EVENT_NEW_MESSAGE)
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.side_effect = LibrusConnectionError("down")
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    client.async_get_unread_messages_count.side_effect = None
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 2}}
    client.async_get_messages.return_value = {"data": [_message("1"), _message("2")]}
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert events == []

    # A new message raises the unread count - the inbox list is fetched
    # again only then (or once an hour).
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 3}}
    client.async_get_messages.return_value = {"data": [_message("1"), _message("2"), _message("3")]}
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert [e.data["id"] for e in events] == ["3"]


async def test_failed_first_fetch_of_homework_does_not_flood_later(hass) -> None:
    """Homework assignments failed on the first poll: nothing is recorded
    until they arrive, and then only later additions are announced."""
    from custom_components.librus_synergia.const import EVENT_NEW_HOMEWORK_ASSIGNMENT  # noqa: PLC0415

    events = async_capture_events(hass, EVENT_NEW_HOMEWORK_ASSIGNMENT)
    client = build_mock_client()
    client.async_get_homework_assignments.side_effect = LibrusUnexpectedResponseError("HTTP 500")
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    client.async_get_homework_assignments.side_effect = None
    client.async_get_homework_assignments.return_value = {"HomeWorkAssignments": [_assignment(1), _assignment(2)]}
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert events == []

    client.async_get_homework_assignments.return_value = {
        "HomeWorkAssignments": [_assignment(1), _assignment(2), _assignment(3)]
    }
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert [e.data["id"] for e in events] == [3]
