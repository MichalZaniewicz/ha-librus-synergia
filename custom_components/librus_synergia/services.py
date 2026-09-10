"""Service registrations for the Librus Synergia (unofficial) integration.

- `get_message` - see its handler's docstring for why this is deliberately
  a service (explicit user action) rather than anything wired into routine
  polling.
- `refresh` - force an immediate data refresh (e.g. right before a morning
  briefing automation, instead of waiting for the next poll).
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import DOMAIN
from .coordinator import LibrusDataUpdateCoordinator, decode_message_content
from .librus_api import LibrusError

_LOGGER = logging.getLogger(__name__)

SERVICE_GET_MESSAGE = "get_message"
SERVICE_REFRESH = "refresh"

_GET_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("message_id"): cv.string,
        vol.Optional("mailbox", default="inbox"): cv.string,
    }
)

_REFRESH_SCHEMA = vol.Schema({vol.Optional("device_id"): cv.string})


def _resolve_coordinator(hass: HomeAssistant, device_id: str) -> LibrusDataUpdateCoordinator:
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(f"Unknown device: {device_id}")

    entry = next(
        (
            e
            for entry_id in device.config_entries
            if (e := hass.config_entries.async_get_entry(entry_id)) is not None
            and e.domain == DOMAIN
        ),
        None,
    )
    if entry is None:
        raise ServiceValidationError(f"Device {device_id} is not a Librus Synergia device.")
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(f"The Librus Synergia entry for {device_id} isn't loaded.")
    return entry.runtime_data


def async_setup_services(hass: HomeAssistant) -> None:
    """Register this integration's services, once for the whole domain
    regardless of how many config entries (students) exist - guarded so a
    second config entry being set up doesn't try to double-register."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_MESSAGE):
        return

    async def _async_handle_refresh(call: ServiceCall) -> None:
        """Force an immediate data refresh. With `device_id` set, just that
        student; otherwise every loaded Librus Synergia entry. Uses the
        coordinator's debounced request, so spamming it is harmless."""
        device_id = call.data.get("device_id")
        if device_id:
            coordinators = [_resolve_coordinator(hass, device_id)]
        else:
            coordinators = [
                entry.runtime_data
                for entry in hass.config_entries.async_entries(DOMAIN)
                if entry.state is ConfigEntryState.LOADED
            ]
        for coordinator in coordinators:
            await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, _async_handle_refresh, schema=_REFRESH_SCHEMA
    )

    async def _async_handle_get_message(call: ServiceCall) -> ServiceResponse:
        """Fetch ONE message's full, untruncated content.

        Deliberately a service, not a sensor attribute or coordinator
        field: CONFIRMED live (2026-09-06) that fetching a single message
        marks it read server-side on Librus (readDate flips from null to a
        real timestamp on the very next poll) - exactly like opening a
        message in the real Librus app. This must only ever run when a
        person has explicitly asked to read ONE specific message (e.g.
        clicking it in a dashboard card) - never from the coordinator's
        routine polling, which only ever uses the list/count endpoints and
        never marks anything read (see LibrusApiClient's module comment
        above async_get_message).
        """
        coordinator = _resolve_coordinator(hass, call.data["device_id"])
        message_id = call.data["message_id"]
        mailbox = call.data["mailbox"]
        try:
            raw = await coordinator.async_fetch_message(mailbox, message_id)
        except LibrusError as err:
            raise HomeAssistantError(f"Failed to fetch message {message_id}: {err}") from err

        data = raw.get("data")
        if not isinstance(data, dict):
            raise HomeAssistantError(f"Unexpected response fetching message {message_id}.")

        sender_name = data.get("senderName") or (
            f"{data.get('senderFirstName', '')} {data.get('senderLastName', '')}".strip()
        )
        return {
            "id": message_id,
            "mailbox": mailbox,
            "sender": sender_name,
            "topic": data.get("topic", ""),
            "content": decode_message_content(data.get("Message", "")),
            "send_date": data.get("sendDate"),
            "read_date": data.get("readDate"),
            "has_attachment": bool(data.get("attachments")),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_MESSAGE,
        _async_handle_get_message,
        schema=_GET_MESSAGE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove this integration's services - call only once the LAST config
    entry is about to be unloaded (see __init__.py::async_unload_entry),
    so a second student's entry doesn't lose the service while the first
    one is just being reloaded."""
    for service in (SERVICE_GET_MESSAGE, SERVICE_REFRESH):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
