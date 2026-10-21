"""The grade notification blueprints' templates with the events they get."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from homeassistant.helpers.template import Template
from homeassistant.util.yaml import load_yaml_dict
from homeassistant.util.yaml.objects import Input

BLUEPRINTS = Path(__file__).parents[1] / "blueprints" / "automation" / "librus_synergia"


def _load(name: str, **inputs: Any) -> dict[str, Any]:
    """The blueprint with its inputs filled: the given ones, else defaults."""
    data = load_yaml_dict(str(BLUEPRINTS / name))
    values = {k: (spec or {}).get("default") for k, spec in data["blueprint"]["input"].items()} | inputs

    def fill(value: Any) -> Any:
        if isinstance(value, Input):
            return values[value.name]
        if isinstance(value, list):
            return [fill(v) for v in value]
        if isinstance(value, dict):
            return {k: fill(v) for k, v in value.items()}
        return value

    return fill(data)


def _render(hass, template: str, variables: dict[str, Any], event: dict[str, Any]) -> Any:
    return Template(template, hass).async_render({**variables, "trigger": {"event": {"data": event}}})


POINT = {"kind": "point", "value": "2", "points": 2.0, "max_points": 2.0, "percentage": 100.0, "subject": "Fizyka"}
NORMAL = {"kind": "normal", "value": "2", "subject": "Fizyka"}


async def test_low_grade_blueprint_skips_point_grades(hass) -> None:
    bp = _load("low_grade_notification.yaml", threshold=2)
    condition = bp["conditions"][0]["value_template"]

    assert _render(hass, condition, bp["variables"], NORMAL) is True
    # "2" points out of 2 is full marks, not a 2 on the 1-6 scale.
    assert _render(hass, condition, bp["variables"], POINT) is False


async def test_new_grade_summary_shows_points_out_of_max(hass) -> None:
    bp = _load("new_grade_notification.yaml", include_average=False, include_details=False)
    summary = bp["actions"][0]["variables"]["grade_summary"]
    variables = bp["variables"]

    # Librus sends the points as text with two decimals.
    point = {**POINT, "value": "17.00", "points": 17.0, "max_points": 20.0, "percentage": 85.0}
    assert _render(hass, summary, variables, point) == "Nowa ocena: 17/20 pkt (85%) (Fizyka)"
    point_frac = {**point, "value": "9.47", "points": 9.47, "max_points": 10.0, "percentage": 94.7}
    assert _render(hass, summary, variables, point_frac) == "Nowa ocena: 9,47/10 pkt (94,7%) (Fizyka)"
    assert _render(hass, summary, variables, {**NORMAL, "value": "5"}) == "Nowa ocena: 5 (Fizyka)"
