"""Repair issues for the Librus Synergia (unofficial) integration.

Two issues, both raised/cleared from coordinator.py (see
`_check_school_year_rollover` and `_note_optional_endpoint_failure`/
`_note_optional_endpoint_recovery` there for exactly when):

- **school_year_rollover** (fixable): the cached `ClassData.end_school_year`
  date is over a month in the past. `_cached_class` is normally refetched
  every 24h, so this self-heals almost immediately once Librus publishes a
  new Class record for the new school year - this only fires if that
  hasn't happened in over a month. The fix is a plain confirm-and-reload,
  which just forces an immediate re-check instead of waiting for the next
  24h cache window.
- **optional_endpoint_degraded** (informational, not fixable): one of the
  supplementary endpoints (BehaviourGrades, DescriptiveGrades, etc. - see
  `const.OPTIONAL_ENDPOINT_LABELS`) has failed on every attempt for over a
  week. Often harmless - this project has several endpoints that are
  confirmed real but simply disabled/empty for a given school for an
  entire year (see BACKLOG.md) - but a full week of unbroken failures is
  worth surfacing rather than staying silent in the debug log forever.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from .const import (
    CONF_GRADE_CATEGORY_NAMES,
    CONF_SUBJECT_NAMES,
    DOMAIN,
    UNMATCHED_SETTING_ISSUES,
)
from .subject_names import parse_category_names


class LibrusSchoolYearRolloverRepairFlow(RepairsFlow):
    """Confirm-only fix: reload the config entry to force an immediate
    reference-data refresh instead of waiting for the normal 24h cache."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        if user_input is not None:
            await self.hass.config_entries.async_reload(self._entry_id)
            return self.async_create_entry(data={})
        # Same lookup the built-in generic `ConfirmRepairFlow` does - lets
        # the confirm step's own description use the issue's own
        # `{end_date}` placeholder (hassfest's translation schema forbids a
        # fixable issue from ALSO having a top-level `description`, so this
        # is the only place that placeholder can be shown).
        description_placeholders = None
        if issue := ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id):
            description_placeholders = issue.translation_placeholders
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders=description_placeholders,
        )


class LibrusUnmatchedSettingRepairFlow(RepairsFlow):
    """Lines under Configure that match nothing (a renamed subject, a typo):
    names are given again field by field like on the Names step, colours and
    point scales are edited as text. Saving updates the entry's options
    (which reloads it); the issue goes away once nothing is unmatched."""

    def __init__(self, entry_id: str, kind: str) -> None:
        self._entry_id = entry_id
        self._kind = kind

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        # Imported here: config_flow imports the client library, repairs
        # loads with the integration.
        from .config_flow import names_schema, names_text

        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        coordinator = getattr(entry, "runtime_data", None) if entry else None
        option = UNMATCHED_SETTING_ISSUES.get(self._kind)
        if entry is None or option is None or coordinator is None or coordinator.data is None:
            return self.async_abort(reason="no_data")
        options = entry.options
        names: list[str] | None = None
        if option == CONF_SUBJECT_NAMES:
            names = coordinator.subject_choices
        elif option == CONF_GRADE_CATEGORY_NAMES:
            names = coordinator.grade_category_choices

        if user_input is not None:
            text = names_text(user_input) if names else str(user_input.get(option) or "").strip()
            new = {**options, option: text}
            if not text:
                new.pop(option)
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            self.hass.config_entries.async_update_entry(entry, options=new)
            return self.async_create_entry(data={})

        placeholders = dict(self._issue_placeholders())
        placeholders["subjects"] = ", ".join(coordinator.subject_choices) or "-"
        if names:
            current, _ = parse_category_names(options.get(option), names)
            schema = names_schema(names, current)
        else:
            schema = vol.Schema(
                {
                    vol.Optional(
                        option, description={"suggested_value": options.get(option)}
                    ): TextSelector(TextSelectorConfig(multiline=True))
                }
            )
        return self.async_show_form(
            step_id="init", data_schema=schema, description_placeholders=placeholders
        )

    def _issue_placeholders(self) -> dict[str, str]:
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
        return dict(issue.translation_placeholders or {}) if issue else {}


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """`school_year_rollover` and the unmatched-setting issues are fixable -
    `optional_endpoint_degraded` is raised with `is_fixable=False` and never
    reaches this."""
    data = data or {}
    if data.get("kind") in UNMATCHED_SETTING_ISSUES:
        return LibrusUnmatchedSettingRepairFlow(data.get("entry_id", ""), data["kind"])
    return LibrusSchoolYearRolloverRepairFlow(data.get("entry_id", ""))
