"""Switch platform: pause or resume the automatic weekly AI summary (see
ai_summary.py), and allow or block sending messages (Message sending, beta)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LibrusConfigEntry, librus_device_info
from .ai_summary import LibrusWeeklySummary
from .const import CONF_MESSAGE_SENDING_ENABLED, DEFAULT_MESSAGE_SENDING_ENABLED, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LibrusConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """The Message sending switch, and the weekly summary switch only while
    the summary is configured."""
    async_add_entities([LibrusMessageSendingSwitch(hass, entry)])
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


class LibrusMessageSendingSwitch(SwitchEntity):
    """On: the send_message / withdraw_message services may write to
    Wiadomości. Off (the default): they refuse (a dry run still works).
    The same setting as "Allow sending messages" under Configure - stored in
    the entry's options, read live, so flipping it doesn't reload anything."""

    _attr_has_entity_name = True
    _attr_translation_key = "message_sending"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: LibrusConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_message_sending"
        self._attr_device_info = librus_device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Follow a change made under Configure."""

        async def _changed(hass: HomeAssistant, entry: LibrusConfigEntry) -> None:
            self.async_write_ha_state()

        self.async_on_remove(self._entry.add_update_listener(_changed))

    @property
    def is_on(self) -> bool:
        return bool(
            self._entry.options.get(CONF_MESSAGE_SENDING_ENABLED, DEFAULT_MESSAGE_SENDING_ENABLED)
        )

    @property
    def icon(self) -> str:
        return "mdi:email-send-outline" if self.is_on else "mdi:email-off-outline"

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._set(False)

    def _set(self, enabled: bool) -> None:
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, CONF_MESSAGE_SENDING_ENABLED: enabled}
        )
        self.async_write_ha_state()
