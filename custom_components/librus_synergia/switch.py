"""Switch platform: pause or resume the automatic weekly AI summary (see ai_summary.py)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LibrusConfigEntry, librus_device_info
from .ai_summary import LibrusWeeklySummary
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LibrusConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the switch only while the weekly summary is configured."""
    summary = entry.runtime_data.weekly_summary
    if summary is not None:
        async_add_entities([LibrusWeeklySummarySwitch(summary, entry)])
        return
    registry = er.async_get(hass)
    if entity_id := registry.async_get_entity_id(
        "switch", DOMAIN, f"{entry.entry_id}_weekly_summary_enabled"
    ):
        registry.async_remove(entity_id)


class LibrusWeeklySummarySwitch(SwitchEntity):
    """On: the summary is written by itself every week. Off: only from the button."""

    _attr_has_entity_name = True
    _attr_translation_key = "weekly_summary_enabled"
    _attr_should_poll = False

    def __init__(self, summary: LibrusWeeklySummary, entry: LibrusConfigEntry) -> None:
        self._summary = summary
        self._attr_unique_id = f"{entry.entry_id}_weekly_summary_enabled"
        self._attr_device_info = librus_device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Follow the summary's updates."""
        self.async_on_remove(self._summary.async_add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self._summary.enabled

    @property
    def icon(self) -> str:
        """Filled sparkles while on, outlined while paused."""
        return "mdi:creation" if self._summary.enabled else "mdi:creation-outline"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._summary.async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._summary.async_set_enabled(False)
