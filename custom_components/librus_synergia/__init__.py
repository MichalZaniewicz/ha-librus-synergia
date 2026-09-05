"""The Librus Synergia (unofficial) integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_COOKIES,
    CONF_SESSION_LOGGED_IN_AT,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import LibrusDataUpdateCoordinator
from .librus_api import LibrusApiClient, LibrusSessionData

type LibrusConfigEntry = ConfigEntry[LibrusDataUpdateCoordinator]


def librus_device_info(entry: LibrusConfigEntry) -> DeviceInfo:
    """Shared device descriptor for every Librus Synergia entity (one device
    per config entry, i.e. per student)."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Librus",
        model="Synergia",
    )


async def async_setup_entry(hass: HomeAssistant, entry: LibrusConfigEntry) -> bool:
    """Set up the integration from a config entry."""

    def _persist_session(session_data: LibrusSessionData) -> None:
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_COOKIES: session_data.cookies,
                CONF_SESSION_LOGGED_IN_AT: session_data.logged_in_at,
            },
        )

    client = LibrusApiClient(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        on_session_update=_persist_session,
    )
    client.import_session(
        LibrusSessionData(
            cookies=entry.data.get(CONF_COOKIES, []),
            logged_in_at=entry.data.get(CONF_SESSION_LOGGED_IN_AT, 0.0),
        )
    )
    # A brand-new entry (just created by the config flow) already has a
    # fresh session from the login the flow itself performed - only force a
    # login here if that session looks stale (e.g. a HA restart long after
    # the last refresh, or the cookies didn't come through intact).
    if not client.is_session_valid():
        await client.async_login(entry.data[CONF_PASSWORD])

    scan_interval_minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
    coordinator = LibrusDataUpdateCoordinator(
        hass, entry, client, timedelta(minutes=scan_interval_minutes)
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LibrusConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: LibrusConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
