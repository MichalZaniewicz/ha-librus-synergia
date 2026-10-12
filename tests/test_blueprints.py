"""Every blueprint must accept what the UI stores for an action input.

The action selector always stores a LIST of actions, even for one action.
Placed as a list item (`- !input notify_action`) that list became a list
inside the action list, and Home Assistant refused to save the automation
("expected dictionary at 'actions[1]'"). Wrapped in `- sequence: !input ...`
it is one valid action.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from homeassistant.helpers import config_validation as cv
from homeassistant.util.yaml import load_yaml_dict
from homeassistant.util.yaml.objects import Input

BLUEPRINTS = sorted(
    (Path(__file__).parents[1] / "blueprints" / "automation" / "librus_synergia").glob("*.yaml")
)

# What the automation editor stores for an action input with one action.
UI_ACTION = [{"action": "notify.notify", "data": {"message": "{{ 'test' }}"}}]


def _sample(spec: dict[str, Any]) -> Any:
    selector = spec.get("selector") or {}
    if "action" in selector:
        return UI_ACTION
    if "default" in spec:
        return spec["default"]
    if "entity" in selector:
        return "sensor.test"
    if "number" in selector:
        return 1
    if "boolean" in selector:
        return True
    if "time" in selector:
        return "18:00:00"
    return "test"


def _substitute(value: Any, inputs: dict[str, Any]) -> Any:
    if isinstance(value, Input):
        return inputs[value.name]
    if isinstance(value, list):
        return [_substitute(item, inputs) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, inputs) for key, item in value.items()}
    return value


def test_every_blueprint_is_checked() -> None:
    assert len(BLUEPRINTS) >= 31


@pytest.mark.parametrize("path", BLUEPRINTS, ids=lambda p: p.name)
def test_blueprint_actions_accept_a_list_of_actions(path: Path) -> None:
    data = load_yaml_dict(str(path))
    inputs = {name: _sample(spec or {}) for name, spec in data["blueprint"]["input"].items()}
    actions = _substitute(data["actions"], inputs)
    cv.SCRIPT_SCHEMA(actions)
