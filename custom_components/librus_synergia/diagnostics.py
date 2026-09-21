"""Diagnostics support for the Librus Synergia (unofficial) integration."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import LibrusConfigEntry

# entry.data holds login/password/session cookies - all secrets. Coordinator
# data carries the student's real grades/attendance/notes, so free-text and
# name fields are redacted too (async_redact_data walks nested dicts/lists
# at any depth, once coordinator.data is converted to a plain dict via
# dataclasses.asdict).
#
# Known limitation: subjects/teachers/classrooms are plain `{id: "name"}`
# maps, so a bare resolved name isn't behind any of these dict KEYS and
# is NOT redacted here - acceptable for a first cut since only whoever the
# config entry's owner shares a diagnostics dump with would see it, but
# worth tightening later if this integration gets wider use.
TO_REDACT = {
    "username",
    "password",
    "cookies",
    "value",
    "text",
    "content",
    "comments",
    "first_name",
    "last_name",
    "subject",
    "sender_name",
    "topic",
}


def _stringify_keys(value: Any) -> Any:
    """Recursively turn every dict key into a `str`.

    `dataclasses.asdict` keeps the coordinator data's real key types - `date`
    for the timetable/attendance-by-day maps, `int` for the subjects/teachers/
    classrooms lookups - and HA serializes diagnostics with orjson, which
    rejects non-string keys outright, so "Download diagnostics" answered
    HTTP 500 instead of a dump."""
    if isinstance(value, dict):
        return {str(key): _stringify_keys(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringify_keys(item) for item in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LibrusConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    coordinator_data = (
        _stringify_keys(dataclasses.asdict(coordinator.data))
        if coordinator.data is not None
        else None
    )
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "coordinator_data": async_redact_data(coordinator_data, TO_REDACT)
        if coordinator_data is not None
        else None,
    }
