"""Diagnostics support for the Librus Synergia (unofficial) integration."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import async_get_integration

from . import LibrusConfigEntry
from .const import CONF_AI_CONTEXT, CONF_STUDENT_NUMBER, DOMAIN

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
    "receiver_name",
    "topic",
    # The family's own notes for the AI summary and the class register
    # number (options and data).
    CONF_AI_CONTEXT,
    CONF_STUDENT_NUMBER,
    # Who the student is (Me) and the teachers' LIDs.
    "account_id",
    "teacher_lid",
    # School trips (where, the route, the teacher in charge), the school's
    # head teacher, file names of homework attachments.
    "coordinator",
    "destination",
    "route",
    "head_teacher_name",
    "filename",
}

# School documents: their names and Synergia links (the `name` key is too
# common to redact everywhere - category and attendance type names are what
# a diagnostics dump is for).
_SCHOOL_FILE_KEYS = ("name", "download_path")
_LID_PREFIX = "LID-"


def _redact_lids(value: Any) -> Any:
    """Every `LID-...` identifier (a Librus account id - the teachers' map
    is keyed by them for the new descriptive grading, and some ids in the
    data are LIDs) replaced by a numbered placeholder, as a key or a
    value. The same LID gets the same placeholder, so the dump stays
    consistent."""
    seen: dict[str, str] = {}

    def placeholder(lid: str) -> str:
        return seen.setdefault(lid, f"{_LID_PREFIX}**REDACTED**-{len(seen) + 1}")

    def walk(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                (placeholder(k) if isinstance(k, str) and k.startswith(_LID_PREFIX) else k): walk(v)
                for k, v in item.items()
            }
        if isinstance(item, list):
            return [walk(v) for v in item]
        if isinstance(item, str) and item.startswith(_LID_PREFIX):
            return placeholder(item)
        return item

    return walk(value)


def _redact_school_files(data: dict[str, Any]) -> None:
    for item in data.get("school_files") or []:
        if isinstance(item, dict):
            for key in _SCHOOL_FILE_KEYS:
                if item.get(key):
                    item[key] = REDACTED


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
    """Return diagnostics for a config entry.

    BUG FIX (live feedback, 2026-09-22 - "co się tam dzieje w ogóle?"):
    the original dump only had the raw entry data + a full coordinator-
    data snapshot - useful for checking what DID come through, but gave
    no direct answer to "why is half of this account unknown/empty" (a
    confirmed-403-degraded endpoint and a genuinely broken one both just
    look like an empty/missing field in that dump, with no way to tell
    them apart). Added sections that answer that directly: whether the
    last update cycle actually succeeded, which endpoints are CURRENTLY
    degrading and since when (straight from the coordinator's own
    repair-issue tracking - see `LibrusDataUpdateCoordinator.
    degraded_endpoints`), any repair issues HA itself has open for this
    entry right now, and - requested the same round, "co jeszcze
    potencjalnie problematycznego można by dorzucić" - the integration's
    own version (confirms which beta someone's actually running, not
    just what they think they installed), the options-flow feature
    toggles (a toggled-off feature and a genuinely degraded endpoint both
    show as "no data" downstream - this tells them apart at a glance),
    and session/reference-data freshness (is the session actually fresh
    or right at the edge of our own lifetime assumption, how stale is the
    cached reference data)."""
    coordinator = entry.runtime_data
    coordinator_data = (
        _redact_lids(_stringify_keys(dataclasses.asdict(coordinator.data)))
        if coordinator.data is not None
        else None
    )
    if coordinator_data is not None:
        _redact_school_files(coordinator_data)
    registry = ir.async_get(hass)
    open_issues = [
        {
            "issue_id": issue.issue_id,
            "translation_key": issue.translation_key,
            "translation_placeholders": issue.translation_placeholders,
            "severity": issue.severity.value if issue.severity else None,
        }
        for issue in registry.issues.values()
        if issue.domain == DOMAIN and entry.entry_id in issue.issue_id
    ]
    integration = await async_get_integration(hass, DOMAIN)
    reference_data_fetched_at = coordinator.reference_data_fetched_at

    def iso(value: Any) -> str | None:
        return value.isoformat() if value else None

    return {
        "integration_version": integration.version,
        "last_update_success": coordinator.last_update_success,
        "last_exception": repr(coordinator.last_exception)
        if coordinator.last_exception
        else None,
        "update_interval_seconds": (
            coordinator.update_interval.total_seconds() if coordinator.update_interval else None
        ),
        # Which optional features are toggled on, so a toggled-off feature
        # (e.g. messages_enabled: false) is never mistaken for a
        # degraded/broken endpoint downstream - minus the free-text notes
        # and the register number.
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "session_valid": coordinator.client.is_session_valid(),
        "session_age_seconds": coordinator.client.session_age_seconds,
        "reference_data_fetched_at": (
            reference_data_fetched_at.isoformat() if reference_data_fetched_at is not None else None
        ),
        # Label -> ISO timestamp it started failing (see
        # `degraded_endpoints`'s own docstring) - empty means every
        # degradable endpoint succeeded on the last cycle.
        "connection": {
            "status": coordinator.status,
            "data_source": coordinator.data_source,
            "last_success": iso(coordinator.last_success_at),
            "last_attempt": iso(coordinator.last_attempt_at),
            "last_error": coordinator.last_error,
            "failures": coordinator.failures,
            "next_attempt": iso(coordinator.next_attempt_at),
            "fallback_sections": sorted(coordinator.fallback_sections),
        },
        "degraded_endpoints": {
            label: since.isoformat() for label, since in coordinator.degraded_endpoints.items()
        },
        "open_repair_issues": open_issues,
        # Kindergarten timetable discovery (issue #5) - state only, never
        # the child's LID itself.
        "kindergarten": coordinator.kindergarten_diagnostics,
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "coordinator_data": async_redact_data(coordinator_data, TO_REDACT)
        if coordinator_data is not None
        else None,
    }
