"""icons.json covers every entity and action the integration has."""

from __future__ import annotations

import json
import re
from pathlib import Path

_COMPONENT = Path(__file__).parent.parent / "custom_components" / "librus_synergia"


def _load(name: str) -> dict:
    return json.loads((_COMPONENT / name).read_text(encoding="utf-8"))


def test_every_translated_entity_has_an_icon() -> None:
    strings = _load("strings.json")["entity"]
    icons = _load("icons.json")["entity"]
    for platform, keys in strings.items():
        missing = set(keys) - set(icons.get(platform, {}))
        assert not missing, f"{platform}: no icon for {sorted(missing)}"
        extra = set(icons.get(platform, {})) - set(keys)
        assert not extra, f"{platform}: icons for unknown keys {sorted(extra)}"


def test_every_action_has_an_icon() -> None:
    services = (_COMPONENT / "services.yaml").read_text(encoding="utf-8")
    names = set(re.findall(r"^([a-z_]+):", services, re.M))
    assert names == set(_load("icons.json")["services"])


def test_rank_icons_match_the_tiers() -> None:
    strings = _load("strings.json")["entity"]["sensor"]["rank"]["state"]
    icons = _load("icons.json")["entity"]["sensor"]["rank"]["state"]
    assert set(icons) == set(strings)


def test_no_icons_left_in_entity_code() -> None:
    """Icons live in icons.json; only the lucky number's party popper is
    picked in code (it depends on an attribute, not the state)."""
    for path in _COMPONENT.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "_attr_icon" not in text, path.name
