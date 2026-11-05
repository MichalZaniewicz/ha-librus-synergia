"""Your own names for subjects (Configure → Subject names, issue #29).

A family where someone doesn't read Polish can call "Edukacja matematyczna"
"Maths": the name is swapped where the integration builds its data, so it
shows everywhere at once - entity names, attributes, events, calendars,
Assist, the weekly AI summary and every companion card. Options that name a
subject themselves (colours, point scales) accept the Librus name or yours.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from .forecast import _subject_key

# Longer than any real subject name; keeps a stray paste from becoming an
# entity name.
_MAX_NAME = 60


def parse_subject_names(
    text: str | None, subjects: Mapping[int | str, str]
) -> tuple[dict[int | str, str], list[str]]:
    """Names from lines like "Edukacja matematyczna: Maths" (one subject a
    line), matched to `subjects` by the Librus name - case and Polish letters
    don't matter - or by the subject id. Returns subject id -> your name, and
    the lines that didn't work (an unknown subject or an empty name)."""
    matched: dict[int | str, str] = {}
    unmatched: list[str] = []
    if not text:
        return matched, unmatched
    names = {_subject_key(name): subject_id for subject_id, name in subjects.items()}
    for line in re.split(r"[\r\n]+", text):
        line = line.strip()
        if not line:
            continue
        original, separator, name = line.partition(":")
        name = " ".join(name.split())[:_MAX_NAME]
        subject_id = names.get(_subject_key(original))
        if subject_id is None:
            subject_id = next((sid for sid in subjects if str(sid) == original.strip()), None)
        if not separator or subject_id is None or not name:
            unmatched.append(line)
            continue
        matched[subject_id] = name
    return matched, unmatched


def rename_subjects(
    subjects: Mapping[int | str, str], names: Mapping[int | str, str]
) -> dict[int | str, str]:
    """`subjects` with your names in place of the Librus ones."""
    return {subject_id: names.get(subject_id, name) for subject_id, name in subjects.items()}


def parse_category_names(
    text: str | None, categories: Iterable[str]
) -> tuple[dict[str, str], list[str]]:
    """Grade category names from lines like "kartkówka: Quiz" (one category a
    line), matched to the school's category names - case and Polish letters
    don't matter. Returns the matched name's key (see `category_key`) ->
    your name, and the lines that didn't work."""
    matched: dict[str, str] = {}
    unmatched: list[str] = []
    if not text:
        return matched, unmatched
    known = {_subject_key(name) for name in categories if name}
    for line in re.split(r"[\r\n]+", text):
        line = line.strip()
        if not line:
            continue
        original, separator, name = line.partition(":")
        name = " ".join(name.split())[:_MAX_NAME]
        key = _subject_key(original)
        if not separator or key not in known or not name:
            unmatched.append(line)
            continue
        matched[key] = name
    return matched, unmatched


def category_key(name: str) -> str:
    """How a category name is matched (case and Polish letters don't matter)."""
    return _subject_key(name)
