"""Tests for the Librus Synergia integration's repair issues (repairs.py)."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.data_entry_flow import FlowResultType

from custom_components.librus_synergia.repairs import (
    LibrusSchoolYearRolloverRepairFlow,
    async_create_fix_flow,
)


async def test_async_create_fix_flow_returns_school_year_flow(hass) -> None:
    """`optional_endpoint_degraded` is raised with `is_fixable=False` and
    never reaches this - only `school_year_rollover` is fixable."""
    flow = await async_create_fix_flow(
        hass, "school_year_rollover_abc123", {"entry_id": "abc123"}
    )
    assert isinstance(flow, LibrusSchoolYearRolloverRepairFlow)
    assert flow._entry_id == "abc123"


async def test_async_create_fix_flow_handles_missing_data(hass) -> None:
    flow = await async_create_fix_flow(hass, "school_year_rollover_abc123", None)
    assert isinstance(flow, LibrusSchoolYearRolloverRepairFlow)
    assert flow._entry_id == ""


async def test_school_year_repair_flow_shows_confirm_step(hass) -> None:
    flow = LibrusSchoolYearRolloverRepairFlow("abc123")
    flow.hass = hass

    result = await flow.async_step_init()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"


async def test_school_year_repair_flow_confirm_reloads_the_entry(hass) -> None:
    """Confirming the fix must force an immediate reload - that's the whole
    point (re-check the school year now instead of waiting up to 24h)."""
    flow = LibrusSchoolYearRolloverRepairFlow("abc123")
    flow.hass = hass

    with patch.object(hass.config_entries, "async_reload") as reload_mock:
        result = await flow.async_step_confirm(user_input={})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    reload_mock.assert_called_once_with("abc123")
