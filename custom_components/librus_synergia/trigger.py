"""Triggers for the automation editor: "Librus: New grade" and friends.

One trigger per bus event of the integration, with the filters people
actually write templates for (a subject, "only a 1 or a 2", "only
unexcused", ...). Picked from the automation editor's list like the
built-in ones, so nobody has to know the event names. The bus events
themselves (and the event entities) are unchanged.

The target picks the student(s) - a Librus device, or an area / label /
entity leading to one. No target means every student. The trigger
variables carry the event's data (`trigger.subject`, `trigger.value`, ...),
the Librus item id as `trigger.item_id` (`trigger.id` is the automation's
own trigger id), and `trigger.event_type`.

Needs the trigger platform of Home Assistant 2025.12+; on an older core
this module defines nothing and nothing changes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import logging
from typing import Any

from homeassistant.const import CONF_OPTIONS, CONF_TARGET
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    EVENT_ACHIEVEMENT_UNLOCKED,
    EVENT_AGENDA_CHANGED,
    EVENT_FORECAST_CHANGED,
    EVENT_GRADE_CHANGED,
    EVENT_JUSTIFICATION_STATUS,
    EVENT_LIBRARY_CHANGED,
    EVENT_MESSAGE_READ,
    EVENT_NEW_ABSENCE,
    EVENT_NEW_ANNOUNCEMENT,
    EVENT_NEW_GRADE,
    EVENT_NEW_HOMEWORK,
    EVENT_NEW_HOMEWORK_ASSIGNMENT,
    EVENT_NEW_MESSAGE,
    EVENT_NEW_NOTE,
    EVENT_NEW_SCHOOL_DOCUMENT,
    EVENT_NEW_SCHOOL_TRIP,
    EVENT_TIMETABLE_CHANGED,
    EVENT_WEEKLY_SUMMARY,
)

try:  # Home Assistant 2026.10+ validates with probatio, older ones voluptuous.
    import probatio as vol
except ImportError:  # pragma: no cover - older cores
    import voluptuous as vol

_LOGGER = logging.getLogger(__name__)

# Keys Home Assistant itself puts in `trigger` - event data must not hide them.
_RESERVED = frozenset({"id", "idx", "alias", "platform", "description", "entity_id"})


def _words(value: Any) -> list[str]:
    """A comma list of words, lowercased, without Polish letters."""
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    return [w for w in (fold(str(i)) for i in items) if w]


def fold(text: str | None) -> str:
    """Lowercase without Polish letters, for forgiving name matching."""
    table = str.maketrans("ąćęłńóśźż", "acelnoszz")
    return " ".join((text or "").lower().translate(table).split())


def _contains_any(words: list[str], *texts: Any) -> bool:
    haystack = " ".join(fold(str(t)) for t in texts if t)
    return any(word in haystack for word in words)


def _in_list(value: Any, allowed: list[str] | None) -> bool:
    return not allowed or value in allowed


# --- filters: (event data, options) -> bool -------------------------------

_GRADE_KINDS = {
    "normal": ("normal",),
    "proposition": ("semester_proposition", "final_proposition"),
    "semester_final": ("semester", "final"),
    "text": ("text",),
    "descriptive": ("descriptive",),
    "point": ("point",),
    "behaviour": ("behaviour",),
}


def _grade_number(data: Mapping[str, Any]) -> float | None:
    if data.get("kind") in ("point", "behaviour", "descriptive", "text"):
        return None
    from .coordinator import parse_grade_value  # heavy module, only when needed

    return parse_grade_value(str(data.get("value") or ""))


def _grade_filter(data: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    if kinds := options.get("kind"):
        allowed = {k for kind in kinds for k in _GRADE_KINDS.get(kind, (kind,))}
        if data.get("kind", "normal") not in allowed:
            return False
    if (subjects := _words(options.get("subject"))) and not _contains_any(
        subjects, data.get("subject")
    ):
        return False
    at_most, at_least = options.get("grade_at_most"), options.get("grade_at_least")
    if at_most is not None or at_least is not None:
        number = _grade_number(data)
        if number is None:
            return False
        if at_most is not None and number > float(at_most):
            return False
        if at_least is not None and number < float(at_least):
            return False
    return True


def _note_filter(data: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    sentiment = data.get("sentiment")
    if sentiment not in ("positive", "negative"):
        sentiment = "neutral"
    return _in_list(sentiment, options.get("sentiment"))


def _absence_filter(data: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    return not options.get("only_unexcused") or not data.get("excused")


def _message_filter(data: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    return _in_list(data.get("mailbox") or "inbox", options.get("mailbox"))


def _agenda_filter(data: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    if (words := _words(options.get("category"))) and not _contains_any(
        words, data.get("category"), data.get("content")
    ):
        return False
    return not (subjects := _words(options.get("subject"))) or _contains_any(
        subjects, data.get("subject")
    )


def _subject_filter(data: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    return not (subjects := _words(options.get("subject"))) or _contains_any(
        subjects, data.get("subject")
    )


def _timetable_filter(data: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    change = data.get("change") or data.get("kind")
    return _in_list(change, options.get("change")) and _subject_filter(data, options)


def _agenda_change_filter(data: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    return _in_list(data.get("kind"), options.get("change"))


def _grade_change_filter(data: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    return _in_list(data.get("change"), options.get("change")) and _subject_filter(data, options)


def _justification_filter(data: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    outcome = "accepted" if data.get("accepted") else "rejected" if data.get("rejected") else "changed"
    return _in_list(outcome, options.get("outcome"))


def _forecast_filter(data: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    return _in_list(data.get("direction"), options.get("direction")) and _subject_filter(
        data, options
    )


def _library_filter(data: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    return _in_list(data.get("action"), options.get("action"))


def _summary_filter(data: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    audience = options.get("audience") or "any"
    return audience == "any" or data.get("audience") == audience


def _no_filter(_data: Mapping[str, Any], _options: Mapping[str, Any]) -> bool:
    return True


_LIST = vol.All(cv.ensure_list, [cv.string])
_GRADE = vol.All(vol.Coerce(float), vol.Range(min=0, max=6))

# key -> (bus event, options schema, filter)
_SPECS: dict[
    str, tuple[str, dict[Any, Any], Callable[[Mapping[str, Any], Mapping[str, Any]], bool]]
] = {
    "new_grade": (
        EVENT_NEW_GRADE,
        {
            vol.Optional("kind"): vol.All(cv.ensure_list, [vol.In(list(_GRADE_KINDS))]),
            vol.Optional("subject"): cv.string,
            vol.Optional("grade_at_most"): _GRADE,
            vol.Optional("grade_at_least"): _GRADE,
        },
        _grade_filter,
    ),
    "grade_changed": (
        EVENT_GRADE_CHANGED,
        {
            vol.Optional("change"): vol.All(cv.ensure_list, [vol.In(["changed", "removed"])]),
            vol.Optional("subject"): cv.string,
        },
        _grade_change_filter,
    ),
    "new_note": (
        EVENT_NEW_NOTE,
        {
            vol.Optional("sentiment"): vol.All(
                cv.ensure_list, [vol.In(["positive", "negative", "neutral"])]
            )
        },
        _note_filter,
    ),
    "new_absence": (
        EVENT_NEW_ABSENCE,
        {vol.Optional("only_unexcused", default=False): cv.boolean},
        _absence_filter,
    ),
    "new_message": (
        EVENT_NEW_MESSAGE,
        {
            vol.Optional("mailbox"): vol.All(
                cv.ensure_list,
                [vol.In(["inbox", "substitutions", "alerts", "justifications"])],
            )
        },
        _message_filter,
    ),
    "new_announcement": (EVENT_NEW_ANNOUNCEMENT, {}, _no_filter),
    "new_agenda_entry": (
        EVENT_NEW_HOMEWORK,
        {vol.Optional("category"): cv.string, vol.Optional("subject"): cv.string},
        _agenda_filter,
    ),
    "new_homework": (
        EVENT_NEW_HOMEWORK_ASSIGNMENT,
        {vol.Optional("subject"): cv.string},
        _subject_filter,
    ),
    "timetable_changed": (
        EVENT_TIMETABLE_CHANGED,
        {
            vol.Optional("change"): vol.All(
                cv.ensure_list,
                [vol.In(["canceled", "substitution", "room_change", "moved"])],
            ),
            vol.Optional("subject"): cv.string,
        },
        _timetable_filter,
    ),
    "agenda_changed": (
        EVENT_AGENDA_CHANGED,
        {vol.Optional("change"): vol.All(cv.ensure_list, [vol.In(["changed", "removed"])])},
        _agenda_change_filter,
    ),
    "justification_status": (
        EVENT_JUSTIFICATION_STATUS,
        {
            vol.Optional("outcome"): vol.All(
                cv.ensure_list, [vol.In(["accepted", "rejected", "changed"])]
            )
        },
        _justification_filter,
    ),
    "forecast_changed": (
        EVENT_FORECAST_CHANGED,
        {
            vol.Optional("direction"): vol.All(cv.ensure_list, [vol.In(["up", "down"])]),
            vol.Optional("subject"): cv.string,
        },
        _forecast_filter,
    ),
    "new_school_trip": (EVENT_NEW_SCHOOL_TRIP, {}, _no_filter),
    "new_school_document": (EVENT_NEW_SCHOOL_DOCUMENT, {}, _no_filter),
    "library_changed": (
        EVENT_LIBRARY_CHANGED,
        {vol.Optional("action"): vol.All(cv.ensure_list, [vol.In(["lent", "returned"])])},
        _library_filter,
    ),
    "message_read": (EVENT_MESSAGE_READ, {}, _no_filter),
    "achievement_unlocked": (EVENT_ACHIEVEMENT_UNLOCKED, {}, _no_filter),
    "weekly_summary": (
        EVENT_WEEKLY_SUMMARY,
        {vol.Optional("audience", default="any"): vol.In(["any", "parent", "student"])},
        _summary_filter,
    ),
}

_TARGET_KEYS = ("entity_id", "device_id", "area_id", "floor_id", "label_id")


def _ids(value: Any) -> set[str]:
    if not value:
        return set()
    return {str(v) for v in (value if isinstance(value, (list, tuple, set)) else [value])}


@callback
def student_entry_ids(hass: HomeAssistant, target: Mapping[str, Any] | None) -> set[str] | None:
    """The Librus config entries a target points at; None = every student.

    A device, an area / floor / label holding the device, or any of the
    student's entities lead to that student."""
    if not target or not any(target.get(key) for key in _TARGET_KEYS):
        return None
    dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)
    device_ids = _ids(target.get("device_id"))
    for entity_id in _ids(target.get("entity_id")):
        if (entity := ent_reg.async_get(entity_id)) is not None and entity.device_id:
            device_ids.add(entity.device_id)
    area_ids = _ids(target.get("area_id"))
    if floor_ids := _ids(target.get("floor_id")):
        area_reg = ar.async_get(hass)
        for floor_id in floor_ids:
            area_ids.update(area.id for area in ar.async_entries_for_floor(area_reg, floor_id))
    for area_id in area_ids:
        device_ids.update(d.id for d in dr.async_entries_for_area(dev_reg, area_id))
    for label_id in _ids(target.get("label_id")):
        device_ids.update(d.id for d in dr.async_entries_for_label(dev_reg, label_id))
    own = {entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)}
    entries: set[str] = set()
    for device_id in device_ids:
        if (device := dev_reg.async_get(device_id)) is not None:
            entries.update(set(device.config_entries) & own)
    return entries


def trigger_payload(event_type: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """The event data as trigger variables, without hiding HA's own keys."""
    payload = {
        (f"item_{key}" if key in _RESERVED else key): value for key, value in data.items()
    }
    payload["event_type"] = event_type
    return payload


try:
    from homeassistant.helpers.trigger import (
        Trigger,
        TriggerActionRunner,
        TriggerConfig,
    )
except ImportError:  # pragma: no cover - Home Assistant before 2025.12
    Trigger = None  # type: ignore[assignment,misc]


# An older Trigger class (no runner) can't host these: define nothing then.
if Trigger is not None and hasattr(Trigger, "async_attach_runner"):

    class LibrusEventTrigger(Trigger):
        """Fires on one Librus bus event that passes the filters."""

        _key: str

        @classmethod
        async def async_validate_config(
            cls, hass: HomeAssistant, config: ConfigType
        ) -> ConfigType:
            options_schema = vol.Schema(_SPECS[cls._key][1])
            schema = vol.Schema(
                {
                    vol.Optional(CONF_TARGET): cv.TARGET_FIELDS,
                    vol.Optional(CONF_OPTIONS, default={}): options_schema,
                }
            )
            return schema(config)  # type: ignore[no-any-return]

        def __init__(self, hass: HomeAssistant, config: TriggerConfig) -> None:
            super().__init__(hass, config)
            self._target = config.target or {}
            self._options = config.options or {}

        async def async_attach_runner(
            self, run_action: TriggerActionRunner, *args: Any, **kwargs: Any
        ) -> CALLBACK_TYPE:
            bus_event, _schema, passes = _SPECS[self._key]
            hass, target, options = self._hass, self._target, self._options

            @callback
            def _wanted(data: Mapping[str, Any]) -> bool:
                entries = student_entry_ids(hass, target)
                if entries is not None and data.get("entry_id") not in entries:
                    return False
                try:
                    return bool(passes(data, options))
                except (TypeError, ValueError):
                    _LOGGER.debug("Filter of %s failed on %s", self._key, data, exc_info=True)
                    return False

            @callback
            def _handle(event: Event) -> None:
                run_action(
                    trigger_payload(event.event_type, event.data),
                    f"Librus {self._key.replace('_', ' ')}",
                    event.context,
                )

            return hass.bus.async_listen(bus_event, _handle, event_filter=_wanted)

    TRIGGERS: dict[str, type[Trigger]] = {
        key: type(f"Librus_{key}_Trigger", (LibrusEventTrigger,), {"_key": key})
        for key in _SPECS
    }

    async def async_get_triggers(hass: HomeAssistant) -> dict[str, type[Trigger]]:
        """The triggers of this integration."""
        return TRIGGERS
