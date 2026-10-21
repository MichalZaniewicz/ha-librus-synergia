"""New behaviour grades and new messages outside the inbox fire events."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.librus_synergia.const import EVENT_NEW_GRADE, EVENT_NEW_MESSAGE
from custom_components.librus_synergia.coordinator import LibrusDataUpdateCoordinator
from librus_synergia import LibrusConnectionError
from librus_synergia.exceptions import LibrusUnexpectedResponseError

from .conftest import build_mock_client, make_config_entry, messages_by_mailbox

BEHAVIOUR_OLD = {
    "Id": 1, "BehaviourGrade": {"Id": 2}, "Category": {"Id": 5}, "AddedBy": {"Id": 7},
    "Semester": 1, "AddDate": "2026-10-05 10:00:00", "Text": "Ocena zachowania miesiąc za IX/26.",
}
BEHAVIOUR_NEW = {
    "Id": 2, "BehaviourGrade": {"Id": 1}, "Category": {"Id": 5}, "AddedBy": {"Id": 7},
    "Semester": 1, "AddDate": "2026-11-04 10:00:00", "Text": "Ocena zachowania miesiąc za X/26.",
}
# Nothing set yet: not announced until it gets a value.
BEHAVIOUR_EMPTY = {"Id": 3, "Category": {"Id": 5}, "Semester": 1, "AddDate": "2026-11-04 10:00:00"}


def _coordinator(hass, client, *, options: dict | None = None) -> LibrusDataUpdateCoordinator:
    entry = make_config_entry(options=options)
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


async def test_new_behaviour_grade_fires_new_grade_event(hass) -> None:
    grades = async_capture_events(hass, EVENT_NEW_GRADE)
    client = build_mock_client(
        async_get_behaviour_grade_points={"Grades": [BEHAVIOUR_OLD]},
        async_get_behaviour_grade_point_categories={"Categories": [{"Id": 5, "Name": "zachowanie"}]},
    )
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()  # first sync only seeds
    await hass.async_block_till_done()
    assert grades == []

    client.async_get_behaviour_grade_points.return_value = {
        "Grades": [BEHAVIOUR_OLD, BEHAVIOUR_NEW, BEHAVIOUR_EMPTY]
    }
    coordinator._fetched_at.clear()
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(grades) == 1
    data = grades[0].data
    assert (data["kind"], data["id"], data["value"], data["name"]) == ("behaviour", 2, "wz", "wzorowe")
    assert (data["month"], data["category"], data["subject"]) == ("2026-10", "zachowanie", None)
    assert data["entry_id"] == coordinator.config_entry.entry_id

    await coordinator._async_update_data()  # not re-announced
    await hass.async_block_till_done()
    assert len(grades) == 1


async def test_behaviour_grades_not_seeded_when_off_or_failed(hass) -> None:
    client = build_mock_client(async_get_behaviour_grade_points={"Grades": [BEHAVIOUR_OLD]})
    coordinator = _coordinator(hass, client, options={"behaviour_grades_enabled": False})
    await coordinator._async_update_data()
    assert coordinator._known_items["behaviour_grades"] is None

    client = build_mock_client()
    client.async_get_behaviour_grade_points.side_effect = LibrusConnectionError("down")
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()
    assert coordinator._known_items["behaviour_grades"] is None


def _message(message_id: str, topic: str) -> dict:
    return {"messageId": message_id, "senderName": "Sekretariat", "topic": topic, "content": ""}


async def test_new_message_in_other_mailbox_fires_new_message_event(hass) -> None:
    messages = async_capture_events(hass, EVENT_NEW_MESSAGE)
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 0, "substitutions": 0}}
    client.async_get_messages.side_effect = messages_by_mailbox(
        {"substitutions": {"data": [_message("s1", "Zastępstwo 1")]}}
    )
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()  # first sync only seeds
    await hass.async_block_till_done()
    assert messages == []

    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 0, "substitutions": 1}}
    client.async_get_messages.side_effect = messages_by_mailbox(
        {"substitutions": {"data": [_message("s2", "Zastępstwo 2"), _message("s1", "Zastępstwo 1")]}}
    )
    coordinator._fetched_at.clear()
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(messages) == 1
    data = messages[0].data
    assert (data["id"], data["mailbox"], data["topic"], data["sender"]) == (
        "s2", "substitutions", "Zastępstwo 2", "Sekretariat"
    )


async def test_missing_mailbox_is_not_seeded(hass) -> None:
    """A mailbox the account doesn't have (404) stays unseeded, so its
    messages aren't all announced once it appears."""
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 0}}
    client.async_get_messages.side_effect = messages_by_mailbox(
        {"alerts": LibrusUnexpectedResponseError("not found", status_code=404)}
    )
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    assert coordinator._known_items["alerts_messages"] is None
    assert coordinator._known_items["substitutions_messages"] == set()
