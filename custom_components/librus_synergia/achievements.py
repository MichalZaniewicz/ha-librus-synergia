"""Badges ("odznaki") for the Achievements card and the achievement event.

Pure functions over one cycle's `LibrusData` - no Home Assistant imports.
Every badge is computed from the whole school year Librus returns, so one
earned before the integration was installed still shows up, with the date
it was really earned. Three can't be dated from Librus data (the honours
forecast, the lucky number and ticked homework): the coordinator stamps
those with the day it first sees them.

A tiered badge (e.g. 5 / 10 / 25 sixes) earns one key per tier
("sixes_10"); a single badge earns its bare key ("first_six"). The keys of
the original ten achievements are unchanged, so existing automations that
match on them keep working.
"""

from __future__ import annotations

import re
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from librus_synergia.models import (
    AttendanceData,
    GradeData,
    LibrusData,
)
from librus_synergia.parsers import parse_grade_value

from .forecast import HONOURS_AVERAGE, average_sums, report_average, subject_forecasts

GOOD_GRADE = 4.0
_TEST_CATEGORY_RE = re.compile(r"sprawdzian|praca klasowa|test", re.IGNORECASE)
_LATE_RE = re.compile(r"późn", re.IGNORECASE)
_EXEMPLARY_BEHAVIOUR = {"wz", "bdb"}
SUBJECT_STAR_AVERAGE = 5.5
SUBJECT_STAR_MIN_GRADES = 3
SUBJECT_ATTENDANCE_LESSONS = 20
PUNCTUAL_DAYS = 30
FULL_MONTH_MIN_RECORDS = 10


@dataclass(slots=True)
class Badge:
    """One badge. `earned` maps an event key to the date (YYYY-MM-DD) it
    was earned, or None when earned but not datable from Librus data."""

    key: str
    title: str
    icon: str
    tiers: tuple[int, ...] = ()
    earned: dict[str, str | None] = field(default_factory=dict)
    value: float | None = None
    target: float | None = None
    unit: str | None = None

    def tier_key(self, tier: int) -> str:
        return f"{self.key}_{tier}"

    def keys(self) -> list[str]:
        """Every event key this badge can earn, lowest tier first."""
        return [self.tier_key(t) for t in self.tiers] if self.tiers else [self.key]


# Titles carried in the achievement event (Polish, like every blueprint's
# text). Tier keys of the original achievements keep their old titles.
TITLES: dict[str, str] = {
    "first_six": "Pierwsza szóstka",
    "sixes": "Kolekcjoner szóstek",
    "good_grade_streak": "Seria dobrych ocen",
    "hat_trick": "Hat-trick",
    "test_ace": "Sprawdzian na 5+",
    "subject_star": "Prymus przedmiotu",
    "honours": "Świadectwo z paskiem",
    "comeback": "Comeback",
    "no_ones": "Semestr bez jedynki",
    "attendance_streak": "Bez nieobecności",
    "full_month": "100% w miesiącu",
    "punctual": "Punktualność",
    "subject_attendance": "Wzorowa frekwencja z przedmiotu",
    "behaviour_streak": "Bez uwagi",
    "praise": "Pochwała",
    "praises": "Seria pochwał",
    "exemplary_behaviour": "Wzorowe zachowanie",
    "lucky": "Szczęśliwy numerek",
    "homework": "Pracowitość",
    "school_year": "Rok ukończony",
}

_TIER_TITLES: dict[str, str] = {
    "first_six": "Pierwsza szóstka!",
    "good_grade_streak_5": "5 dobrych ocen z rzędu",
    "good_grade_streak_10": "10 dobrych ocen z rzędu",
    "good_grade_streak_20": "20 dobrych ocen z rzędu",
    "attendance_streak_7": "Tydzień bez nieobecności",
    "attendance_streak_30": "Miesiąc bez nieobecności",
    "attendance_streak_90": "3 miesiące bez nieobecności",
    "behaviour_streak_7": "Tydzień bez uwagi",
    "behaviour_streak_30": "Miesiąc bez uwagi",
    "behaviour_streak_90": "3 miesiące bez uwagi",
    "sixes_5": "5 szóstek",
    "sixes_10": "10 szóstek",
    "sixes_25": "25 szóstek",
    "praises_3": "3 pochwały",
    "praises_5": "5 pochwał",
    "homework_10": "10 odhaczonych zadań domowych",
    "homework_25": "25 odhaczonych zadań domowych",
}

ICONS: dict[str, str] = {
    "first_six": "mdi:numeric-6-box",
    "sixes": "mdi:star-shooting",
    "good_grade_streak": "mdi:fire",
    "hat_trick": "mdi:crown",
    "test_ace": "mdi:certificate",
    "subject_star": "mdi:school",
    "honours": "mdi:medal",
    "comeback": "mdi:arrow-up-bold-circle",
    "no_ones": "mdi:check-decagram",
    "attendance_streak": "mdi:calendar-check",
    "full_month": "mdi:calendar-star",
    "punctual": "mdi:clock-check-outline",
    "subject_attendance": "mdi:run-fast",
    "behaviour_streak": "mdi:shield-star",
    "praise": "mdi:thumb-up",
    "praises": "mdi:cards-playing-heart-multiple",
    "exemplary_behaviour": "mdi:medal-outline",
    "lucky": "mdi:clover",
    "homework": "mdi:checkbox-marked-circle-outline",
    "school_year": "mdi:flag-checkered",
}


def key_title(key: str) -> str:
    """Event title for one earned key ("sixes_10" -> "10 szóstek")."""
    if key in _TIER_TITLES:
        return _TIER_TITLES[key]
    if key in TITLES:
        return TITLES[key]
    base, _, tier = key.rpartition("_")
    return f"{TITLES.get(base, base)}: {tier}" if base else key


def _day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _iso(value: date) -> str:
    return value.isoformat()


def _day_to_day(grades: list[GradeData]) -> list[GradeData]:
    return [
        g
        for g in grades
        if not g.is_semester_proposition
        and not g.is_final_proposition
        and not g.is_semester
        and not g.is_final
    ]


def _tiered(badge: Badge, dates: list[str]) -> None:
    """Tier N is earned on the N-th date of a chronological list."""
    for tier in badge.tiers:
        if len(dates) >= tier:
            badge.earned[badge.tier_key(tier)] = dates[tier - 1]


def _streak_tiers(
    badge: Badge, breaks: list[date], start: date | None, today: date
) -> None:
    """Calendar days without a break (an absence, a negative note), counted
    from the school year start: tier N is earned on the day the N-th clean
    day passed, the first time any gap was that long. Value = the current
    gap."""
    if start is None:
        start = min(breaks) if breaks else None
    if start is None:
        return
    gap_start = start
    for end in [*(b for b in sorted(set(breaks)) if start <= b <= today), today]:
        for tier in badge.tiers:
            key = badge.tier_key(tier)
            reached = gap_start + timedelta(days=tier)
            if key not in badge.earned and reached <= end:
                badge.earned[key] = _iso(reached)
        if end < today:
            gap_start = max(gap_start, end)
    badge.value = max(0, (today - gap_start).days)


def compute_badges(
    data: LibrusData,
    today: date,
    *,
    thresholds: tuple[float, ...],
    weighted: bool = True,
    student_number: int | None = None,
    homework_done: int = 0,
) -> list[Badge]:
    """Every badge with what's earned (and when) and the progress towards
    the next tier. Order is the card's order."""
    grading = data.grading_system
    categories = data.grade_categories
    school_class = data.school_class
    year_start = _day(school_class.begin_school_year) if school_class else None
    first_semester_end = _day(school_class.end_first_semester) if school_class else None
    year_end = _day(school_class.end_school_year) if school_class else None

    grades = sorted(
        (g for g in _day_to_day(data.grades) if _day(g.add_date)),
        key=lambda g: (g.add_date or "", g.id),
    )
    numeric = [(g, v) for g in grades if (v := parse_grade_value(g.value, grading)) is not None]
    badges: list[Badge] = []

    def badge(key: str, tiers: tuple[int, ...] = (), **kwargs: Any) -> Badge:
        item = Badge(key, TITLES[key], ICONS[key], tiers, **kwargs)
        badges.append(item)
        return item

    # --- grades -----------------------------------------------------
    six_dates = [(g.add_date or "")[:10] for g, v in numeric if v >= 6.0]
    first_six = badge("first_six")
    if six_dates:
        first_six.earned["first_six"] = six_dates[0]

    sixes = badge("sixes", (5, 10, 25), value=len(six_dates), unit="grades")
    _tiered(sixes, six_dates)

    streak_badge = badge("good_grade_streak", (5, 10, 20), unit="grades")
    streak = 0
    for g, value in numeric:
        if value < GOOD_GRADE:
            streak = 0
            continue
        streak += 1
        key = streak_badge.tier_key(streak)
        if streak in streak_badge.tiers and key not in streak_badge.earned:
            streak_badge.earned[key] = (g.add_date or "")[:10]
    streak_badge.value = streak

    hat_trick = badge("hat_trick", target=3, unit="grades")
    six_days = [d for s in six_dates if (d := _day(s))]
    best = 0
    for i, day in enumerate(six_days):
        window = sum(1 for d in six_days[: i + 1] if (day - d).days < 7)
        best = max(best, window)
        if window >= 3 and "hat_trick" not in hat_trick.earned:
            hat_trick.earned["hat_trick"] = _iso(day)
    hat_trick.value = min(best, 3)

    test_ace = badge("test_ace")
    for g, value in numeric:
        category = categories.get(g.category_id) if g.category_id is not None else None
        if value >= 5.0 and category and _TEST_CATEGORY_RE.search(category.name):
            test_ace.earned["test_ace"] = (g.add_date or "")[:10]
            break

    star = badge("subject_star", target=SUBJECT_STAR_AVERAGE, unit="average")
    by_subject: dict[Any, list[GradeData]] = defaultdict(list)
    best_average: float | None = None
    for g, _value in numeric:
        if g.subject_id is None:
            continue
        prefix = by_subject[g.subject_id]
        prefix.append(g)
        if len(prefix) < SUBJECT_STAR_MIN_GRADES:
            continue
        total, weight = average_sums(
            prefix, categories, subject_id=g.subject_id, weighted=weighted, grading=grading
        )
        if not weight:
            continue
        average = total / weight
        if average >= SUBJECT_STAR_AVERAGE and "subject_star" not in star.earned:
            star.earned["subject_star"] = (g.add_date or "")[:10]
    for subject_id, subject_grades in by_subject.items():
        if len(subject_grades) < SUBJECT_STAR_MIN_GRADES:
            continue
        total, weight = average_sums(
            subject_grades, categories, subject_id=subject_id, weighted=weighted, grading=grading
        )
        if weight:
            best_average = max(best_average or 0.0, round(total / weight, 2))
    star.value = best_average

    honours = badge("honours", target=HONOURS_AVERAGE, unit="average")
    report = report_average(subject_forecasts(data, today, thresholds, weighted=weighted))
    honours.value = round(report, 2) if report is not None else None
    if report is not None and report >= HONOURS_AVERAGE:
        honours.earned["honours"] = None

    comeback = badge("comeback")
    by_id = {g.id: g for g in data.grades}
    for g, value in numeric:
        old = by_id.get(g.improves_id) if g.improves_id is not None else None
        old_value = parse_grade_value(old.value, grading) if old else None
        if old_value is not None and value > old_value:
            comeback.earned["comeback"] = (g.add_date or "")[:10]
            break

    no_ones = badge("no_ones", unit="days")
    semesters = [(1, year_start, first_semester_end), (2, first_semester_end, year_end)]
    for number, begin, end in semesters:
        if begin is None or end is None:
            continue
        in_semester = [
            v
            for g, v in numeric
            if g.semester == number
            or (g.semester is None and begin <= (_day(g.add_date) or begin) <= end)
        ]
        has_one = any(v < 2.0 for v in in_semester)
        if end < today:
            if in_semester and not has_one and "no_ones" not in no_ones.earned:
                no_ones.earned["no_ones"] = _iso(end)
        elif begin <= today and not has_one:
            no_ones.value = (today - begin).days
            no_ones.target = (end - begin).days

    # --- attendance -------------------------------------------------
    types = data.attendance_types
    records = sorted(
        (a for a in data.attendances if _day(a.date)), key=lambda a: a.date or ""
    )

    def is_absence(a: AttendanceData) -> bool:
        kind = types.get(a.type_id) if a.type_id is not None else None  # type: ignore[arg-type]
        return kind is not None and not kind.is_presence_kind

    def is_late(a: AttendanceData) -> bool:
        kind = types.get(a.type_id) if a.type_id is not None else None  # type: ignore[arg-type]
        return kind is not None and _LATE_RE.search(kind.name) is not None

    attendance = badge("attendance_streak", (7, 30, 90), unit="days")
    _streak_tiers(
        attendance, [d for a in records if is_absence(a) and (d := _day(a.date))], year_start, today
    )

    full_month = badge("full_month", unit="days")
    months: dict[str, list[AttendanceData]] = defaultdict(list)
    for a in records:
        months[(a.date or "")[:7]].append(a)
    for month, month_records in sorted(months.items()):
        year, mon = int(month[:4]), int(month[5:7])
        last = date(year, mon, monthrange(year, mon)[1])
        clean = not any(is_absence(a) or is_late(a) for a in month_records)
        if last < today:
            if clean and len(month_records) >= FULL_MONTH_MIN_RECORDS:
                full_month.earned.setdefault("full_month", _iso(last))
        elif clean and "full_month" not in full_month.earned:
            full_month.value = today.day
            full_month.target = monthrange(year, mon)[1]

    punctual = badge("punctual", target=PUNCTUAL_DAYS, unit="days")
    late_days = {a.date[:10] for a in records if a.date and is_late(a)}
    run = 0
    for day_text in sorted({(a.date or "")[:10] for a in records}):
        run = 0 if day_text in late_days else run + 1
        if run >= PUNCTUAL_DAYS and "punctual" not in punctual.earned:
            punctual.earned["punctual"] = day_text
    punctual.value = run

    subject_attendance = badge(
        "subject_attendance", target=SUBJECT_ATTENDANCE_LESSONS, unit="lessons"
    )
    per_subject: dict[Any, int] = defaultdict(int)
    spoiled: set[Any] = set()
    for a in records:
        subject = data.lesson_subjects.get(a.lesson_id) if a.lesson_id is not None else None
        if subject is None or subject in spoiled:
            continue
        if is_absence(a):
            spoiled.add(subject)
            continue
        per_subject[subject] += 1
        if (
            per_subject[subject] == SUBJECT_ATTENDANCE_LESSONS
            and "subject_attendance" not in subject_attendance.earned
        ):
            subject_attendance.earned["subject_attendance"] = (a.date or "")[:10]
    clean_counts = [n for s, n in per_subject.items() if s not in spoiled]
    subject_attendance.value = min(max(clean_counts, default=0), SUBJECT_ATTENDANCE_LESSONS)

    # --- behaviour --------------------------------------------------
    notes = sorted((n for n in data.notes if _day(n.date)), key=lambda n: n.date or "")
    behaviour = badge("behaviour_streak", (7, 30, 90), unit="days")
    _streak_tiers(
        behaviour,
        [d for n in notes if n.sentiment == "negative" and (d := _day(n.date))],
        year_start,
        today,
    )

    praise_dates = [(n.date or "")[:10] for n in notes if n.sentiment == "positive"]
    praise = badge("praise")
    if praise_dates:
        praise.earned["praise"] = praise_dates[0]
    praises = badge("praises", (3, 5), value=len(praise_dates), unit="notes")
    _tiered(praises, praise_dates)

    exemplary = badge("exemplary_behaviour")
    for grade in sorted(data.behaviour_grades, key=lambda b: b.add_date or ""):
        if grade.display.lower() in _EXEMPLARY_BEHAVIOUR:
            exemplary.earned["exemplary_behaviour"] = (grade.add_date or "")[:10] or None
            break

    # --- other ------------------------------------------------------
    lucky = badge("lucky")
    number = data.lucky_number
    if number is not None and student_number is not None and number.number == student_number:
        lucky.earned["lucky"] = (number.day or "")[:10] or None

    homework = badge("homework", (10, 25), value=homework_done, unit="tasks")
    for tier in homework.tiers:
        if homework_done >= tier:
            homework.earned[homework.tier_key(tier)] = None

    school_year = badge("school_year", unit="days")
    if year_start and year_end:
        if today >= year_end:
            school_year.earned["school_year"] = _iso(year_end)
        else:
            school_year.value = max(0, (today - year_start).days)
            school_year.target = (year_end - year_start).days

    return badges
