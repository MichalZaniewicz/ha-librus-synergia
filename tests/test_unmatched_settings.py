"""A repair issue when a line under Configure matches nothing any more
(the school renamed a subject, a typo): names, colours, point scales."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

from custom_components.librus_synergia.const import (
    CONF_SUBJECT_COLORS,
    CONF_SUBJECT_NAMES,
    DOMAIN,
)
from custom_components.librus_synergia.coordinator import unmatched_setting_issue_id
from custom_components.librus_synergia.repairs import (
    LibrusUnmatchedSettingRepairFlow,
    async_create_fix_flow,
)

from .conftest import build_mock_client, setup_integration


def _client():
    return build_mock_client(
        async_get_subjects={"Subjects": [{"Id": 42005, "Name": "Matematyka"}, {"Id": 42006, "Name": "Fizyka"}]},
        async_get_grades={
            "Grades": [
                {"Id": 1, "Grade": "5", "Category": {"Id": 10}, "Subject": {"Id": 42005},
                 "Semester": 1, "AddDate": "2026-09-01"},
                {"Id": 2, "Grade": "4", "Category": {"Id": 10}, "Subject": {"Id": 42006},
                 "Semester": 1, "AddDate": "2026-09-02"},
            ]
        },
        async_get_grade_categories={
            "Categories": [{"Id": 10, "Name": "kartkówka", "CountToTheAverage": True, "Weight": 1}]
        },
    )


def _flow(hass, entry, kind: str) -> LibrusUnmatchedSettingRepairFlow:
    flow = LibrusUnmatchedSettingRepairFlow(entry.entry_id, kind)
    flow.hass = hass
    flow.issue_id = unmatched_setting_issue_id(entry.entry_id, kind)
    return flow


async def test_everything_matching_raises_no_issue(hass) -> None:
    entry = await setup_integration(
        hass, _client(), options={CONF_SUBJECT_NAMES: "Matematyka: Maths", CONF_SUBJECT_COLORS: "Fizyka: red"}
    )
    issues = [i for (domain, i) in ir.async_get(hass).issues if domain == DOMAIN]
    assert not [i for i in issues if entry.entry_id in i and i.startswith("unmatched_")]


async def test_renamed_subject_raises_an_issue_fixed_field_by_field(hass) -> None:
    client = _client()
    entry = await setup_integration(
        hass, client, options={CONF_SUBJECT_NAMES: "Matematyka: Maths\nBiologia: Biology"}
    )
    issue_id = unmatched_setting_issue_id(entry.entry_id, "unmatched_subject_names")
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.is_fixable
    assert issue.translation_placeholders["lines"] == "- Biologia: Biology"

    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    assert isinstance(flow, LibrusUnmatchedSettingRepairFlow)
    flow = _flow(hass, entry, "unmatched_subject_names")
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    fields = {str(k): k for k in result["data_schema"].schema}
    assert list(fields) == ["Fizyka", "Matematyka"]
    assert fields["Matematyka"].description == {"suggested_value": "Maths"}

    with patch("custom_components.librus_synergia.LibrusApiClient", return_value=client):
        result = await flow.async_step_init({"Matematyka": "Maths", "Fizyka": "Physics"})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SUBJECT_NAMES] == "Matematyka: Maths\nFizyka: Physics"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_unmatched_colour_is_fixed_as_text(hass) -> None:
    client = _client()
    entry = await setup_integration(hass, client, options={CONF_SUBJECT_COLORS: "Chemia: red"})
    issue_id = unmatched_setting_issue_id(entry.entry_id, "unmatched_subject_colors")
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    flow = _flow(hass, entry, "unmatched_subject_colors")
    result = await flow.async_step_init()
    assert [str(k) for k in result["data_schema"].schema] == [CONF_SUBJECT_COLORS]
    assert result["description_placeholders"]["subjects"] == "Fizyka, Matematyka"

    # Emptying the field removes the option.
    with patch("custom_components.librus_synergia.LibrusApiClient", return_value=client):
        result = await flow.async_step_init({CONF_SUBJECT_COLORS: ""})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_SUBJECT_COLORS not in entry.options
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
