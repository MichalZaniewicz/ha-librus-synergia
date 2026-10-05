"""Button platform: generate the weekly AI summary on demand (see ai_summary.py)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    """Add the button only while the weekly summary is configured."""
    summary = entry.runtime_data.weekly_summary
    if summary is not None:
        async_add_entities([LibrusWeeklySummaryButton(summary, entry)])
        return
    registry = er.async_get(hass)
    if entity_id := registry.async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_weekly_summary_generate"
    ):
        registry.async_remove(entity_id)


class LibrusWeeklySummaryButton(ButtonEntity):
    """Write the weekly summary now, whether or not this week's is done."""

    _attr_has_entity_name = True
    _attr_translation_key = "weekly_summary_generate"
    _attr_icon = "mdi:creation-outline"

    def __init__(self, summary: LibrusWeeklySummary, entry: LibrusConfigEntry) -> None:
        self._summary = summary
        self._attr_unique_id = f"{entry.entry_id}_weekly_summary_generate"
        self._attr_device_info = librus_device_info(entry)

    async def async_press(self) -> None:
        """Generate; a failure surfaces in the UI as an error toast."""
        await self._summary.async_generate()
