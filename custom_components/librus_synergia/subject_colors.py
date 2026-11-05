"""One colour per subject, the same in every companion card.

Every subject the student has gets a stable slot of the cards' 16-colour
palette (`subject_palette`); the Subject colours option under Configure
overrides chosen subjects with any CSS colour (`subject_colors`). Both are
attributes of the School sensor, so the cards read one place per student.
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Iterable, Mapping

from librus_synergia.models import LibrusData

from .forecast import _subject_key

PALETTE_SIZE = 16

# Hex, a colour name, or a CSS colour function - the cards hand it to CSS
# as is, so anything else (a typo, a stray character) is refused here.
_COLOR_RE = re.compile(
    r"^(#[0-9a-f]{3,8}|[a-z]{3,30}|(rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch)\([0-9a-z%.,/ +-]+\))$",
    re.IGNORECASE,
)


def student_subjects(data: LibrusData) -> dict[int, str]:
    """The subjects this student has - in the timetable, the standing plan
    or their grades - by id; not the school's whole subject list."""
    ids: set[int] = set()
    for lessons in data.timetable.values():
        ids.update(lesson.subject_id for lesson in lessons if lesson.subject_id is not None)
    ids.update(lesson.subject_id for lesson in data.standing_timetable if lesson.subject_id is not None)
    ids.update(grade.subject_id for grade in data.grades if grade.subject_id is not None)
    return {subject_id: data.subjects[subject_id] for subject_id in ids if data.subjects.get(subject_id)}


def palette_slots(names: Iterable[str]) -> dict[str, int]:
    """A palette slot (1-16) per subject name: each subject's preferred slot
    comes from its name, so a subject keeps its colour whatever else the
    student has; a taken slot moves on to the next free one (in name
    order). Past 16 subjects the slots repeat."""
    slots: dict[str, int] = {}
    used: set[int] = set()
    for name in sorted({n for n in names if n}, key=_subject_key):
        if len(used) == PALETTE_SIZE:
            used.clear()
        slot = zlib.crc32(_subject_key(name).encode("utf-8")) % PALETTE_SIZE
        while slot in used:
            slot = (slot + 1) % PALETTE_SIZE
        used.add(slot)
        slots[name] = slot + 1
    return slots


def parse_subject_colors(
    text: str | None, subjects: Mapping[int | str, str]
) -> tuple[dict[str, str], list[str]]:
    """Colours from lines like "Matematyka: #2563eb" (one subject a line),
    matched to `subjects` by name - case and Polish letters don't matter -
    or by the subject id. Returns subject name -> colour, and the lines that
    didn't work (an unknown subject or something that isn't a colour)."""
    matched: dict[str, str] = {}
    unmatched: list[str] = []
    if not text:
        return matched, unmatched
    names = {_subject_key(name): name for name in subjects.values()}
    for line in re.split(r"[\r\n]+", text):
        line = line.strip()
        if not line:
            continue
        name, separator, color = line.partition(":")
        color = color.strip()
        subject = names.get(_subject_key(name))
        if subject is None:
            subject = next((n for sid, n in subjects.items() if str(sid) == name.strip()), None)
        if not separator or subject is None or not _COLOR_RE.match(color):
            unmatched.append(line)
            continue
        matched[subject] = color
    return matched, unmatched
