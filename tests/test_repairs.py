"""Tests for the Librus Synergia integration's repair issues (repairs.py)."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

from custom_components.librus_synergia.const import DOMAIN
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


def _make_flow(hass, entry_id: str = "abc123", issue_id: str = "school_year_rollover_abc123"):
    flow = LibrusSchoolYearRolloverRepairFlow(entry_id)
    # Normally set by the (generic, base) FlowManager - data_entry_flow.py's
    # FlowManager.async_init sets `.hass` right after RepairsFlowManager's
    # own async_create_flow (which sets `.issue_id`/`.data`) returns, before
    # the first step ever runs - safe to assume both are always present in
    # a real fix-flow step.
    flow.hass = hass
    flow.issue_id = issue_id
    return flow


async def test_school_year_repair_flow_shows_confirm_step(hass) -> None:
    flow = _make_flow(hass)

    result = await flow.async_step_init()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"


async def test_school_year_repair_flow_confirm_reloads_the_entry(hass) -> None:
    """Confirming the fix must force an immediate reload - that's the whole
    point (re-check the school year now instead of waiting up to 24h)."""
    flow = _make_flow(hass)

    with patch.object(hass.config_entries, "async_reload") as reload_mock:
        result = await flow.async_step_confirm(user_input={})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    reload_mock.assert_called_once_with("abc123")


async def test_school_year_repair_flow_confirm_shows_end_date_placeholder(hass) -> None:
    """The confirm step's own description is the only place `{end_date}`
    can be shown (hassfest forbids a fixable issue from ALSO carrying a
    top-level `description`) - it must be pulled from the real issue's
    `translation_placeholders`, matching the built-in generic
    `ConfirmRepairFlow`'s own lookup pattern."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        "school_year_rollover_abc123",
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="school_year_rollover",
        translation_placeholders={"end_date": "2026-06-20"},
        data={"entry_id": "abc123"},
    )
    flow = _make_flow(hass)

    result = await flow.async_step_confirm()

    assert result["description_placeholders"] == {"end_date": "2026-06-20"}
