"""The school's grade scale, the new descriptive grading (grade 1) and
read receipts for sent messages."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.librus_synergia.const import DOMAIN, EVENT_MESSAGE_READ, EVENT_NEW_GRADE
from custom_components.librus_synergia.coordinator import LibrusDataUpdateCoordinator

from .conftest import build_mock_client, make_config_entry, messages_by_mailbox, setup_integration

SUBJECTS = {"Subjects": [{"Id": 9, "Name": "Edukacja muzyczna"}]}
TEACHERS = {"Users": [{"Id": 7, "AccountId": "LID-AUTH-USER-T", "FirstName": "Anna", "LastName": "Nowak"}]}
PARTIAL = {
    "data": [
        {
            "gradeId": 501,
            "teacherId": "LID-AUTH-USER-T",
            "area": {"name": "Muzyka i ruch"},
            "subjectId": "LID-S-9",
            "scaleValue": {"value": "W"},
            "content": "Śpiewa czysto",
            "implementedRequirements": [{"name": "Śpiewa piosenki"}],
            "date": "2026-10-01",
            "semester": 1,
            "addDate": "2026-10-01 10:00:00",
        }
    ],
    "pagination": {"limit": 50, "page": 1, "total": 1},
}


def _coordinator(hass, client) -> LibrusDataUpdateCoordinator:
    entry = make_config_entry()
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


def _sensor(hass, entry, key: str):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
    return hass.states.get(entity_id)


def _partial_client():
    return build_mock_client(
        async_get_subjects=SUBJECTS,
        async_get_teachers=TEACHERS,
        async_get_token_info={"UserIdentifier": "LID-AUTH-USER-P"},
        async_get_user_info={"IdentifierOfStudentAssignedWithUser": "LID-AUTH-USER-K"},
        async_get_partial_grades=PARTIAL,
        async_get_auth_subjects={"data": [{"identifier": "LID-S-9", "numericIdentifier": 9}]},
    )


async def test_average_uses_the_schools_plus_value(hass) -> None:
    client = build_mock_client(
        async_get_grades={"Grades": [{"Id": 1, "Grade": "4+", "Subject": {"Id": 9}, "AddDate": "2026-10-01"}]},
        async_get_subjects=SUBJECTS,
        async_get_grading_system={"countZero": False, "plusValue": 0.25, "minusValue": 0.25},
    )
    entry = await setup_integration(hass, client)

    assert float(_sensor(hass, entry, "overall_average").state) == 4.25


async def test_partial_grades_join_the_descriptive_grades(hass) -> None:
    client = _partial_client()
    entry = await setup_integration(hass, client)

    client.async_get_partial_grades.assert_awaited_with("LID-AUTH-USER-K")
    grade = _sensor(hass, entry, "descriptive_grades").attributes["grades"][0]
    assert grade["subject"] == "Edukacja muzyczna"
    assert (grade["value"], grade["skill"], grade["teacher"]) == ("W", "Muzyka i ruch", "Anna Nowak")
    assert grade["comments"] == ["Śpiewa czysto"]
    assert grade["requirements"] == ["Śpiewa piosenki"]


async def test_new_partial_grade_fires_new_grade_event(hass) -> None:
    events = async_capture_events(hass, EVENT_NEW_GRADE)
    client = _partial_client()
    client.async_get_partial_grades.return_value = {"data": []}
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()  # first sync only seeds

    client.async_get_partial_grades.return_value = PARTIAL
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    (event,) = events
    assert (event.data["kind"], event.data["value"], event.data["teacher"]) == ("descriptive", "W", "Anna Nowak")


async def test_child_lookup_is_daily_and_a_refusal_means_no_partial_grades(hass) -> None:
    client = build_mock_client()
    client.async_get_token_info.return_value = {"UserIdentifier": "LID-AUTH-USER-P"}
    client.async_get_user_info.return_value = {}
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert client.async_get_user_info.call_count == 1
    client.async_get_partial_grades.assert_not_called()


def _sent(read: str):
    today = dt_util.now().date().isoformat()
    return {
        "data": {
            "messageId": "9",
            "topic": "Pytanie",
            "Message": "",
            "receivers": [{"firstName": "Anna", "lastName": "Nowak", "group": "nauczyciel", "readed": read}],
        }
    }, {
        "outbox": {
            "data": [
                {"messageId": "9", "receiverName": "Anna Nowak", "topic": "Pytanie", "content": "", "sendDate": f"{today} 08:00:00"}
            ]
        }
    }


async def test_read_receipt_fires_once_when_the_recipient_reads(hass) -> None:
    events = async_capture_events(hass, EVENT_MESSAGE_READ)
    unread_detail, mailboxes = _sent("")
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 0}}
    client.async_get_messages.side_effect = messages_by_mailbox(mailboxes)
    client.async_get_message.return_value = unread_detail
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()  # first look only records
    client.async_get_message.assert_awaited_with("outbox", "9")
    assert coordinator.read_receipts["9"]["read"] == 0

    read_detail, _ = _sent("2026-10-02 08:00:00")
    client.async_get_message.return_value = read_detail
    coordinator._receipts_fetched_at.clear()  # checked at most once an hour
    await coordinator._async_update_data()
    coordinator._receipts_fetched_at.clear()
    await coordinator._async_update_data()  # everyone has read it: not asked again
    await hass.async_block_till_done()

    (event,) = events
    assert (event.data["receiver"], event.data["topic"]) == ("Anna Nowak", "Pytanie")
    assert client.async_get_message.await_count == 2
