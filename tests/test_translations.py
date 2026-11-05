"""Every translation has every string of strings.json, with the same
placeholders - a missing key shows the raw key in the UI, a missing or
extra placeholder breaks the text."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent / "custom_components" / "librus_synergia"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _flat(node, path=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _flat(value, (*path, key))
    else:
        yield path, node


def _load(path: Path) -> dict:
    return dict(_flat(json.loads(path.read_text(encoding="utf-8"))))


SOURCE = _load(ROOT / "strings.json")


@pytest.mark.parametrize("path", sorted((ROOT / "translations").glob("*.json")), ids=lambda p: p.stem)
def test_translation_matches_strings(path: Path) -> None:
    strings = _load(path)
    assert set(strings) == set(SOURCE), (
        f"missing: {sorted(set(SOURCE) - set(strings))[:10]}, extra: {sorted(set(strings) - set(SOURCE))[:10]}"
    )
    wrong = [
        key
        for key, text in SOURCE.items()
        if set(_PLACEHOLDER.findall(str(text))) != set(_PLACEHOLDER.findall(str(strings[key])))
    ]
    assert not wrong, wrong[:10]


def test_ukrainian_and_portuguese_exist() -> None:
    assert (ROOT / "translations" / "uk.json").exists()
    assert (ROOT / "translations" / "pt.json").exists()
