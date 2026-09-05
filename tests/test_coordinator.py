"""Tests for the Librus Synergia data update coordinator."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.librus_synergia.const import EVENT_NEW_GRADE
from custom_components.librus_synergia.coordinator import LibrusDataUpdateCoordinator
from custom_components.librus_synergia.librus_api import (
    LibrusConnectionError,
    LibrusInvalidCredentialsError,
    LibrusSessionExpiredError,
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
    # async_config_entry_first_refresh() asserts the entry is mid-setup -
    # true when HA drives it via async_setup_entry, but this suite calls it
    # directly on a bare MockConfigEntry, so fake that state ourselves.
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
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


async def test_session_expired_mid_cycle_recovers_via_forced_relogin(hass) -> None:
    """CONFIRMED live (2026-09-05): Librus's real session lifetime can run
    shorter than our own assumed-lifetime clock - a data endpoint rejects
    the session mid-cycle even though `async_ensure_session_valid` thought
    it was still fresh. The coordinator must force one re-login and retry
    silently, WITHOUT ever asking the user to reauth, as long as the
    forced re-login itself succeeds."""
    client = build_mock_client()
    client.async_get_grades.side_effect = [
        LibrusSessionExpiredError("session dead"),
        GRADE_PAYLOAD,
    ]
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert len(data.grades) == 1
    # First call is the normal not-yet-expired check; second is the forced
    # re-login triggered by the session-expired error.
    assert client.async_ensure_session_valid.call_count == 2
    _, kwargs = client.async_ensure_session_valid.call_args
    assert kwargs.get("force") is True


async def test_session_expired_and_relogin_also_fails_raises_auth_failed(hass) -> None:
    """If the forced re-login itself fails (genuinely wrong password,
    captcha, account action required), THAT must surface as a real reauth
    prompt - there's nothing more to retry automatically."""
    client = build_mock_client()
    client.async_get_grades.side_effect = LibrusSessionExpiredError("session dead")

    async def ensure_session_valid(password, *, force: bool = False):
        if force:
            raise LibrusInvalidCredentialsError("bad password")

    client.async_ensure_session_valid.side_effect = ensure_session_valid
    coordinator = _make_coordinator(hass, client)

    with pytest.raises(ConfigEntryAuthFailed):
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
    assert data.unread_messages_by_mailbox == {}
    assert data.messages == []
    client.async_get_unread_messages_count.assert_not_called()


async def test_messages_parsed_when_available(hass) -> None:
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {
        "data": {"inbox": 1, "notes": 2, "alerts": 0}
    }
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
    assert data.unread_messages_by_mailbox["inbox"] == 1
    assert data.unread_messages_by_mailbox["notes"] == 2
    assert data.unread_messages_by_mailbox["alerts"] == 0
    assert data.unread_messages_by_mailbox["trash"] == 0
    assert len(data.messages) == 1
    assert data.messages[0].id == "42"
    assert data.messages[0].content == "Dzień dobry"
