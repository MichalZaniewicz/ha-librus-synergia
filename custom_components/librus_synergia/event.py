"""Event entities: new grades, notes, absences, timetable changes and more.

Each entity mirrors one of the integration's existing bus events
(`librus_synergia_new_grade` etc.) for its own student, so an automation
can be built in the UI ("When Ola's New grade event fires") without
blueprints or typing event names, and every occurrence lands in the
logbook. The bus events themselves are unchanged. The event type says what
kind of thing happened where that matters for automations: a positive /
negative / neutral note, an excused / unexcused absence, a cancelled
lesson / substitution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LibrusConfigEntry, librus_device_info
from .const import (
    EVENT_ACHIEVEMENT_UNLOCKED,
    EVENT_AGENDA_CHANGED,
    EVENT_FORECAST_CHANGED,
    EVENT_JUSTIFICATION_STATUS,
    EVENT_NEW_ABSENCE,
    EVENT_NEW_ANNOUNCEMENT,
    EVENT_NEW_GRADE,
    EVENT_NEW_HOMEWORK,
    EVENT_NEW_HOMEWORK_ASSIGNMENT,
    EVENT_NEW_MESSAGE,
    EVENT_NEW_NOTE,
    EVENT_TIMETABLE_CHANGED,
)


@dataclass(frozen=True, slots=True)
class LibrusEventDescription:
    key: str
    bus_event: str
    event_types: tuple[str, ...]
    icon: str
    # Picks the event type from the bus event's data.
    event_type: Callable[[dict[str, Any]], str]


def _note_type(data: dict[str, Any]) -> str:
    sentiment = data.get("sentiment")
    return sentiment if sentiment in ("positive", "negative") else "neutral"


DESCRIPTIONS: tuple[LibrusEventDescription, ...] = (
    LibrusEventDescription(
        "grade", EVENT_NEW_GRADE, ("new_grade",), "mdi:numeric-6-box-outline", lambda d: "new_grade"
    ),
    LibrusEventDescription(
        "note", EVENT_NEW_NOTE, ("positive", "negative", "neutral"), "mdi:note-text-outline", _note_type
    ),
    LibrusEventDescription(
        "absence",
        EVENT_NEW_ABSENCE,
        ("unexcused", "excused"),
        "mdi:account-cancel-outline",
        lambda d: "excused" if d.get("excused") else "unexcused",
    ),
    LibrusEventDescription(
        "timetable_change",
        EVENT_TIMETABLE_CHANGED,
        ("canceled", "substitution"),
        "mdi:calendar-alert",
        lambda d: "substitution" if d.get("kind") == "substitution" else "canceled",
    ),
    LibrusEventDescription(
        "homework",
        EVENT_NEW_HOMEWORK_ASSIGNMENT,
        ("new_homework",),
        "mdi:notebook-edit-outline",
        lambda d: "new_homework",
    ),
    LibrusEventDescription(
        "agenda", EVENT_NEW_HOMEWORK, ("new_entry",), "mdi:calendar-plus", lambda d: "new_entry"
    ),
    LibrusEventDescription(
        "agenda_change",
        EVENT_AGENDA_CHANGED,
        ("changed", "removed"),
        "mdi:calendar-edit",
        lambda d: "removed" if d.get("kind") == "removed" else "changed",
    ),
    LibrusEventDescription(
        "justification",
        EVENT_JUSTIFICATION_STATUS,
        ("accepted", "rejected", "changed"),
        "mdi:file-document-check-outline",
        lambda d: "accepted" if d.get("accepted") else "rejected" if d.get("rejected") else "changed",
    ),
    LibrusEventDescription(
        "announcement",
        EVENT_NEW_ANNOUNCEMENT,
        ("new_announcement",),
        "mdi:bullhorn-outline",
        lambda d: "new_announcement",
    ),
    LibrusEventDescription(
        "message", EVENT_NEW_MESSAGE, ("new_message",), "mdi:email-outline", lambda d: "new_message"
    ),
    LibrusEventDescription(
        "forecast",
        EVENT_FORECAST_CHANGED,
        ("up", "down"),
        "mdi:crystal-ball",
        lambda d: "up" if d.get("direction") == "up" else "down",
    ),
    LibrusEventDescription(
        "achievement",
        EVENT_ACHIEVEMENT_UNLOCKED,
        ("unlocked",),
        "mdi:trophy-outline",
        lambda d: "unlocked",
    ),
)

# Not useful as event attributes: the routing key and the raw item id.
_DROP = frozenset({"entry_id"})


class LibrusEventEntity(EventEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: LibrusConfigEntry, description: LibrusEventDescription) -> None:
        self._entry_id = entry.entry_id
        self._description = description
        self._attr_translation_key = f"{description.key}_event"
        self._attr_unique_id = f"{entry.entry_id}_{description.key}_event"
        self._attr_event_types = list(description.event_types)
        self._attr_icon = description.icon
        self._attr_device_info = librus_device_info(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.hass.bus.async_listen(self._description.bus_event, self._async_handle)
        )

    @callback
    def _async_handle(self, event: Event) -> None:
        if event.data.get("entry_id") != self._entry_id:
            return
        attributes = {k: v for k, v in event.data.items() if k not in _DROP}
        self._trigger_event(self._description.event_type(dict(event.data)), attributes)
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant, entry: LibrusConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(LibrusEventEntity(entry, d) for d in DESCRIPTIONS)
