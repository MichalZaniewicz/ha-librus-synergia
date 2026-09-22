"""Diagnostics must be serializable by Home Assistant's own JSON encoder.

Regression (found live, 2026-09-21): `dataclasses.asdict(coordinator.data)`
keeps `date` keys (timetable) and `int` keys (subjects/teachers lookups), which
orjson - what HA serializes diagnostics with - refuses, so "Download
diagnostics" returned HTTP 500 ("Failed to serialize to JSON").
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.helpers.json import json_bytes
from homeassistant.util import dt as dt_util

from custom_components.librus_synergia.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.librus_synergia.librus_api import LibrusSessionExpiredError

from .conftest import build_mock_client, setup_integration

_TOMORROW = (dt_util.now().date() + timedelta(days=1)).isoformat()


async def test_diagnostics_are_json_serializable(hass) -> None:
    client = build_mock_client(
        async_get_timetable={
            "Timetable": {
                _TOMORROW: [
                    [
                        {
                            "LessonNo": "1",
                            "HourFrom": "08:00",
                            "HourTo": "08:45",
                            "Subject": {"Id": "42005", "Name": "Matematyka"},
                            "Teacher": {"Id": "1000"},
                            "Classroom": {"Id": "2000"},
                            "IsCanceled": False,
                            "IsSubstitutionClass": False,
                        }
                    ]
                ]
            }
        },
        async_get_subjects={"Subjects": [{"Id": 42005, "Name": "Matematyka"}]},
    )
    entry = await setup_integration(hass, client)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    # HA's real encoder (orjson) - a plain `json.dumps` would be more
    # forgiving about some value types and would not have caught this.
    json_bytes(diagnostics)
    assert _TOMORROW in diagnostics["coordinator_data"]["timetable"]
    # Secrets stay redacted.
    assert diagnostics["entry_data"]["password"] != "pw"
    # A fully healthy cycle: no degraded endpoints, no open repair issues.
    assert diagnostics["last_update_success"] is True
    assert diagnostics["degraded_endpoints"] == {}
    assert diagnostics["open_repair_issues"] == []


async def test_diagnostics_report_a_degraded_endpoint(hass) -> None:
    """BUG FIX (live feedback, 2026-09-22 - "może diagnostics byśmy dodali
    sprawdzające co się tam dzieje w ogóle?"): the original dump had no
    way to tell a confirmed-403-degraded endpoint apart from one that's
    genuinely broken, or from an account that just has no data for it -
    all three look identical (an empty/missing field) in a bare
    coordinator-data snapshot. `degraded_endpoints` must surface exactly
    which endpoint is degrading and since when, straight from the
    coordinator's own repair-issue tracking dict."""
    client = build_mock_client()
    client.async_get_attendance_types.side_effect = LibrusSessionExpiredError(
        "Session rejected on .../Attendances/Types (HTTP 403).", status_code=403
    )
    entry = await setup_integration(hass, client)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    json_bytes(diagnostics)
    # The cycle as a whole still succeeded - only one piece degraded.
    assert diagnostics["last_update_success"] is True
    assert "Attendances/Types" in diagnostics["degraded_endpoints"]
    # A single failed attempt doesn't cross the 7-day threshold yet.
    assert diagnostics["open_repair_issues"] == []
