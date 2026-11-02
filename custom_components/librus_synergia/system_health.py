"""Librus Synergia in Settings > System > Repairs > System information.

The first thing to look at when something seems off: can Home Assistant
reach Librus at all, how each student's connection is doing, when the data
was last fetched and which parts of Librus are failing. No names - this
page tends to be pasted into bug reports.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components import system_health
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

try:
    from librus_synergia import __version__ as LIBRARY_VERSION
except ImportError:  # pragma: no cover - the library always has it
    LIBRARY_VERSION = "?"


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register the system health info."""
    register.async_register_info(system_health_info, "/config/integrations/integration/librus_synergia")


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """The info shown on the system information page."""
    entries = hass.config_entries.async_entries(DOMAIN)
    coordinators = [
        entry.runtime_data
        for entry in entries
        if entry.state is ConfigEntryState.LOADED and getattr(entry, "runtime_data", None)
    ]
    statuses = [str(c.status) for c in coordinators]
    updates = [c.last_success_at for c in coordinators if c.last_success_at is not None]
    degraded = sorted({label for c in coordinators for label in c.degraded_endpoints})
    last_update: datetime | str = min(updates) if updates else "-"
    return {
        "students": f"{len(coordinators)} / {len(entries)}",
        "connection": ", ".join(statuses) if statuses else "-",
        "last_update": last_update,
        "degraded_endpoints": ", ".join(degraded) if degraded else "-",
        "library_version": LIBRARY_VERSION,
        "can_reach_synergia": system_health.async_check_can_reach_url(
            hass, "https://synergia.librus.pl/"
        ),
        "can_reach_login": system_health.async_check_can_reach_url(
            hass, "https://api.librus.pl/"
        ),
        "can_reach_messages": system_health.async_check_can_reach_url(
            hass, "https://wiadomosci.librus.pl/"
        ),
    }
