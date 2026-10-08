"""What to catch up on after an absence.

The latest absence period (consecutive school days with an absence, a
weekend in between doesn't break it), the lessons missed in it with their
topics, the homework given meanwhile, and the day the student came back.
Used by the Lesson topics sensor (`catch_up`), the Catch-up blueprint and
the companion card. Pure functions over `LibrusData`.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from librus_synergia.models import LibrusData

from .coordinator import infer_subject_id, teacher_subject_ids

# Absence days at most this far apart belong to one period (Friday and the
# following Monday are 3 days apart).
_PERIOD_GAP_DAYS = 3
# An absence older than this isn't "to catch up on" any more.
_MAX_AGE_DAYS = 21


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def catch_up(data: LibrusData, today: date) -> dict[str, Any] | None:
    """The latest absence period within the last three weeks, or None."""
    absent: dict[date, list[Any]] = {}
    present_days: set[date] = set()
    for record in data.attendances:
        day = _day(record.date)
        kind = data.attendance_types.get(record.type_id) if record.type_id is not None else None
        if day is None or kind is None:
            continue
        if kind.is_presence_kind:
            present_days.add(day)
        else:
            absent.setdefault(day, []).append(record)
    if not absent:
        return None

    days = sorted(absent)
    end = days[-1]
    if end < today - timedelta(days=_MAX_AGE_DAYS):
        return None
    start = end
    for day in reversed(days[:-1]):
        if (start - day).days > _PERIOD_GAP_DAYS:
            break
        start = day
    period = [d for d in days if start <= d <= end]
    back_on = min((d for d in present_days if d > end), default=None)

    topics = {((t.date or "")[:10], t.lesson_no): t for t in data.lesson_topics}
    lessons: dict[tuple[str, int | None], dict[str, Any]] = {}
    for day in period:
        for record in absent[day]:
            lesson_no = _as_int(record.lesson_no)
            key = (day.isoformat(), lesson_no)
            if key in lessons:
                continue
            topic = topics.get(key)
            subject_id = (
                topic.subject_id
                if topic is not None and topic.subject_id is not None
                else data.lesson_subjects.get(record.lesson_id)
                if record.lesson_id is not None
                else None
            )
            lessons[key] = {
                "date": day.isoformat(),
                "lesson_no": lesson_no,
                "subject": data.subjects.get(subject_id) if subject_id is not None else None,
                "topic": topic.topic if topic is not None and topic.topic else None,
            }

    by_teacher = teacher_subject_ids(data.timetable)
    homework = []
    for item in data.homework_assignments:
        given = _day(item.date)
        if given is None or not start <= given <= end:
            continue
        subject_id = infer_subject_id(item.teacher_id, by_teacher)
        homework.append(
            {
                "id": item.id,
                "date": given.isoformat(),
                "due_date": (item.due_date or "")[:10] or None,
                "subject": data.subjects.get(subject_id) if subject_id is not None else None,
                "topic": item.topic,
                "text": (item.text or "")[:300],
            }
        )

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "days": len(period),
        "back_on": back_on.isoformat() if back_on else None,
        "back_today": back_on == today,
        "lessons": sorted(lessons.values(), key=lambda l: (l["date"], l["lesson_no"] or 0)),
        "homework": sorted(homework, key=lambda h: (h["date"], str(h["id"]))),
    }
