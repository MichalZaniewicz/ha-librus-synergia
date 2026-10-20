"""Days with an absence or a late arrival, one entry per date - for the
Absences calendar (calendar.py). Pure: no Home Assistant imports.

Librus keeps attendance per lesson; a parent thinks in days ("Tuesday he
was off sick, lessons 1-3"). Each day lists its lessons, and an unexcused
day says whether a justification was already sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from librus_synergia.models import LibrusData
from librus_synergia.parsers import justified_dates

# "Spóźnienie" (late) is a PRESENCE-kind type, like plain "Obecność" -
# Librus has no separate flag for it, so "późn" in the type's own name is
# the only signal (same best-effort match as
# `AttendanceTypeData.is_excused_absence` with "uspr.").
_LATE_TYPE_NAME_RE = re.compile(r"późn", re.IGNORECASE)


def is_late_type_name(name: str) -> bool:
    return _LATE_TYPE_NAME_RE.search(name) is not None


@dataclass(slots=True)
class AbsenceLesson:
    lesson_no: int | None
    subject: str | None
    kind: str  # "unexcused", "excused" or "late"
    type_name: str


@dataclass(slots=True)
class AbsenceDay:
    date: str  # YYYY-MM-DD
    lessons: list[AbsenceLesson] = field(default_factory=list)
    justification_sent: bool = False

    def count(self, kind: str) -> int:
        return sum(1 for lesson in self.lessons if lesson.kind == kind)

    @property
    def summary(self) -> str:
        unexcused, excused, late = (self.count(k) for k in ("unexcused", "excused", "late"))
        if unexcused and excused:
            text = f"Nieobecność: {excused} uspr., {unexcused} nieuspr."
        elif unexcused:
            text = f"Nieobecność nieusprawiedliwiona ({lessons_text(unexcused)})"
        elif excused:
            text = f"Nieobecność usprawiedliwiona ({lessons_text(excused)})"
        else:
            numbers = [lesson.lesson_no for lesson in self.lessons if lesson.lesson_no is not None]
            text = "Spóźnienie" + (f" (lekcja {numbers[0]})" if len(numbers) == 1 else "")
            return text if late == 1 else f"Spóźnienia ({late})"
        if late:
            text += f", spóźnienie ({late})" if late > 1 else ", spóźnienie"
        return text

    @property
    def description(self) -> str:
        lines = []
        for lesson in self.lessons:
            number = f"{lesson.lesson_no}. " if lesson.lesson_no is not None else ""
            subject = lesson.subject or "?"
            lines.append(f"{number}{subject} – {lesson.type_name}")
        if self.justification_sent:
            lines.append("Usprawiedliwienie wysłane")
        return "\n".join(lines)


def lessons_text(count: int) -> str:
    """"1 lekcja", "3 lekcje", "5 lekcji", "22 lekcje" (Polish plural)."""
    if count == 1:
        return "1 lekcja"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} lekcje"
    return f"{count} lekcji"


def absence_days(data: LibrusData) -> list[AbsenceDay]:
    """Every date with an absence (excused or not) or a late arrival,
    oldest first; lessons in lesson order. Records of an unknown type are
    left out - nothing says they are an absence."""
    days: dict[str, AbsenceDay] = {}
    for record in data.attendances:
        if not record.date or record.type_id is None:
            continue
        attendance_type = data.attendance_types.get(record.type_id)
        if attendance_type is None:
            continue
        if attendance_type.is_presence_kind:
            if not is_late_type_name(attendance_type.name):
                continue
            kind = "late"
        else:
            kind = "excused" if attendance_type.is_excused_absence else "unexcused"
        subject_id = (
            data.lesson_subjects.get(record.lesson_id) if record.lesson_id is not None else None
        )
        day = days.setdefault(record.date[:10], AbsenceDay(record.date[:10]))
        day.lessons.append(
            AbsenceLesson(
                lesson_no=record.lesson_no,
                subject=data.subjects.get(subject_id) if subject_id is not None else None,
                kind=kind,
                type_name=attendance_type.name,
            )
        )
    covered = justified_dates(data.justifications) if days else set()
    for day in days.values():
        day.lessons.sort(key=lambda lesson: (lesson.lesson_no is None, lesson.lesson_no or 0))
        day.justification_sent = day.count("unexcused") > 0 and day.date in covered
    return [days[key] for key in sorted(days)]
