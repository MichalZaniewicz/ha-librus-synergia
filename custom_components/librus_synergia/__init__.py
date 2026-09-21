"""The Librus Synergia (unofficial) integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_COOKIES,
    CONF_SESSION_LOGGED_IN_AT,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    OPTIONAL_ENDPOINT_LABELS,
    PLATFORMS,
)
from .coordinator import (
    LibrusDataUpdateCoordinator,
    optional_endpoint_issue_id,
    school_year_issue_id,
)
from .librus_api import LibrusApiClient, LibrusSessionData
from .services import async_setup_services, async_unload_services

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

    # A dedicated session per entry, NOT the hass-wide `async_get_
    # clientsession(hass)` - this client's auth lives entirely in the
    # session's cookie jar, keyed only by domain. Sharing one session
    # across multiple students (multiple config entries) means their
    # `oauth_token` cookies collide in that one jar - whichever entry
    # last logged in/re-imported its cookies "wins" it for every entry's
    # next request, so two children's coordinators polling independently
    # end up intermittently swapping data. See LibrusApiClient's docstring.
    client = LibrusApiClient(
        async_create_clientsession(hass),
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

    options_at_setup = dict(entry.options)

    async def _async_update_listener(hass: HomeAssistant, entry: LibrusConfigEntry) -> None:
        """Reload the entry only when its OPTIONS changed.

        HA fires update listeners for ANY change to the entry, including
        `_persist_session` above writing fresh session cookies into
        `entry.data` after every re-login (roughly daily, plus every
        mid-cycle session-expiry recovery). Reloading on those tears the
        whole entry down - closing this entry's dedicated aiohttp session -
        while the very request that triggered the re-login is still about to
        be retried on it, so the retry died with `RuntimeError: Session is
        closed` (not a `LibrusError`, so nothing caught it) and the dashboard
        showed a broken timetable/agenda until the next manual refresh.
        Reauth/reconfigure already reload themselves via
        `async_update_reload_and_abort`, so data-only changes never need
        this listener to do it for them.
        """
        if dict(entry.options) == options_at_setup:
            return
        await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_setup_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LibrusConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # This entry's own dedicated session (see async_setup_entry) -
        # close it here or a reload leaks one aiohttp session/connector
        # per reload, since a fresh one is created on every setup.
        await entry.runtime_data.client.async_close()
    # Services are domain-wide, not per-entry - only drop them once the
    # LAST Librus Synergia entry (student) is going away, so a second
    # entry doesn't lose `get_message` while the first is just reloading.
    remaining = [e for e in hass.config_entries.async_entries(DOMAIN) if e.entry_id != entry.entry_id]
    if unloaded and not remaining:
        async_unload_services(hass)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: LibrusConfigEntry) -> None:
    """Called once when a config entry is actually being deleted (not on a
    plain reload) - clears any repair issues raised for it, so a removed
    student doesn't leave a phantom "school year rollover" or "endpoint
    degraded" repair sitting in the issue registry forever (issue_registry
    entries have no lifecycle tie to a config entry on their own). Computed
    from the entry id directly rather than via `entry.runtime_data`, since
    the coordinator is already gone by the time this runs (unload happens
    first)."""
    issue_ids = [school_year_issue_id(entry.entry_id)] + [
        optional_endpoint_issue_id(entry.entry_id, label) for label in OPTIONAL_ENDPOINT_LABELS
    ]
    for issue_id in issue_ids:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
