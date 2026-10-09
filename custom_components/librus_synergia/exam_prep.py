"""What to revise for an upcoming test.

A test's material is, in practice, what was taught in that subject since
the previous test in the same subject. Librus has both halves: the Agenda
(`HomeWorks`) says when a test is and in which subject, and `Realizations`
lists every lesson held with its topic. This module joins them; the Next
exam sensor, the Assist tools and the weekly AI summary all use it.

Pure functions over `LibrusData` - no Home Assistant imports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from librus_synergia.models import HomeworkEventData, LibrusData

# Agenda entries that are graded assessments, matched on the category name
# and, when the category doesn't match, on the free-text description: some
# teachers file a "kartkówka" under "Inne" and name it only in the text
# (GitHub issue #1). Best-effort by design - a school naming a test category
# something else isn't picked up; no false positives matters more. "quiz"
# was added on a user's request.
EXAM_RE = re.compile(r"sprawdzian|praca\s+klasowa|kartków|egzamin|diagnoz|quiz", re.IGNORECASE)

# A test rarely covers more than this many lessons; a subject with no
# earlier test this year would otherwise list the whole year so far.
MAX_TOPICS = 25


@dataclass(slots=True)
class RevisionTopic:
    date: str
    lesson_no: int | None
    topic: str
    absent: bool
    # Every lesson with this same topic (a teacher often enters one topic
    # for several lessons); `date` is the first of them, `absent` is true
    # when the student missed any.
    dates: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExamPrep:
    day: date
    item: HomeworkEventData
    subject: str | None
    category: str | None
    # Topics are counted from the day after this date (the previous test in
    # the subject); None when there was no earlier test this year.
    since: date | None
    topics: list[RevisionTopic] = field(default_factory=list)
    more_topics: int = 0

    @property
    def missed(self) -> int:
        return sum(1 for t in self.topics if t.absent)


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


def category_name(data: LibrusData, item: HomeworkEventData) -> str | None:
    return data.homework_categories.get(item.category_id) if item.category_id is not None else None


def is_exam(data: LibrusData, item: HomeworkEventData) -> bool:
    category = category_name(data, item)
    if category is not None and EXAM_RE.search(category):
        return True
    return bool(item.content and EXAM_RE.search(item.content))


def missed_lessons(data: LibrusData) -> set[tuple[str, int | None]]:
    """(date, lesson number) of every lesson the student was absent from."""
    missed = set()
    for record in data.attendances:
        kind = data.attendance_types.get(record.type_id) if record.type_id is not None else None
        if kind is not None and not kind.is_presence_kind:
            missed.add(((record.date or "")[:10], _as_int(record.lesson_no)))
    return missed


def exam_dates_by_subject(data: LibrusData) -> dict[Any, list[date]]:
    """Every test date in the Agenda, per subject id, sorted."""
    out: dict[Any, list[date]] = {}
    for item in data.homeworks:
        day = _day(item.date)
        if day is None or item.subject_id is None or not is_exam(data, item):
            continue
        out.setdefault(item.subject_id, []).append(day)
    for days in out.values():
        days.sort()
    return out


def upcoming_exams(data: LibrusData, today: date) -> list[tuple[date, HomeworkEventData]]:
    """Tests from today on, soonest first."""
    out = []
    for item in data.homeworks:
        day = _day(item.date)
        if day is None or day < today or not is_exam(data, item):
            continue
        out.append((day, item))
    out.sort(key=lambda pair: (pair[0], pair[1].time_from or ""))
    return out


def exam_prep(
    data: LibrusData,
    today: date,
    *,
    limit: int | None = None,
    until: date | None = None,
) -> list[ExamPrep]:
    """Upcoming tests with the topics to revise for each.

    The topics are the lessons held in the test's subject after the
    previous test in that subject (or from the start of the school year)
    and before the test, up to today - a lesson that hasn't happened yet
    has no topic. A test without a subject gets no topics."""
    exams = upcoming_exams(data, today)
    if until is not None:
        exams = [(d, i) for d, i in exams if d <= until]
    if limit is not None:
        exams = exams[:limit]
    if not exams:
        return []
    by_subject = exam_dates_by_subject(data)
    missed = missed_lessons(data)
    # Every held lesson with a topic, per subject, its date parsed once -
    # each test used to walk (and parse) every lesson of the school year.
    held_by_subject: dict[Any, list[tuple[date, Any]]] = {}
    for lesson in data.lesson_topics:
        held = _day(lesson.date)
        if held is None or lesson.subject_id is None or not lesson.topic.strip():
            continue
        held_by_subject.setdefault(lesson.subject_id, []).append((held, lesson))
    year_start = _day(data.school_class.begin_school_year) if data.school_class else None
    result = []
    for day, item in exams:
        subject_id = item.subject_id
        prep = ExamPrep(
            day=day,
            item=item,
            subject=data.subjects.get(subject_id) if subject_id is not None else None,
            category=category_name(data, item),
            since=None,
        )
        if subject_id is not None:
            earlier = [d for d in by_subject.get(subject_id, []) if d < day]
            prep.since = earlier[-1] if earlier else None
            topics = []
            for held, lesson in held_by_subject.get(subject_id, []):
                if held >= day or held > today:
                    continue
                # After the previous test (that day's lesson was the test
                # itself), else from the start of the school year.
                if prep.since is not None and held <= prep.since:
                    continue
                if prep.since is None and year_start is not None and held < year_start:
                    continue
                topics.append(
                    RevisionTopic(
                        date=held.isoformat(),
                        lesson_no=lesson.lesson_no,
                        topic=lesson.topic.strip(),
                        absent=(held.isoformat(), lesson.lesson_no) in missed,
                    )
                )
            topics.sort(key=lambda t: (t.date, t.lesson_no or 0))
            topics = _merge_repeated(topics)
            if len(topics) > MAX_TOPICS:
                prep.more_topics = len(topics) - MAX_TOPICS
                topics = topics[-MAX_TOPICS:]
            prep.topics = topics
        result.append(prep)
    return result


def _merge_repeated(topics: list[RevisionTopic]) -> list[RevisionTopic]:
    """One entry per distinct topic (case and spacing ignored), in the order
    the topic first came up."""
    merged: dict[str, RevisionTopic] = {}
    for topic in topics:
        key = " ".join(topic.topic.casefold().split())
        first = merged.get(key)
        if first is None:
            topic.dates = [topic.date]
            merged[key] = topic
        else:
            first.dates.append(topic.date)
            first.absent = first.absent or topic.absent
    return list(merged.values())


def topics_as_dicts(prep: ExamPrep) -> list[dict[str, Any]]:
    return [
        {
            "date": t.date,
            "lesson_no": t.lesson_no,
            "topic": t.topic,
            "absent": t.absent,
            "dates": t.dates,
            "lessons": len(t.dates),
        }
        for t in prep.topics
    ]
