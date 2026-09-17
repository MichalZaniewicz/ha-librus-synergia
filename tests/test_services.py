"""Tests for the Librus Synergia integration's HA services (`get_message`)."""

from __future__ import annotations

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from custom_components.librus_synergia.const import DOMAIN
from custom_components.librus_synergia.librus_api import LibrusInvalidCredentialsError

from .conftest import build_mock_client, setup_integration

GOOD_MESSAGE_PAYLOAD = {
    "data": {
        "senderName": "Marciszak Amelia",
        "topic": "Zebranie z rodzicami",
        # base64 for "Dzien dobry, zapraszam na zebranie."
        "Message": "RHppZW4gZG9icnksIHphcHJhc3phbSBuYSB6ZWJyYW5pZS4=",
        "sendDate": "2026-09-04T17:47:10",
        "readDate": "2026-09-06T18:22:51",
        "attachments": [],
    }
}

GOOD_GRADES_PAYLOAD = {
    "Grades": [
        {
            "Id": 1,
            "Grade": "4",
            "Category": {"Id": 10},
            "Subject": {"Id": 100},
            "Semester": 1,
            "AddDate": "2026-09-16 10:58:09",
            "Comments": [],
        },
        {
            "Id": 2,
            "Grade": "6",
            "Category": {"Id": 11},
            "Subject": {"Id": 200},
            "Semester": 1,
            "AddDate": "2026-09-15 14:02:06",
            "Comments": [],
        },
    ]
}
GRADES_SUBJECTS_PAYLOAD = {
    "Subjects": [{"Id": 100, "Name": "Biologia"}, {"Id": 200, "Name": "Geografia"}]
}
GRADES_CATEGORIES_PAYLOAD = {
    "Categories": [
        {"Id": 10, "Name": "praca na lekcji", "CountToTheAverage": True, "Weight": 3},
        {"Id": 11, "Name": "odpowiedź ustna", "CountToTheAverage": True, "Weight": 3},
    ]
}


def _device_id(hass, entry) -> str:
    # async_get_device(identifiers=...) is deprecated (identifiers are no
    # longer guaranteed unique across config entries) - the modern,
    # unambiguous lookup is by identifier scoped to the owning entry.
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert device is not None
    return device.id


async def test_get_message_service_returns_decoded_content(hass) -> None:
    client = build_mock_client()
    client.async_get_message.return_value = GOOD_MESSAGE_PAYLOAD
    entry = await setup_integration(hass, client)

    response = await hass.services.async_call(
        DOMAIN,
        "get_message",
        {"device_id": _device_id(hass, entry), "message_id": "186536"},
        blocking=True,
        return_response=True,
    )

    assert response["sender"] == "Marciszak Amelia"
    assert response["topic"] == "Zebranie z rodzicami"
    assert response["content"] == "Dzien dobry, zapraszam na zebranie."
    assert response["read_date"] == "2026-09-06T18:22:51"
    client.async_get_message.assert_awaited_once_with("inbox", "186536")


async def test_get_message_service_mailbox_field_passed_through(hass) -> None:
    """New (2026-09-06): the `mailbox` field lets a card fetch full
    content for a substitutions/alerts message, not just inbox."""
    client = build_mock_client()
    client.async_get_message.return_value = GOOD_MESSAGE_PAYLOAD
    entry = await setup_integration(hass, client)

    response = await hass.services.async_call(
        DOMAIN,
        "get_message",
        {"device_id": _device_id(hass, entry), "message_id": "1", "mailbox": "substitutions"},
        blocking=True,
        return_response=True,
    )

    assert response["mailbox"] == "substitutions"
    client.async_get_message.assert_awaited_once_with("substitutions", "1")


async def test_get_message_service_unknown_device_raises(hass) -> None:
    client = build_mock_client()
    await setup_integration(hass, client)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "get_message",
            {"device_id": "not-a-real-device", "message_id": "186536"},
            blocking=True,
            return_response=True,
        )


async def test_get_message_service_client_error_surfaces_as_hass_error(hass) -> None:
    """A genuinely failed fetch (e.g. forced re-login also rejected) must
    surface as a real error to whoever clicked the message, not silently
    swallow it or crash uncaught."""
    client = build_mock_client()
    client.async_get_message.side_effect = LibrusInvalidCredentialsError("bad password")
    entry = await setup_integration(hass, client)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "get_message",
            {"device_id": _device_id(hass, entry), "message_id": "186536"},
            blocking=True,
            return_response=True,
        )


async def test_get_message_service_unregistered_after_last_entry_unloaded(hass) -> None:
    client = build_mock_client()
    entry = await setup_integration(hass, client)
    assert hass.services.has_service(DOMAIN, "get_message")

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, "get_message")


async def test_refresh_service_triggers_a_coordinator_update(hass) -> None:
    client = build_mock_client()
    entry = await setup_integration(hass, client)
    assert hass.services.has_service(DOMAIN, "refresh")
    before = client.async_get_grades.await_count

    await hass.services.async_call(
        DOMAIN, "refresh", {"device_id": _device_id(hass, entry)}, blocking=True
    )
    await hass.async_block_till_done()

    assert client.async_get_grades.await_count > before


async def test_refresh_service_without_device_refreshes_all(hass) -> None:
    client = build_mock_client()
    await setup_integration(hass, client)
    before = client.async_get_grades.await_count

    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    await hass.async_block_till_done()

    assert client.async_get_grades.await_count > before


async def test_get_grades_service_returns_every_subject_sorted_newest_first(hass) -> None:
    client = build_mock_client(
        async_get_grades=GOOD_GRADES_PAYLOAD,
        async_get_subjects=GRADES_SUBJECTS_PAYLOAD,
        async_get_grade_categories=GRADES_CATEGORIES_PAYLOAD,
    )
    entry = await setup_integration(hass, client)

    response = await hass.services.async_call(
        DOMAIN,
        "get_grades",
        {"device_id": _device_id(hass, entry)},
        blocking=True,
        return_response=True,
    )

    assert response["count"] == 2
    assert [g["subject"] for g in response["grades"]] == ["Biologia", "Geografia"]
    assert response["grades"][0]["value"] == "4"
    assert response["grades"][0]["category"] == "praca na lekcji"


async def test_get_grades_service_filters_by_subject_id(hass) -> None:
    client = build_mock_client(
        async_get_grades=GOOD_GRADES_PAYLOAD,
        async_get_subjects=GRADES_SUBJECTS_PAYLOAD,
        async_get_grade_categories=GRADES_CATEGORIES_PAYLOAD,
    )
    entry = await setup_integration(hass, client)

    response = await hass.services.async_call(
        DOMAIN,
        "get_grades",
        {"device_id": _device_id(hass, entry), "subject_id": 200},
        blocking=True,
        return_response=True,
    )

    assert response["count"] == 1
    assert response["grades"][0]["subject"] == "Geografia"


async def test_get_grades_service_unknown_device_raises(hass) -> None:
    client = build_mock_client()
    await setup_integration(hass, client)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "get_grades",
            {"device_id": "not-a-real-device"},
            blocking=True,
            return_response=True,
        )
