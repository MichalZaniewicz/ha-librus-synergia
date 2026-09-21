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
