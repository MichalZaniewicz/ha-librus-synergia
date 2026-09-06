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
