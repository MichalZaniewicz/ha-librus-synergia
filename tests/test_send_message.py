"""Writing messages: get_message_recipients, send_message (incl. replies and
dry runs) and withdraw_message, and the "Allow sending messages" option."""

from __future__ import annotations

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from custom_components.librus_synergia.const import CONF_MESSAGE_SENDING_ENABLED, DOMAIN
from custom_components.librus_synergia.messaging import RecipientError, resolve_recipient
from librus_synergia import LibrusMessageSendError
from librus_synergia.models import MessageRecipientData, SentMessageData

from .conftest import build_mock_client, setup_integration

TYPES = {
    "data": {
        "list": [
            {"id": "tutors", "name": "Wychowawcy"},
            {"id": "teachers", "name": "Nauczyciele"},
            {"id": "secretary", "name": "Sekretariat"},
        ]
    }
}
GROUPS = {
    "tutors": {"receivers": [{"accountId": "11", "label": "Nowak Anna"}]},
    "teachers": {
        "receivers": [
            {"accountId": "11", "label": "Nowak Anna"},
            {"accountId": "12", "label": "Kowal Jan"},
        ]
    },
    "secretary": {"receivers": [{"accountId": "13", "label": "sekretariat"}]},
}
SUBJECTS = {
    "data": [
        {"teacherIdentifier": 11, "subject": "Biologia"},
        {"teacherIdentifier": 12, "subject": "Matematyka"},
    ]
}
ON = {CONF_MESSAGE_SENDING_ENABLED: True}


async def _setup(hass, *, options: dict | None = None, **overrides):
    client = build_mock_client(**overrides)
    client.async_get_message_recipient_types.return_value = TYPES
    client.async_get_message_recipients.side_effect = lambda group: GROUPS[group]
    client.async_get_student_subject_teachers.return_value = SUBJECTS
    client.async_get_messages_me.return_value = {"accountId": 1}
    client.async_send_message_checked.return_value = SentMessageData(message_id=987, status="sent")
    client.async_get_messages.return_value = {"data": []}
    entry = await setup_integration(hass, client, options=options)
    # The mock account has no Wiadomości during the poll; switch it on here.
    entry.runtime_data._messages_available = True
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    return client, device.id


async def _send(hass, device_id: str, **data):
    return await hass.services.async_call(
        DOMAIN,
        "send_message",
        {"device_id": device_id, **data},
        blocking=True,
        return_response=True,
    )


async def test_sending_is_off_by_default(hass) -> None:
    client, device_id = await _setup(hass)
    with pytest.raises(ServiceValidationError) as err:
        await _send(hass, device_id, recipient="wychowawca", topic="T", message="M")
    assert err.value.translation_key == "message_sending_disabled"
    client.async_send_message_checked.assert_not_called()


async def test_dry_run_works_with_sending_off_and_sends_nothing(hass) -> None:
    client, device_id = await _setup(hass)
    response = await _send(
        hass, device_id, recipient="Matematyka", topic="Pytanie", message="Treść", dry_run=True
    )
    assert response == {
        "sent": False,
        "dry_run": True,
        "to": ["Kowal Jan"],
        "account_ids": ["12"],
        "topic": "Pytanie",
        "message": "Treść",
    }
    client.async_send_message_checked.assert_not_called()


async def test_send_to_a_subjects_teacher_and_the_tutor(hass) -> None:
    client, device_id = await _setup(hass, options=ON)
    response = await _send(
        hass, device_id, recipient=["Matematyka", "wychowawca"], topic="Pytanie", message="Treść"
    )
    assert response["sent"] is True and response["message_id"] == 987
    assert response["to"] == ["Kowal Jan", "Nowak Anna"]
    client.async_send_message_checked.assert_awaited_once_with(
        ["12", "11"], "Pytanie", "Treść", reply_to=None
    )
    # The session was checked with a read before the send.
    client.async_get_messages_me.assert_awaited()


async def test_ambiguous_or_unknown_recipient_is_refused(hass) -> None:
    client, device_id = await _setup(hass, options=ON)
    with pytest.raises(ServiceValidationError) as err:
        await _send(hass, device_id, recipient="a", topic="T", message="M")
    assert err.value.translation_key == "message_recipient_ambiguous"
    with pytest.raises(ServiceValidationError) as err:
        await _send(hass, device_id, recipient="Fizyka", topic="T", message="M")
    assert err.value.translation_key == "message_recipient_unknown"
    client.async_send_message_checked.assert_not_called()


async def test_reply_goes_to_the_sender_in_the_thread(hass) -> None:
    client, device_id = await _setup(
        hass,
        options=ON,
        async_get_message={
            "data": {"topic": "Zebranie", "senderId": "13", "senderName": "sekretariat", "noReply": 0}
        },
    )
    response = await _send(hass, device_id, reply_to="55", message="Dziękuję")
    assert response["to"] == ["sekretariat"] and response["topic"] == "Re: Zebranie"
    client.async_send_message_checked.assert_awaited_once_with(
        ["13"], "Re: Zebranie", "Dziękuję", reply_to="55"
    )


async def test_no_reply_message_is_not_answered(hass) -> None:
    client, device_id = await _setup(
        hass,
        options=ON,
        async_get_message={"data": {"topic": "System", "senderId": "2", "noReply": 1}},
    )
    with pytest.raises(HomeAssistantError) as err:
        await _send(hass, device_id, reply_to="56", message="x")
    assert err.value.translation_key == "message_not_sent"
    client.async_send_message_checked.assert_not_called()


async def test_unconfirmed_send_says_it_may_have_gone_out(hass) -> None:
    client, device_id = await _setup(hass, options=ON)
    client.async_send_message_checked.side_effect = LibrusMessageSendError("timeout", sent=None)
    with pytest.raises(HomeAssistantError) as err:
        await _send(hass, device_id, recipient="sekretariat", topic="T", message="M")
    assert err.value.translation_key == "message_maybe_sent"
    assert client.async_send_message_checked.await_count == 1


async def test_hourly_sending_limit(hass) -> None:
    client, device_id = await _setup(hass, options=ON)
    for _ in range(10):
        await _send(hass, device_id, recipient="sekretariat", topic="T", message="M")
    with pytest.raises(HomeAssistantError) as err:
        await _send(hass, device_id, recipient="sekretariat", topic="T", message="M")
    assert err.value.translation_key == "message_not_sent"
    assert client.async_send_message_checked.await_count == 10


async def test_withdraw_and_recipients_services(hass) -> None:
    client, device_id = await _setup(hass, options=ON)
    await hass.services.async_call(
        DOMAIN, "withdraw_message", {"device_id": device_id, "message_id": "987"}, blocking=True
    )
    client.async_withdraw_message.assert_awaited_once_with("987")
    response = await hass.services.async_call(
        DOMAIN, "get_message_recipients", {"device_id": device_id}, blocking=True, return_response=True
    )
    assert [(r["account_id"], r["group"], r["subjects"]) for r in response["recipients"]] == [
        ("11", "tutors", ["Biologia"]),
        ("12", "teachers", ["Matematyka"]),
        ("13", "secretary", []),
    ]


def _recipient(account_id: str, name: str, group: str, subjects: list[str]) -> MessageRecipientData:
    return MessageRecipientData(
        account_id=account_id, name=name, group=group, group_name=group, subjects=subjects
    )


def test_resolve_recipient_rules() -> None:
    people = [
        _recipient("11", "Nowak Anna", "tutors", ["Biologia"]),
        _recipient("12", "Kowal Jan", "teachers", ["Matematyka", "Fizyka"]),
        _recipient("14", "Lis Ewa", "teachers", ["Język polski"]),
        _recipient("15", "Wróbel Piotr", "teachers", ["Język angielski"]),
        _recipient("13", "sekretariat", "secretary", []),
    ]
    assert resolve_recipient(people, "Wychowawca").account_id == "11"
    assert resolve_recipient(people, "SEKRETARIAT").account_id == "13"
    assert resolve_recipient(people, "fizyka").account_id == "12"
    assert resolve_recipient(people, "matem").account_id == "12"
    assert resolve_recipient(people, "język polski").account_id == "14"
    assert resolve_recipient(people, "wróbel").account_id == "15"
    assert resolve_recipient(people, "15").account_id == "15"
    with pytest.raises(RecipientError) as err:
        resolve_recipient(people, "język")
    assert len(err.value.candidates) == 2
    with pytest.raises(RecipientError):
        resolve_recipient(people, "999")



async def test_option_turns_sending_on_without_a_reload(hass) -> None:
    from homeassistant.helpers import entity_registry as er

    client, device_id = await _setup(hass)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    coordinator = entry.runtime_data
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_message_sending") is None
    sensor_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_unread_messages")

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_MESSAGE_SENDING_ENABLED: True}
    )
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator  # not reloaded
    state = hass.states.get(sensor_id)
    if state is not None and state.state != "unavailable":
        assert state.attributes["sending_enabled"] is True

    response = await _send(hass, device_id, recipient="sekretariat", topic="T", message="M")
    assert response["sent"] is True

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_MESSAGE_SENDING_ENABLED: False}
    )
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError):
        await _send(hass, device_id, recipient="sekretariat", topic="T", message="M")


async def test_sent_list_is_refreshed_right_after_sending(hass) -> None:
    client, device_id = await _setup(hass, options=ON)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    client.async_get_messages.return_value = {
        "data": [
            {"messageId": "987", "receiverName": "Kowal Jan", "topic": "Pytanie",
             "content": "", "sendDate": "2026-10-12 10:00:00"}
        ]
    }
    await _send(hass, device_id, recipient="Matematyka", topic="Pytanie", message="Treść")
    client.async_get_messages.assert_awaited_with(mailbox="outbox", limit=10)
    assert [(m.id, m.receiver_name) for m in entry.runtime_data.data.sent_messages] == [
        ("987", "Kowal Jan")
    ]
