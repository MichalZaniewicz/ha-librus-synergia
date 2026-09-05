"""Tests for the Librus Synergia data update coordinator."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.librus_synergia.const import EVENT_NEW_GRADE
from custom_components.librus_synergia.coordinator import LibrusDataUpdateCoordinator
from custom_components.librus_synergia.librus_api import (
    LibrusConnectionError,
    LibrusInvalidCredentialsError,
)

from .conftest import build_mock_client, make_config_entry

GRADE_PAYLOAD = {
    "Grades": [
        {
            "Id": 1,
            "Grade": "5",
            "Category": {"Id": 10},
            "Subject": {"Id": 100},
            "Semester": 1,
            "AddDate": "2026-09-01",
        }
    ]
}


def _make_coordinator(hass, client) -> LibrusDataUpdateCoordinator:
    entry = make_config_entry()
    entry.add_to_hass(hass)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


async def test_first_refresh_builds_data_and_seeds_silently(hass) -> None:
    """A brand-new entry's first refresh must populate data without firing
    any "new item" events - those events exist to flag things that appeared
    SINCE the last check, and there is no "last check" yet."""
    events = async_capture_events(hass, EVENT_NEW_GRADE)
    client = build_mock_client(async_get_grades=GRADE_PAYLOAD)
    coordinator = _make_coordinator(hass, client)

    await coordinator.async_config_entry_first_refresh()
    await hass.async_block_till_done()

    assert coordinator.data is not None
    assert len(coordinator.data.grades) == 1
    assert events == []


async def test_second_refresh_fires_new_grade_event(hass) -> None:
    events = async_capture_events(hass, EVENT_NEW_GRADE)
    client = build_mock_client()
    coordinator = _make_coordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    client.async_get_grades.return_value = GRADE_PAYLOAD
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["id"] == 1
    assert events[0].data["value"] == "5"


async def test_grade_seen_twice_is_not_re_announced(hass) -> None:
    """Union, not replace: a grade dropping out of a later fetch and then
    reappearing must not fire a second event for the same id."""
    events = async_capture_events(hass, EVENT_NEW_GRADE)
    client = build_mock_client()
    coordinator = _make_coordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    client.async_get_grades.return_value = GRADE_PAYLOAD
    await coordinator.async_refresh()
    client.async_get_grades.return_value = {"Grades": []}
    await coordinator.async_refresh()
    client.async_get_grades.return_value = GRADE_PAYLOAD
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 1


async def test_auth_error_raises_config_entry_auth_failed(hass) -> None:
    client = build_mock_client()
    client.async_ensure_session_valid.side_effect = LibrusInvalidCredentialsError("bad")
    coordinator = _make_coordinator(hass, client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_connection_error_raises_update_failed(hass) -> None:
    client = build_mock_client()
    client.async_ensure_session_valid.side_effect = LibrusConnectionError("net")
    coordinator = _make_coordinator(hass, client)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_messages_unavailable_school_is_non_fatal(hass) -> None:
    """Bootstrap returning False (module not enabled for this school) must
    not fail the whole update cycle - see _async_get_messages."""
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = False
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert data.messages_available is False
    assert data.unread_message_count == 0
    assert data.messages == []
    client.async_get_unread_messages_count.assert_not_called()


async def test_messages_parsed_when_available(hass) -> None:
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 1}}
    client.async_get_messages.return_value = {
        "data": [
            {
                "messageId": "42",
                "senderName": "Amelia Marciszak",
                "topic": "Zebranie",
                "content": "RHppZcWEIGRvYnJ5",
                "sendDate": "2026-09-04T17:47:10",
                "readDate": None,
                "isAnyFileAttached": False,
            }
        ]
    }
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert data.messages_available is True
    assert data.unread_message_count == 1
    assert len(data.messages) == 1
    assert data.messages[0].id == "42"
    assert data.messages[0].content == "Dzień dobry"
