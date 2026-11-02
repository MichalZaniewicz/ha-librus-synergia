"""Conditions for the automation editor: "Librus: School day" and friends.

Each one reads the student's own entity (School day tomorrow, At school,
Test within 24 hours, Lucky number, Unexcused absences), so a condition
says exactly what the dashboard shows. The target picks the student(s) -
none means every student; `behavior` says whether any or all of them must
match. A student whose entity is missing or unavailable doesn't match.

Needs the condition platform of Home Assistant 2025.12+; on an older core
this module defines nothing and nothing changes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.const import CONF_OPTIONS, CONF_TARGET, STATE_ON
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .trigger import student_entry_ids

try:  # Home Assistant 2026.10+ validates with probatio, older ones voluptuous.
    import probatio as vol
except ImportError:  # pragma: no cover - older cores
    import voluptuous as vol


def _on(state: State) -> bool:
    return state.state == STATE_ON


def _lucky_yours(state: State) -> bool:
    return bool(state.attributes.get("is_yours")) and bool(state.attributes.get("is_today"))


def _has_unexcused(state: State) -> bool:
    try:
        return int(float(state.state)) > 0
    except ValueError:
        return False


# key -> (options schema, (options -> (platform, unique id suffix)), test)
_SPECS: dict[
    str,
    tuple[dict[Any, Any], Callable[[dict[str, Any]], tuple[str, str]], Callable[[State], bool]],
] = {
    "school_day": (
        {vol.Optional("day", default="today"): vol.In(["today", "tomorrow"])},
        lambda options: ("binary_sensor", f"school_day_{options.get('day', 'today')}"),
        _on,
    ),
    "in_school": ({}, lambda _o: ("binary_sensor", "in_school"), _on),
    "exam_soon": ({}, lambda _o: ("binary_sensor", "exam_soon"), _on),
    "lucky_number_yours": ({}, lambda _o: ("sensor", "lucky_number"), _lucky_yours),
    "absences_to_justify": ({}, lambda _o: ("sensor", "unexcused_absences"), _has_unexcused),
}


def check(
    hass: HomeAssistant, key: str, target: dict[str, Any] | None, options: dict[str, Any]
) -> bool:
    """Whether the targeted students match condition `key`."""
    _schema, entity_of, test = _SPECS[key]
    entries = student_entry_ids(hass, target)
    if entries is None:
        entries = {
            entry.entry_id
            for entry in hass.config_entries.async_entries(DOMAIN)
            if not entry.disabled_by
        }
    if not entries:
        return False
    platform, suffix = entity_of(options)
    ent_reg = er.async_get(hass)
    results = []
    for entry_id in entries:
        entity_id = ent_reg.async_get_entity_id(platform, DOMAIN, f"{entry_id}_{suffix}")
        state = hass.states.get(entity_id) if entity_id else None
        results.append(state is not None and state.state not in ("unknown", "unavailable") and test(state))
    if options.get("behavior") == "all":
        return all(results)
    return any(results)


try:
    from homeassistant.helpers.condition import Condition
except ImportError:  # pragma: no cover - Home Assistant before 2025.12
    Condition = None  # type: ignore[assignment,misc]


if Condition is not None:

    class LibrusCondition(Condition):
        """One Librus condition, checked against the student's entity."""

        _key: str

        @classmethod
        async def async_validate_config(
            cls, hass: HomeAssistant, config: ConfigType
        ) -> ConfigType:
            options = dict(_SPECS[cls._key][0])
            options[vol.Optional("behavior", default="any")] = vol.In(["any", "all"])
            schema = vol.Schema(
                {
                    vol.Optional(CONF_TARGET): cv.TARGET_FIELDS,
                    vol.Optional(CONF_OPTIONS, default={}): vol.Schema(options),
                }
            )
            return schema(config)  # type: ignore[no-any-return]

        def __init__(self, hass: HomeAssistant, config: Any) -> None:
            super().__init__(hass, config)
            self._hass = hass
            self._target = getattr(config, "target", None) or {}
            self._options = getattr(config, "options", None) or {}

        # Home Assistant 2026.10+.
        def _async_check(self, **kwargs: Any) -> bool:
            return check(self._hass, self._key, self._target, self._options)

        # Home Assistant before 2026.10 asks for a checker function instead.
        async def async_get_checker(self) -> Callable[..., bool]:
            def checker(hass: HomeAssistant, variables: Any = None, **kwargs: Any) -> bool:
                return check(self._hass, self._key, self._target, self._options)

            return checker

    CONDITIONS: dict[str, type[Condition]] = {
        key: type(f"Librus_{key}_Condition", (LibrusCondition,), {"_key": key})
        for key in _SPECS
    }

    async def async_get_conditions(hass: HomeAssistant) -> dict[str, type[Condition]]:
        """The conditions of this integration."""
        return CONDITIONS
