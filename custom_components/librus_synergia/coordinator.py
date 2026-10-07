"""Data update coordinator for the Librus Synergia (unofficial) integration.

Single coordinator, not split by cadence: unlike ha-suunto (live heart rate
vs. sleep/workouts genuinely differ in volatility), Librus data - grades,
attendance, timetable, announcements - all change at "a few times a day at
most" cadence, so one coordinator covers everything. The one exception (the
daily lucky number) is special-cased inline rather than given its own
coordinator, for the same reason ha-suunto special-cases its one-off VO2max
deep-scan inside its normal coordinator instead of adding new machinery for a
single edge case.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from librus_synergia import (
    LibrusApiClient,
    LibrusAuthError,
    LibrusError,
    LibrusSessionExpiredError,
)
from librus_synergia.changes import Changes, ChangeTracker, SeenIds
from librus_synergia.models import (
    AttendanceData,
    AttendanceTypeData,
    ClassData,
    FreeDayData,
    GradeCategoryData,
    GradeData,
    LessonData,
    LibrusData,
    LuckyNumberData,
    MessageData,
    JustificationData,
    NoteData,
    PointGradeData,
    SchoolData,
)

# Parsers live in the `librus-synergia` library. `merge_timetables`,
# `decode_message_content`, `resolve_sender_name` and `parse_grade_value`
# are re-exported from here for calendar.py/services.py/sensor.py.
from librus_synergia.parsers import (  # noqa: F401
    collect_lid_user_identifiers,
    decode_message_content,
    extract_token_user_identifier,
    merge_timetables,
    parse_attendance_types,
    parse_attendances,
    parse_behaviour_grades,
    parse_class,
    parse_comment_text_map,
    parse_descriptive_grades,
    parse_free_days,
    parse_grade_categories,
    parse_grade_value,
    parse_grades,
    parse_homework_assignments,
    parse_homeworks,
    parse_id_name_map,
    parse_kindergarten_activity_types,
    parse_kindergarten_classrooms,
    parse_kindergarten_group,
    parse_kindergarten_teachers,
    parse_lesson_subjects,
    parse_lucky_number,
    parse_me,
    parse_message_list,
    parse_messages,
    parse_notes,
    parse_parent_teacher_conferences,
    parse_justifications,
    parse_realizations,
    parse_school_files,
    parse_school_trips,
    parse_text_grade_categories,
    parse_text_grades,
    parse_user_class_register_number,
    parse_point_grade_categories,
    parse_point_grades,
    parse_school,
    parse_school_notices,
    parse_student_number,
    point_grades_enabled,
    resolve_sender_name,
)

from .const import (
    AVERAGE_MODE_ARITHMETIC,
    CONF_ANNOUNCEMENTS_ENABLED,
    CONF_AVERAGE_MODE,
    CONF_BEHAVIOUR_GRADES_ENABLED,
    CONF_DESCRIPTIVE_GRADES_ENABLED,
    CONF_FREE_DAYS_ENABLED,
    CONF_GRADE_THRESHOLDS,
    CONF_MESSAGES_ENABLED,
    CONF_QUIET_HOURS_ENABLED,
    CONF_QUIET_HOURS_END,
    CONF_QUIET_HOURS_START,
    CONF_SMART_POLLING,
    CORE_ENDPOINT_LABELS,
    DEFAULT_ANNOUNCEMENTS_ENABLED,
    DEFAULT_AVERAGE_MODE,
    DEFAULT_BEHAVIOUR_GRADES_ENABLED,
    DEFAULT_DESCRIPTIVE_GRADES_ENABLED,
    DEFAULT_FREE_DAYS_ENABLED,
    DEFAULT_MESSAGES_ENABLED,
    DEFAULT_QUIET_HOURS_ENABLED,
    DEFAULT_QUIET_HOURS_END,
    DEFAULT_QUIET_HOURS_START,
    DEFAULT_SMART_POLLING,
    DOMAIN,
    EVENT_ACHIEVEMENT_UNLOCKED,
    EVENT_AGENDA_CHANGED,
    EVENT_FORECAST_CHANGED,
    EVENT_JUSTIFICATION_STATUS,
    EVENT_NEW_SCHOOL_DOCUMENT,
    EVENT_NEW_SCHOOL_TRIP,
    EVENT_NEW_ABSENCE,
    EVENT_NEW_ANNOUNCEMENT,
    EVENT_NEW_GRADE,
    EVENT_NEW_HOMEWORK,
    EVENT_NEW_HOMEWORK_ASSIGNMENT,
    EVENT_NEW_MESSAGE,
    EVENT_NEW_NOTE,
    EVENT_TIMETABLE_CHANGED,
    ISSUE_OPTIONAL_ENDPOINT_DEGRADED,
    ISSUE_SCHOOL_YEAR_ROLLOVER,
    LAST_GOOD_DATA_MAX_AGE,
    LUCKY_NUMBER_PUBLISH_HOUR,
    OPTIONAL_ENDPOINT_LABELS,
    OUTAGE_BACKOFF_MAX,
    REFERENCE_DATA_ENDPOINT_LABELS,
    SMART_POLLING_DAY_OFF,
    SMART_POLLING_NIGHT,
    SMART_POLLING_NIGHT_END,
    SMART_POLLING_NIGHT_START,
    STATE_SAVE_DELAY,
    STATE_STORE_VERSION,
    STATUS_DEGRADED,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_STALE,
)
from .forecast import average_sums, forecast_basis, parse_thresholds, subject_forecasts

if TYPE_CHECKING:
    from .ai_summary import LibrusWeeklySummary

_LOGGER = logging.getLogger(__name__)

# Label for degraded-endpoint tracking of the informacja web page (listed
# in const.MISC_DEGRADABLE_ENDPOINT_LABELS too).
STUDENT_INFO_LABEL = "Informacja"

# Kindergarten discovery (see `_async_maybe_discover_kindergarten`): how
# long to wait before trying again after finding nothing, and how many
# candidate LIDs to probe per attempt.
_KINDERGARTEN_DISCOVERY_RETRY = timedelta(hours=24)
_KINDERGARTEN_MAX_CANDIDATES = 6

# The order of `_async_fetch_core_payloads`' result: Me, tier 1, tier 2.
_CORE_PAYLOAD_LABELS = ("Me", *CORE_ENDPOINT_LABELS, *OPTIONAL_ENDPOINT_LABELS)
# Optional endpoints parsed in _build_data from their raw payloads.
_EXTRA_LABELS = ("BaseTextGrades", "Realizations", "SchoolTrips", "SchoolFiles")
# Lesson topics, trips and school documents change a few times a day.
_HOURLY = timedelta(hours=1)


def school_file_url(path: str | None) -> str | None:
    """Absolute Synergia URL of a school document's download path."""
    if not path:
        return None
    return path if path.startswith("http") else f"https://synergia.librus.pl{path}"


# Labels of the two point-grade requests (const.MISC_DEGRADABLE_ENDPOINT_LABELS).
_POINT_GRADE_LABELS = ("PointGrades", "PointGrades/Categories")
# `_async_get_messages`' result when nothing was fetched.
_NO_MESSAGES: tuple[Any, ...] = (0, {}, [], [], [], [])


# Agenda fields whose change fires EVENT_AGENDA_CHANGED (names resolved
# alongside, but compared by id so a renamed category isn't a change).
_AGENDA_COMPARED = ("date", "time_from", "content", "category_id", "subject_id")


def _agenda_fields(item: Any, data: LibrusData) -> dict[str, Any]:
    """One Agenda entry as a JSON-able dict (saved, and the event payload)."""
    return {
        "date": item.date,
        "time_from": item.time_from,
        "content": (item.content or "")[:500],
        "category_id": item.category_id,
        "category": data.homework_categories.get(item.category_id)
        if item.category_id is not None
        else None,
        "subject_id": item.subject_id,
        "subject": data.subjects.get(item.subject_id) if item.subject_id is not None else None,
    }


def lesson_change(day: date, lesson: LessonData, data: LibrusData) -> dict[str, Any]:
    """What a substituted lesson changes compared with the plan (Librus
    flags room changes and moved lessons as substitutions too, with the
    original in `lesson.original`). `kind` is `canceled`, `substitution`
    (another teacher or subject), `room_change`, `moved` or None for an
    ordinary lesson; the rest are resolved names (None when unknown)."""
    original = lesson.original

    def name(lookup: dict[Any, str], key: Any) -> str | None:
        return lookup.get(key) if key is not None else None

    result: dict[str, Any] = {
        "kind": None,
        "room_changed": lesson.room_changed,
        "classroom": name(data.classrooms, lesson.classroom_id),
        "original_classroom": name(data.classrooms, original.classroom_id) if original else None,
        "original_subject": name(data.subjects, original.subject_id) if original else None,
        "original_teacher": name(data.teachers, original.teacher_id) if original else None,
        "original_date": original.date if original else None,
        "original_lesson_no": original.lesson_no if original else None,
    }
    if lesson.is_canceled:
        result["kind"] = "canceled"
    elif lesson.is_substitution:
        result["kind"] = "substitution"
        if original is not None:
            same_lesson = (original.subject_id in (None, lesson.subject_id)) and (
                original.teacher_id in (None, lesson.teacher_id)
            )
            moved = (original.date is not None and original.date[:10] != day.isoformat()) or (
                original.lesson_no is not None and original.lesson_no != lesson.lesson_no
            )
            if same_lesson and moved:
                result["kind"] = "moved"
            elif same_lesson and lesson.room_changed:
                result["kind"] = "room_change"
    return result


def state_store_key(entry_id: str) -> str:
    """Storage key of one entry's saved coordinator state."""
    return f"{DOMAIN}.{entry_id}.state"


def _restore_id(key: str) -> int | str:
    """JSON object keys are strings - subject ids are ints."""
    try:
        return int(key)
    except ValueError:
        return key


def optional_endpoint_issue_id(entry_id: str, label: str) -> str:
    """Stable repair-issue id for one entry's one supplementary-endpoint
    degradation. Public (not underscore-prefixed) - `__init__.py::
    async_remove_entry` needs to compute the same ids to clear them when a
    config entry is deleted for good, without needing a live coordinator."""
    return f"{ISSUE_OPTIONAL_ENDPOINT_DEGRADED}_{entry_id}_{label.lower().replace('/', '_')}"


def school_year_issue_id(entry_id: str) -> str:
    """Stable repair-issue id for one entry's school-year-rollover check -
    see `optional_endpoint_issue_id`'s docstring for why this is public."""
    return f"{ISSUE_SCHOOL_YEAR_ROLLOVER}_{entry_id}"


# "Dobra" ocena, for the good-grade-streak sensor/achievements below - 4
# ("dobry") and up. Not user-configurable (unlike e.g. the Low Grade Alert
# blueprint's own threshold input) - keeping this one fixed avoids a
# second, subtly different "what counts as good" knob to document.
_GOOD_GRADE_STREAK_THRESHOLD = 4.0


def good_grade_streak(grades: list[GradeData]) -> int:
    """Consecutive most-recent NUMERIC grades >= _GOOD_GRADE_STREAK_THRESHOLD,
    counting back from the newest until the first one below it. Semester/
    final grades (proposed OR actual) excluded (not day-to-day grades,
    same as the average calculation). A non-numeric mark (bz/np/...) is
    SKIPPED, not counted as breaking the streak - it isn't really a "bad
    grade", just an administrative mark, and penalizing it would feel
    unfair for what this is meant to be: a small, motivating "passa" a
    student can watch grow."""
    dated = sorted(
        (
            g
            for g in grades
            if g.add_date
            and not g.is_semester_proposition
            and not g.is_final_proposition
            and not g.is_semester
            and not g.is_final
        ),
        key=lambda g: g.add_date,
        reverse=True,
    )
    streak = 0
    for grade in dated:
        value = parse_grade_value(grade.value)
        if value is None:
            continue
        if value < _GOOD_GRADE_STREAK_THRESHOLD:
            break
        streak += 1
    return streak


def _days_since(dates: list[str], school_class: ClassData | None, today: date) -> int | None:
    """Shared by days_since_last_absence/days_since_last_negative_note
    below - falls back to days since the school year started
    (`ClassData.begin_school_year`) when `dates` is empty, so a student
    with a genuinely perfect record shows a real, growing streak instead
    of `unknown`. `None` only when there's truly nothing to anchor to."""
    reference = max(dates) if dates else (school_class.begin_school_year if school_class else None)
    if not reference:
        return None
    try:
        reference_date = date.fromisoformat(reference[:10])
    except ValueError:
        return None
    return max(0, (today - reference_date).days)


def days_since_last_absence(
    attendances: list[AttendanceData],
    attendance_types: dict[int, AttendanceTypeData],
    school_class: ClassData | None,
    today: date,
) -> int | None:
    dates = [
        a.date
        for a in attendances
        if a.date and (t := attendance_types.get(a.type_id)) is not None and not t.is_presence_kind
    ]
    return _days_since(dates, school_class, today)


def days_since_last_negative_note(
    notes: list[NoteData], school_class: ClassData | None, today: date
) -> int | None:
    dates = [n.date for n in notes if n.date and n.sentiment == "negative"]
    return _days_since(dates, school_class, today)


# Milestone thresholds for the streak-based achievements fired by
# `LibrusDataUpdateCoordinator._check_achievements` - crossing one fires
# EVENT_ACHIEVEMENT_UNLOCKED exactly once.
_GOOD_GRADE_STREAK_MILESTONES = (5, 10, 20)
_STREAK_DAY_MILESTONES = (7, 30, 90)

# Human-readable Polish titles carried in the event payload
# (`{{ trigger.event.data.title }}`). Achievements are this integration's
# own invention - Librus has no such concept, so there's no "real" name to
# resolve from account data the way EVENT_NEW_GRADE etc. resolve a subject
# name - hardcoded Polish, matching every blueprint's own briefing/digest
# text elsewhere in this codebase (this integration is Poland-only by
# nature, Librus itself being Polish-schools-only).
_ACHIEVEMENT_TITLES: dict[str, str] = {
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
}


def teacher_subject_ids(timetable: dict[date, list[LessonData]]) -> dict[Any, set[Any]]:
    """teacher id -> every subject id that teacher has in the cached
    (current + next week) timetable, counting every teacher of a split
    lesson."""
    result: dict[Any, set[Any]] = {}
    for lessons in timetable.values():
        for lesson in lessons:
            if lesson.subject_id is None:
                continue
            teacher_ids = lesson.teacher_ids or (
                (lesson.teacher_id,) if lesson.teacher_id is not None else ()
            )
            for teacher_id in teacher_ids:
                result.setdefault(teacher_id, set()).add(lesson.subject_id)
    return result


def infer_subject_id(teacher_id: Any, by_teacher: dict[Any, set[Any]]) -> Any:
    """The subject a homework assignment belongs to, inferred from its
    teacher - `HomeWorkAssignments` has NO Subject field (CONFIRMED live,
    2026-10-03). Only when that teacher teaches exactly one subject in the
    timetable; a teacher with two subjects (e.g. Informatyka + WF) gives
    `None` rather than a guess."""
    subjects = by_teacher.get(teacher_id) if teacher_id is not None else None
    return next(iter(subjects)) if subjects and len(subjects) == 1 else None


def grade_improvements(grades: list[GradeData]) -> tuple[dict[int, str], set[int]]:
    """Corrections ("poprawy"): `{grade_id: value of the grade it improves}`
    for every correction, and the ids of grades that were improved later.
    The link is `Grades[].Improvement.Id` (`GradeData.improves_id`); the
    earlier grade stays in the list and keeps counting the way Librus
    reports it - this only labels the pair."""
    by_id = {g.id: g for g in grades}
    improves: dict[int, str] = {}
    improved: set[int] = set()
    for grade in grades:
        old_id = grade.improves_id
        if old_id is None:
            continue
        improved.add(old_id)
        old = by_id.get(old_id)
        if old is not None:
            improves[grade.id] = old.value
    return improves, improved


def _grade_event_details(
    grade: GradeData,
    categories: dict[int, GradeCategoryData],
    improves: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Extra `librus_synergia_new_grade` fields (issue #12) - all from data
    already fetched this cycle, no extra Librus request. `kind` says whether
    this is an ordinary grade or a semester/final one (or its proposition),
    so a notification can say "Propozycja oceny semestralnej" instead of
    presenting it like any other grade."""
    category = categories.get(grade.category_id) if grade.category_id is not None else None
    if grade.is_final:
        kind = "final"
    elif grade.is_final_proposition:
        kind = "final_proposition"
    elif grade.is_semester:
        kind = "semester"
    elif grade.is_semester_proposition:
        kind = "semester_proposition"
    else:
        kind = "normal"
    return {
        "category": category.name if category else None,
        "weight": category.weight if category else None,
        "counts_to_average": category.count_to_average if category else None,
        "comments": list(grade.comments),
        "date": grade.add_date,
        "semester": grade.semester,
        "kind": kind,
        # The value of the earlier grade this one corrects, None otherwise.
        "improves": (improves or {}).get(grade.id),
    }


def calculate_average(
    grades: list[GradeData],
    categories: dict[int, GradeCategoryData],
    *,
    subject_id: int | None = None,
    semester: int | None = None,
    weighted: bool = True,
) -> float | None:
    """Grade average, excluding semester/final entries (proposed OR
    actual - see GradeData.is_semester/is_final's own docstring for why
    the actual ones matter too, not just the propositions) and
    categories marked as not counting toward the average. Weighted by the
    grade category's weight unless `weighted=False` (plain arithmetic
    mean of the same counted grades). `semester` restricts to grades from
    that semester when given."""
    running, weight_total = average_sums(
        grades, categories, subject_id=subject_id, semester=semester, weighted=weighted
    )
    if weight_total <= 0:
        return None
    return round(running / weight_total, 2)


def _teacher_name(data: LibrusData, teacher_id: Any) -> str | None:
    return data.teachers.get(teacher_id) if teacher_id is not None else None


class LibrusDataUpdateCoordinator(DataUpdateCoordinator[LibrusData]):
    """Fetches everything Librus Synergia exposes for one student."""

    # Set by async_setup_entry while the weekly AI summary is configured.
    weekly_summary: LibrusWeeklySummary | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: LibrusApiClient,
        scan_interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"Librus Synergia ({client.username})",
            update_interval=scan_interval,
            config_entry=entry,
        )
        self._client = client

        # Subjects/teachers/classrooms are near-static reference data -
        # refetched at most once a day rather than every cycle.
        self._reference_data_fetched_at: datetime | None = None
        # Smart polling (see _smart_polling_skip): when the last real fetch
        # happened, and a one-shot override for a manual refresh.
        self._last_fetch_at: datetime | None = None
        self._force_next_fetch = False
        self._cached_subjects: dict[int | str, str] = {}
        self._cached_teachers: dict[int | str, str] = {}
        self._cached_classrooms: dict[int | str, str] = {}
        self._cached_lesson_subjects: dict[int, int] = {}
        # Class register number ("nr w dzienniku") from the student's own
        # Users record (informacja web page as a fallback), refreshed with the
        # rest of the reference data. The Configure-dialog value wins.
        self.student_number_from_librus: int | None = None
        # Kindergarten (przedszkole) accounts - issue #5 / PR #8. Their
        # standard `Timetables` 403s; the real timetable lives in a separate
        # `/gateway/ms/kindergartens/...` API keyed by the CHILD's LID
        # (`LID-AUTH-USER-...`), which has to be discovered - see
        # `_async_maybe_discover_kindergarten`. `None` = not a kindergarten
        # account (or not discovered yet).
        self._kindergarten_lid: str | None = None
        self._kindergarten_group_id: str | None = None
        self._kindergarten_source: str | None = None
        self._kindergarten_next_discovery: datetime | None = None
        # Set by `_fetch_timetable_or_unpublished` on a confirmed 403 - the
        # ONLY trigger for kindergarten discovery, so an ordinary account
        # (whose Timetables works) never makes a single extra request.
        self._timetable_forbidden = False
        self._cached_school: SchoolData | None = None
        self._cached_class: ClassData | None = None
        self._cached_homework_categories: dict[int, str] = {}
        self._cached_free_days: list[FreeDayData] = []
        self._cached_note_categories: dict[int, str] = {}
        self._cached_behaviour_grade_categories: dict[int, str] = {}

        # The lucky number is normally published once a day; skip refetching
        # it before LUCKY_NUMBER_PUBLISH_HOUR once today's value is cached.
        self._cached_lucky_number: LuckyNumberData | None = None
        self._lucky_number_fetched_date: date | None = None

        # Wiadomości (messages) needs a one-time-per-login bootstrap (a
        # separate session cookie on wiadomosci.librus.pl) - not every cycle,
        # and not every school has this module enabled, so failure here is
        # non-fatal (see _async_get_messages).
        self._messages_bootstrapped = False
        self._messages_available = False

        # New-item bus events: the library's ChangeTracker remembers what
        # has been seen and reports what's new. In-memory only - the first
        # cycle just seeds it instead of replaying history as "new" on
        # install, and a HA restart re-seeds quietly (same tradeoff
        # ha-suunto makes for its EVENT_NEW_WORKOUT tracking).
        self._change_tracker = ChangeTracker()
        # Achievement keys already unlocked (e.g. "good_grade_streak_10") -
        # same seed-silently-then-union pattern as the tracker above, for
        # a small fixed vocabulary of milestones (the library knows nothing
        # about achievements). See _check_achievements.
        self._known_achievements: set[str] | None = None
        # subject id -> forecast grade at the previous poll (None = not
        # seeded yet), and the basis it was computed on.
        self._known_forecast: dict[int, int] | None = None
        self._known_forecast_basis: str | None = None
        # Real homework assignments - the library's ChangeTracker doesn't
        # cover HomeWorkAssignments, so same seed-then-union set as above.
        self._known_homework_assignment_ids: set[Any] | None = None
        # Agenda entry id -> its last seen fields (see
        # _fire_agenda_change_events); None until the first sync.
        self._known_agenda: dict[str, dict[str, Any]] | None = None
        # Justification id -> its last seen status (None until first sync).
        self._known_justifications: dict[str, str] | None = None
        # Ids already announced per kind (text grades, school trips, school
        # documents); None until the first sync, which only seeds them.
        self._known_items: dict[str, set[Any] | None] = {
            "text_grades": None,
            "school_trips": None,
            "school_files": None,
        }
        # When each throttled optional endpoint last answered (see
        # _async_optional).
        self._fetched_at: dict[str, datetime] = {}
        self._cached_text_grade_categories: dict[int, tuple[str, bool]] = {}
        self._cached_homework_assignment_categories: dict[int | str, str] = {}

        # First-failure timestamp per OPTIONAL_ENDPOINT_LABELS entry - used
        # to raise a repair issue only once a supplementary endpoint has
        # failed on EVERY attempt for a week straight (not a single
        # hiccup). In-memory only, same "a HA restart just re-seeds
        # quietly" tradeoff as the new-item id sets above - a restart just
        # restarts the 7-day countdown, which is fine for something this
        # low-stakes.
        self._optional_endpoint_first_failure: dict[str, datetime] = {}

        # Saved across restarts (see async_restore_state): the last good
        # response per endpoint label and per timetable week - the fallback
        # when one fails, or when Librus is down while HA starts.
        self._state_store: Store[dict[str, Any]] = Store(
            hass, STATE_STORE_VERSION, state_store_key(entry.entry_id)
        )
        self._last_good: dict[str, Any] = {}
        self._timetable_cache: dict[str, Any] = {}
        # Health of the last cycles, for the Status / Last update sensors.
        self.last_success_at: datetime | None = None
        self.last_attempt_at: datetime | None = None
        self.last_error: str | None = None
        self.failures = 0
        self.next_attempt_at: datetime | None = None
        # "live" (fetched this cycle), "stale" (Librus failed, showing the
        # last data) or "cache" (rebuilt from the saved responses at start).
        self.data_source = "live"
        # Sections shown from their saved copy this cycle (they failed).
        self.fallback_sections: set[str] = set()
        # Whether the school grades in points (Units); None until known.
        self.point_grades_enabled: bool | None = None

    async def async_restore_state(self) -> None:
        """Load what the previous run saved: the ids already announced (so
        a grade added while HA was off still fires its event instead of
        being swallowed by the silent first-poll seeding), the last good
        responses and when Librus last answered. A missing or unreadable
        file just means a fresh start."""
        try:
            stored = await self._state_store.async_load()
        except Exception:  # noqa: BLE001 - a corrupt file must not block setup
            _LOGGER.warning("Could not read the saved Librus state - starting fresh", exc_info=True)
            stored = None
        if not isinstance(stored, dict):
            return
        if isinstance(seen := stored.get("seen"), dict):
            self._change_tracker = ChangeTracker(SeenIds.from_dict(seen))
        if isinstance(agenda := stored.get("agenda"), dict):
            self._known_agenda = agenda
        if isinstance(statuses := stored.get("justifications"), dict):
            self._known_justifications = statuses
        if isinstance(items := stored.get("known_items"), dict):
            for kind in self._known_items:
                if isinstance(ids := items.get(kind), list):
                    self._known_items[kind] = set(ids)
        if isinstance(ids := stored.get("homework_assignment_ids"), list):
            self._known_homework_assignment_ids = set(ids)
        if isinstance(keys := stored.get("achievements"), list):
            self._known_achievements = {str(k) for k in keys}
        forecast = stored.get("forecast")
        if isinstance(forecast, dict) and isinstance(forecast.get("values"), dict):
            self._known_forecast = {
                _restore_id(k): v for k, v in forecast["values"].items() if isinstance(v, int)
            }
            self._known_forecast_basis = forecast.get("basis")
        if isinstance(payloads := stored.get("payloads"), dict):
            self._last_good = payloads
            if isinstance(units := payloads.get("Units"), dict):
                self.point_grades_enabled = point_grades_enabled(units)
        if isinstance(weeks := stored.get("timetable"), dict):
            self._timetable_cache = weeks
        if isinstance(number := stored.get("student_number"), int):
            self.student_number_from_librus = number
        if saved := stored.get("last_success_at"):
            self.last_success_at = dt_util.parse_datetime(saved)

    def _state_to_save(self) -> dict[str, Any]:
        tracker = self._change_tracker
        return {
            "seen": tracker.seen.to_dict() if tracker.is_seeded else None,
            "agenda": self._known_agenda,
            "justifications": self._known_justifications,
            "known_items": {
                kind: sorted(ids, key=str) if ids is not None else None
                for kind, ids in self._known_items.items()
            },
            "homework_assignment_ids": (
                sorted(self._known_homework_assignment_ids, key=str)
                if self._known_homework_assignment_ids is not None
                else None
            ),
            "achievements": (
                sorted(self._known_achievements) if self._known_achievements is not None else None
            ),
            "forecast": (
                {
                    "basis": self._known_forecast_basis,
                    "values": {str(k): v for k, v in self._known_forecast.items()},
                }
                if self._known_forecast is not None
                else None
            ),
            "payloads": self._last_good,
            "timetable": self._timetable_cache,
            "student_number": self.student_number_from_librus,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
        }

    @property
    def status(self) -> str:
        """Overall health for the Status sensor: `error` while the entities
        are unavailable, `stale` while Librus fails and the last data is
        shown, `degraded` when some section failed this cycle and its last
        good copy is shown, `ok` otherwise."""
        if not self.last_update_success:
            return STATUS_ERROR
        if self.data_source != "live":
            return STATUS_STALE
        if self.fallback_sections:
            return STATUS_DEGRADED
        return STATUS_OK

    @property
    def client(self) -> LibrusApiClient:
        """Expose the client so calendar entities can fetch arbitrary weeks
        on demand (dashboards can ask CalendarEntity.async_get_events for
        ranges outside this coordinator's current+next-week cache)."""
        return self._client

    @property
    def is_kindergarten(self) -> bool:
        """Whether this account's timetable comes from the kindergarten API."""
        return self._kindergarten_lid is not None

    @property
    def kindergarten_diagnostics(self) -> dict[str, Any]:
        """Kindergarten discovery state for `diagnostics.py` - no LIDs."""
        return {
            "detected": self._kindergarten_lid is not None,
            "source": self._kindergarten_source,
            "group_known": self._kindergarten_group_id is not None,
            "timetable_forbidden": self._timetable_forbidden,
            "next_discovery": (
                self._kindergarten_next_discovery.isoformat()
                if self._kindergarten_next_discovery
                else None
            ),
        }

    @property
    def reference_data_fetched_at(self) -> datetime | None:
        """When `_async_refresh_reference_data` last completed (`None` if
        never yet, e.g. right after setup). Public for `diagnostics.py` -
        reference data (subjects/teachers/school/class/...) is only
        refreshed at most once every 24h, so this tells a diagnostics
        reader whether it's looking at genuinely fresh data or something
        cached from up to a day ago."""
        return self._reference_data_fetched_at

    @property
    def degraded_endpoints(self) -> dict[str, datetime]:
        """Snapshot of `_optional_endpoint_first_failure` - label -> the
        timestamp it started failing (cleared on recovery). Public so
        `diagnostics.py` can surface it: for an account where several
        endpoints are degrading (a confirmed-403 module-unavailable case,
        or a genuinely flaky one), this is the single most direct answer
        to "what's actually going on" - a `{}` here means every degradable
        endpoint succeeded on the last cycle. A copy, not the live dict,
        so a diagnostics consumer can't accidentally mutate coordinator
        state.

        Covers all four groups that can degrade to empty instead of
        failing the whole cycle: OPTIONAL_ENDPOINT_LABELS (tier 2 of the
        core fetch), CORE_ENDPOINT_LABELS (tier 1), REFERENCE_DATA_
        ENDPOINT_LABELS, and MISC_DEGRADABLE_ENDPOINT_LABELS (Timetable/
        LuckyNumbers/Messages/Messages-Secondary, each guarding its own
        call outside any shared gather). BUG FIX (live feedback, issue
        #5's account): the last three groups were NOT covered when this
        property was first added - only OPTIONAL_ENDPOINT_LABELS/
        CORE_ENDPOINT_LABELS were, which made the very diagnostics dump
        built to debug that account's degraded state genuinely
        incomplete (it couldn't explain why Class was unknown while
        School wasn't, since reference-data failures were silently
        DEBUG-logged only). Should have covered every degrade path from
        the start rather than needing a second round to notice the gap."""
        return dict(self._optional_endpoint_first_failure)

    async def _fetch_timetable_or_unpublished(self, week_start: date) -> dict[str, Any]:
        """Fetch one week's raw `Timetable` payload, treating a CONFIRMED
        403 as "this class's timetable isn't published yet" (issue #4,
        reported live) rather than a session problem - Synergia's own web
        UI shows an explicit "Plan lekcji klasy ... nie został jeszcze
        opublikowany" message for this exact case, and a fresh re-login
        can never fix it (the login itself succeeds fine, as reported). A
        genuine 401 still means the session actually died and is left to
        propagate, so the normal forced-relogin-and-retry-once recovery
        (see `_async_update_data` / `async_fetch_timetable_week`) still
        runs for that case.

        BUG FIX (live feedback, issue #5's account): this predates
        `degraded_endpoints`/the repair-issue tracking and never fed it -
        a persistently-403ing Timetable was invisible in diagnostics even
        though the calendar was correctly showing empty. Now tracked
        under the "Timetable" label, same as every other degrade path.
        Called from both TIER 1 (this week/next week) and the on-demand
        `async_fetch_timetable_week` path - both count as the same
        endpoint for tracking purposes.

        Kindergarten accounts (issue #5 / PR #8): once the child's LID is
        known, the week comes from the kindergarten API instead - its
        `timetableEntries` payload is understood by `merge_timetables`
        directly, so callers don't branch on account type. Same 403 degrade,
        same "Timetable" label."""
        try:
            if self._kindergarten_lid is not None:
                payload = await self._client.async_get_kindergarten_timetable(
                    self._kindergarten_lid, week_start, week_start + timedelta(days=6)
                )
            else:
                payload = await self._client.async_get_timetable(week_start)
        except LibrusSessionExpiredError as err:
            if err.status_code == 403:
                self._note_optional_endpoint_failure("Timetable")
                if self._kindergarten_lid is None:
                    self._timetable_forbidden = True
                return {}
            raise
        except LibrusError:
            # A transient failure (timeout, 5xx, garbled response): the last
            # good copy of that week beats failing the whole cycle.
            cached = self._timetable_cache.get(week_start.isoformat())
            if cached is None:
                raise
            _LOGGER.debug("Timetable %s fetch failed - using the saved copy", week_start)
            self._note_optional_endpoint_failure("Timetable")
            self.fallback_sections.add("Timetable")
            return cached
        self._timetable_forbidden = False
        self._note_optional_endpoint_recovery("Timetable")
        self._remember_timetable_week(week_start, payload)
        return payload

    def _remember_timetable_week(self, week_start: date, payload: dict[str, Any]) -> None:
        """Keep the last good copy of the current and the next week (the two
        the coordinator polls; weeks a dashboard browses to are not kept)."""
        this_week = dt_util.now().date()
        this_week -= timedelta(days=this_week.weekday())
        if not this_week <= week_start <= this_week + timedelta(days=7):
            return
        self._timetable_cache[week_start.isoformat()] = payload
        for key in [k for k in self._timetable_cache if k < this_week.isoformat()]:
            del self._timetable_cache[key]

    async def _async_maybe_discover_kindergarten(self, me_payload: dict[str, Any]) -> bool:
        """Try to find a kindergarten child's LID, returning True only when
        one was newly found (so the caller refetches this cycle's timetable).

        Gated on the standard `Timetables` having just 403'd - a regular
        student account never gets here. Also rate-limited to once per
        `_KINDERGARTEN_DISCOVERY_RETRY`, so a school whose ordinary timetable
        is simply unpublished (issue #4, also a 403) costs a handful of
        requests a day, not every cycle. Every probe is non-fatal: nothing
        here can raise into the update cycle or trigger reauth (the original
        PR #8 version re-raised a 403 from these auxiliary endpoints, which
        the coordinator would have treated as a dead session)."""
        if self._kindergarten_lid is not None or not self._timetable_forbidden:
            return False
        now = dt_util.utcnow()
        if self._kindergarten_next_discovery is not None and now < self._kindergarten_next_discovery:
            return False
        self._kindergarten_next_discovery = now + _KINDERGARTEN_DISCOVERY_RETRY
        if not await self._async_discover_kindergarten(me_payload):
            _LOGGER.debug("Timetables is forbidden and no kindergarten timetable was found")
            return False
        _LOGGER.info(
            "Kindergarten account detected - using the kindergarten timetable API (via %s)",
            self._kindergarten_source,
        )
        # Pick up activity names/classrooms/group in this same cycle rather
        # than up to 24h later.
        self._reference_data_fetched_at = None
        return True

    async def _async_probe(self, coro: Any) -> dict[str, Any]:
        """Await one discovery request, degrading ANY Librus error to `{}`."""
        try:
            result = await coro
        except LibrusError as err:
            _LOGGER.debug("Kindergarten discovery probe failed: %s", err)
            return {}
        return result if isinstance(result, dict) else {}

    async def _async_discover_kindergarten(self, me_payload: dict[str, Any]) -> bool:
        """Collect candidate `LID-AUTH-USER-...` identifiers and keep the
        first one the kindergarten timetable endpoint returns entries for.

        A parent login can expose both the parent's and the child's LID, and
        the timetable endpoint is the decisive check between them (PR #8's
        finding, from Synergia's own web UI). Candidate sources, in order:
        `/Me`, `Auth/TokenInfo` (+ `Auth/UserInfo/<lid>`), and `Users/<id>`
        for the account's own numeric ids."""
        candidates: dict[str, str] = {}  # lid -> where it came from

        def add(values: list[str], source: str) -> None:
            for value in values:
                candidates.setdefault(value, source)

        me = me_payload.get("Me") if isinstance(me_payload, dict) else None
        me = me if isinstance(me, dict) else {}
        add(collect_lid_user_identifiers(me.get("User")), "Me.User")
        add(collect_lid_user_identifiers(me), "Me")

        token_info = await self._async_probe(self._client.async_get_token_info())
        token_lid = extract_token_user_identifier(token_info)
        if token_lid:
            add([token_lid], "Auth/TokenInfo")
            user_info = await self._async_probe(self._client.async_get_user_info(token_lid))
            add(collect_lid_user_identifiers(user_info), "Auth/UserInfo")

        account = me.get("Account")
        account = account if isinstance(account, dict) else {}
        numeric_ids = [
            value
            for value in (account.get("UserId"), account.get("Id"))
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        ]
        for numeric_id in dict.fromkeys(numeric_ids):
            user_record = await self._async_probe(self._client.async_get_user(numeric_id))
            add(collect_lid_user_identifiers(user_record), f"Users/{numeric_id}")

        today = dt_util.now().date()
        for lid, source in list(candidates.items())[:_KINDERGARTEN_MAX_CANDIDATES]:
            payload = await self._async_probe(
                self._client.async_get_kindergarten_timetable(
                    lid, today - timedelta(days=30), today + timedelta(days=60)
                )
            )
            entries = payload.get("timetableEntries")
            if not isinstance(entries, list) or not entries:
                continue
            self._kindergarten_lid = lid
            self._kindergarten_source = source
            child = await self._async_probe(self._client.async_get_kindergartener(lid))
            child_data = child.get("data")
            group_id = child_data.get("groupIdentifier") if isinstance(child_data, dict) else None
            self._kindergarten_group_id = group_id if isinstance(group_id, str) and group_id else None
            return True
        self._kindergarten_source = f"not_found ({len(candidates)} candidates)"
        return False

    async def async_fetch_timetable_week(self, week_start: date) -> Any:
        """Fetch one week's raw `Timetable` payload on demand, for
        `LibrusTimetableCalendar.async_get_events` serving a date range
        outside the current+next-week window this coordinator normally
        caches (e.g. a dashboard card asking for "this ISO week" while
        today is a Sunday, whose Monday has already rolled out of that
        window).

        BUG FIX (2026-09-06, found live): this on-demand path used to call
        `self.client.async_get_timetable(week_start)` directly, with none
        of `_async_update_data`'s forced-relogin-and-retry-once recovery
        for a mid-cycle session expiry. When the real Librus session died
        between the coordinator's own last successful poll and a
        dashboard's calendar REST request, the raw `LibrusSessionExpiredError`
        propagated straight out of `CalendarEntity.async_get_events` and
        crashed the whole `/api/calendars/<entity>` request with an
        unhandled 500 - confirmed live via `ha_config_get_calendar_events`
        returning exactly that 500 for a past-week range, traced to this
        exact exception in the error log. Same one-retry-only recovery as
        `_async_update_data`, just reusable outside the normal poll cycle.
        """
        assert self.config_entry is not None
        try:
            return await self._fetch_timetable_or_unpublished(week_start)
        except LibrusSessionExpiredError:
            await self._client.async_ensure_session_valid(
                self.config_entry.data[CONF_PASSWORD], force=True
            )
            return await self._fetch_timetable_or_unpublished(week_start)

    async def async_fetch_message(self, mailbox: str, message_id: str) -> Any:
        """Fetch one message's full body on demand, for `services.py`'s
        `get_message` service - deliberately outside the coordinator's
        normal poll cycle (see `LibrusApiClient.async_get_message`'s
        docstring for why: CONFIRMED live this marks the message read
        server-side, so it must only ever run on a user's own explicit
        action, never automatically).

        BUG FIX (live feedback, 2026-09-23, same session as the messages-
        polling fixes above): this used to only recover the MAIN Synergia
        session (`async_ensure_session_valid`) on a `LibrusSessionExpiredError`
        - correct for `async_fetch_timetable_week` (Timetable lives on that
        same main session), but `async_get_message` lives on the SEPARATE
        wiadomosci.librus.pl session instead, which this project has now
        confirmed dies independently and far more often. The retry used to
        reuse the same now-stale Wiadomości cookies and fail again, this
        time uncaught - surfacing as a real "couldn't load message" error
        to whoever just clicked a message in a card. Now also forces a
        fresh Wiadomości bootstrap before retrying, mirroring
        `_async_bootstrap_and_fetch_primary_messages`'s own recovery."""
        assert self.config_entry is not None
        try:
            return await self._client.async_get_message(mailbox, message_id)
        except LibrusSessionExpiredError:
            await self._client.async_ensure_session_valid(
                self.config_entry.data[CONF_PASSWORD], force=True
            )
            self._messages_bootstrapped = False
            self._messages_available = await self._client.async_bootstrap_messages()
            self._messages_bootstrapped = True
            return await self._client.async_get_message(mailbox, message_id)

    async def _async_update_data(self) -> LibrusData:
        assert self.config_entry is not None
        # `self.data is not None` guard: the FIRST refresh always runs for
        # real, even if it happens to land inside the quiet-hours window -
        # there's nothing to fall back to yet, and every other coordinator
        # in this codebase expects async_config_entry_first_refresh to
        # actually populate data. Subsequent cycles during the window just
        # return the last-known data unchanged - a normal, supported
        # DataUpdateCoordinator pattern (entities keep their last state,
        # nothing goes stale/unavailable) and skips the network round-trip
        # entirely, not just the parsing - see _in_quiet_hours.
        if self.data is not None and self._in_quiet_hours():
            return self.data
        force = self._force_next_fetch
        self._force_next_fetch = False
        if self.data is not None and not force and self._smart_polling_skip():
            return self.data
        if self.data is not None and not force and self._in_outage_backoff():
            return self.data
        self.last_attempt_at = dt_util.utcnow()
        self.fallback_sections = set()
        try:
            data = await self._async_fetch_live()
        except UpdateFailed as err:
            return self._handle_failed_cycle(err)
        self.last_success_at = self._last_fetch_at = dt_util.utcnow()
        self.last_error = None
        self.failures = 0
        self.next_attempt_at = None
        self.data_source = "live"
        self._state_store.async_delay_save(self._state_to_save, STATE_SAVE_DELAY)
        return data

    def _in_outage_backoff(self) -> bool:
        return self.next_attempt_at is not None and dt_util.utcnow() < self.next_attempt_at

    def _handle_failed_cycle(self, err: UpdateFailed) -> LibrusData:
        """Librus failed this cycle (not a rejected password - that raises
        ConfigEntryAuthFailed and never gets here). Keep showing the last
        good data while it is recent enough, rebuild it from the saved
        responses when there is none yet (HA started during the outage),
        and space out the next attempts after the second failure in a row
        so an outage isn't met with a login attempt every cycle."""
        now = dt_util.utcnow()
        self.failures += 1
        self.last_error = str(err)
        if self.failures >= 2 and self.update_interval is not None:
            delay = min(self.update_interval * 2 ** (self.failures - 1), OUTAGE_BACKOFF_MAX)
            self.next_attempt_at = now + delay
        recent = (
            self.last_success_at is not None
            and now - self.last_success_at < LAST_GOOD_DATA_MAX_AGE
        )
        if recent and self.data is not None:
            if self.failures == 1:
                _LOGGER.warning("Librus is not responding (%s) - showing the last data", err)
            if self.data_source == "live":
                self.data_source = "stale"
            return self.data
        if recent and self.data is None:
            data = self._build_data_from_saved_responses()
            if data is not None:
                _LOGGER.warning(
                    "Librus is not responding (%s) - starting with the data saved at %s",
                    err,
                    self.last_success_at,
                )
                self.data_source = "cache"
                return data
        raise err

    def _build_data_from_saved_responses(self) -> LibrusData | None:
        """LibrusData parsed from the last good responses (no events, no
        messages, no lucky number), or None when nothing usable is saved."""
        if "Me" not in self._last_good:
            return None
        today = dt_util.now().date()
        week_start = today - timedelta(days=today.weekday())
        core = [self._last_good.get(label, {}) for label in _CORE_PAYLOAD_LABELS]
        timetable_index = _CORE_PAYLOAD_LABELS.index("Timetable (this week)")
        core[timetable_index] = self._timetable_cache.get(week_start.isoformat(), {})
        core[timetable_index + 1] = self._timetable_cache.get(
            (week_start + timedelta(days=7)).isoformat(), {}
        )
        self._apply_reference_payloads(
            {label: self._last_good.get(label, {}) for label in REFERENCE_DATA_ENDPOINT_LABELS}
        )
        try:
            return self._build_data(
                tuple(core),
                None,
                _NO_MESSAGES,
                self._saved_point_grades(),
                parse_justifications(self._last_good.get("Justifications") or {}),
                {label: self._last_good.get(label) or {} for label in _EXTRA_LABELS},
            )
        except Exception:  # noqa: BLE001 - a bad saved file must not block setup
            _LOGGER.warning("Could not rebuild data from the saved responses", exc_info=True)
            return None

    async def _async_fetch_live(self) -> LibrusData:
        """One real fetch from Librus, with the new-item events."""
        assert self.config_entry is not None
        try:
            await self._client.async_ensure_session_valid(self.config_entry.data[CONF_PASSWORD])
        except LibrusAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except LibrusError as err:
            raise UpdateFailed(str(err)) from err

        today = dt_util.now().date()
        week_start = today - timedelta(days=today.weekday())
        next_week_start = week_start + timedelta(days=7)

        try:
            core_payloads = await self._async_fetch_core_payloads(week_start, next_week_start)
        except LibrusSessionExpiredError:
            # CONFIRMED live (2026-09-05): Librus's real session lifetime can
            # run shorter than our own conservative ASSUMED_SESSION_LIFETIME_
            # SECONDS estimate - `async_ensure_session_valid` above thought
            # the session was still fresh, but a data endpoint rejected it
            # anyway. The stored password is still there for exactly this
            # case: force one fresh login and retry ONCE before ever
            # bothering the user with Home Assistant's reauth flow - never
            # retry more than once per cycle (avoid hammering Librus).
            try:
                await self._client.async_ensure_session_valid(
                    self.config_entry.data[CONF_PASSWORD], force=True
                )
                core_payloads = await self._async_fetch_core_payloads(week_start, next_week_start)
            except LibrusAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except LibrusError as err:
                raise UpdateFailed(str(err)) from err
        except LibrusAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except LibrusError as err:
            raise UpdateFailed(str(err)) from err

        # Order: _CORE_PAYLOAD_LABELS (Me, tier 1, tier 2).
        me_payload = core_payloads[0]
        self._last_good["Me"] = me_payload
        timetable_this_week, timetable_next_week = core_payloads[6], core_payloads[7]

        if await self._async_maybe_discover_kindergarten(me_payload):
            try:
                timetable_this_week, timetable_next_week = await asyncio.gather(
                    self._fetch_timetable_or_unpublished(week_start),
                    self._fetch_timetable_or_unpublished(next_week_start),
                )
            except LibrusError as err:
                # Just found - the next cycle fetches it normally.
                _LOGGER.debug("Kindergarten timetable fetch failed right after discovery: %s", err)

        # BUG FIX (code review, 0.7.4): these three used to run sequentially,
        # one `await` after another, even though none of them reads state
        # any of the others writes. Lucky number and reference data both
        # hit the SAME main Synergia domain and are gathered together
        # below.
        #
        # BUG FIX (live feedback, 2026-09-23): messages is deliberately NOT
        # in that same gather (it originally was, briefly, same session as
        # the fix above) - `_async_refresh_reference_data` alone fires 10
        # concurrent requests; bundling the separate wiadomosci.librus.pl
        # bootstrap+fetch into that exact same burst (up to ~15 simultaneous
        # requests sharing one aiohttp session/connector) is a real, live-
        # identified suspect for why that session specifically started
        # dying far more often than observed before - not proven as the
        # sole cause (no pre-2026-09-23 diagnostics exist to compare
        # against), but cheap and safe to remove as a variable regardless.
        # Still concurrent with the OTHER two (not back to fully
        # sequential), just not sharing their exact same burst.
        lucky_number, _ = await asyncio.gather(
            self._async_get_lucky_number(today),
            self._async_refresh_reference_data(),
        )
        messages_result = await self._async_get_messages()
        point_grades = await self._async_get_point_grades()
        justifications = await self._async_get_justifications()
        extras = {
            "BaseTextGrades": await self._async_optional(
                "BaseTextGrades", self._client.async_get_base_text_grades
            ),
            "Realizations": await self._async_optional(
                "Realizations", self._client.async_get_realizations, every=_HOURLY
            ),
            "SchoolTrips": await self._async_optional(
                "SchoolTrips", self._client.async_get_school_trips, every=_HOURLY
            ),
            "SchoolFiles": await self._async_optional(
                "SchoolFiles", self._client.async_get_school_files, every=_HOURLY
            ),
        }
        core_payloads = (
            *core_payloads[:6],
            timetable_this_week,
            timetable_next_week,
            *core_payloads[8:],
        )
        data = self._build_data(
            core_payloads, lucky_number, messages_result, point_grades, justifications, extras
        )
        me, grades, notes = data.me, data.grades, data.notes
        attendances, attendance_types = data.attendances, data.attendance_types
        self._fire_change_events(self._change_tracker.update(data, today=today), data)
        self._fire_new_homework_assignment_events(data)
        self._fire_agenda_change_events(data, today)
        self._fire_justification_events(data)
        self._fire_extra_item_events(data)
        self._check_achievements(grades, attendances, attendance_types, notes, today, me.display_name)
        self._fire_forecast_events(data, today)
        return data

    def _build_data(
        self,
        core_payloads: tuple[Any, ...],
        lucky_number: LuckyNumberData | None,
        messages_result: tuple[Any, ...],
        point_grades: list[PointGradeData] | None = None,
        justifications: list[JustificationData] | None = None,
        extras: dict[str, Any] | None = None,
    ) -> LibrusData:
        """Parse one cycle's responses (in `_CORE_PAYLOAD_LABELS` order)
        together with the cached reference lookups."""
        (
            me_payload,
            grades_payload,
            categories_payload,
            notes_payload,
            attendances_payload,
            attendance_types_payload,
            timetable_this_week,
            timetable_next_week,
            homeworks_payload,
            notices_payload,
            grade_comments_payload,
            homework_assignments_payload,
            behaviour_grades_payload,
            behaviour_grade_comments_payload,
            descriptive_grades_payload,
            parent_teacher_conferences_payload,
        ) = core_payloads
        (
            unread_count,
            unread_by_mailbox,
            messages,
            substitution_messages,
            alert_messages,
            justification_messages,
        ) = messages_result

        me = parse_me(me_payload)
        grades = parse_grades(grades_payload, parse_comment_text_map(grade_comments_payload))
        school_notices = parse_school_notices(notices_payload)
        notes = parse_notes(notes_payload)
        homeworks = parse_homeworks(homeworks_payload)
        attendances = parse_attendances(attendances_payload)
        attendance_types = parse_attendance_types(attendance_types_payload)
        timetable = merge_timetables(timetable_this_week, timetable_next_week)
        grade_categories = parse_grade_categories(categories_payload)
        return LibrusData(
            me=me,
            grades=grades,
            grade_categories=grade_categories,
            notes=notes,
            attendances=attendances,
            attendance_types=attendance_types,
            timetable=timetable,
            homeworks=homeworks,
            school_notices=school_notices,
            lucky_number=lucky_number,
            subjects=self._cached_subjects,
            teachers=self._cached_teachers,
            classrooms=self._cached_classrooms,
            lesson_subjects=self._cached_lesson_subjects,
            messages_available=self._messages_available,
            unread_message_count=unread_count,
            unread_messages_by_mailbox=unread_by_mailbox,
            messages=messages,
            substitution_messages=substitution_messages,
            alert_messages=alert_messages,
            justification_messages=justification_messages,
            school=self._cached_school,
            school_class=self._cached_class,
            free_days=self._cached_free_days,
            homework_assignments=parse_homework_assignments(homework_assignments_payload),
            behaviour_grades=parse_behaviour_grades(
                behaviour_grades_payload, parse_comment_text_map(behaviour_grade_comments_payload)
            ),
            homework_categories=self._cached_homework_categories,
            note_categories=self._cached_note_categories,
            behaviour_grade_categories=self._cached_behaviour_grade_categories,
            descriptive_grades=parse_descriptive_grades(descriptive_grades_payload),
            point_grades=point_grades or [],
            justifications=justifications or [],
            text_grades=parse_text_grades(
                (extras or {}).get("BaseTextGrades") or {}, self._cached_text_grade_categories
            ),
            lesson_topics=parse_realizations(
                (extras or {}).get("Realizations") or {}, self._cached_lesson_subjects
            ),
            school_trips=parse_school_trips((extras or {}).get("SchoolTrips") or {}),
            school_files=parse_school_files((extras or {}).get("SchoolFiles") or {}),
            homework_assignment_categories=self._cached_homework_assignment_categories,
            parent_teacher_conferences=parse_parent_teacher_conferences(
                parent_teacher_conferences_payload
            ),
        )

    @property
    def weighted_average(self) -> bool:
        """The options flow's average mode (weighted unless arithmetic)."""
        if self.config_entry is None:
            return True
        mode = self.config_entry.options.get(CONF_AVERAGE_MODE, DEFAULT_AVERAGE_MODE)
        return mode != AVERAGE_MODE_ARITHMETIC

    @property
    def grade_thresholds(self) -> tuple[float, ...]:
        """The forecast's minimum averages for a 2..6 (options flow)."""
        text = self.config_entry.options.get(CONF_GRADE_THRESHOLDS) if self.config_entry else None
        return parse_thresholds(text)

    def _fire_forecast_events(self, data: LibrusData, today: date) -> None:
        """EVENT_FORECAST_CHANGED when a subject's forecast grade moves.
        Silent on the first poll and when the basis changes (the second
        semester starts), so neither looks like a jump."""
        basis, _semester = forecast_basis(data, today)
        forecasts = subject_forecasts(
            data, today, self.grade_thresholds, weighted=self.weighted_average
        )
        current = {f.subject_id: f.predicted for f in forecasts}
        known = self._known_forecast
        if known is not None and basis == self._known_forecast_basis:
            entry_id = self.config_entry.entry_id if self.config_entry else None
            for forecast in forecasts:
                old = known.get(forecast.subject_id)
                if old is None or old == forecast.predicted:
                    continue
                self.hass.bus.async_fire(
                    EVENT_FORECAST_CHANGED,
                    {
                        "entry_id": entry_id,
                        "student": data.me.display_name,
                        "subject_id": forecast.subject_id,
                        "subject": forecast.subject,
                        "old": old,
                        "new": forecast.predicted,
                        "direction": "up" if forecast.predicted > old else "down",
                        "average": forecast.average,
                        "sixes_to_next": forecast.sixes_to_next,
                        "ones_to_drop": forecast.ones_to_drop,
                    },
                )
        self._known_forecast = current
        self._known_forecast_basis = basis

    def _feature_enabled(self, key: str, default: bool) -> bool:
        """Read one of the options-flow feature toggles (see config_flow.py)
        - defaults to enabled (the pre-toggle behaviour) if the entry has
        never set it, or if called before a config_entry is attached."""
        if self.config_entry is None:
            return default
        return bool(self.config_entry.options.get(key, default))

    async def async_force_refresh(self) -> None:
        """A refresh that skips the smart-polling throttle (the manual
        refresh button/service). Quiet hours still apply."""
        self._force_next_fetch = True
        await self.async_request_refresh()

    def smart_polling_interval(self, now: datetime | None = None) -> timedelta | None:
        """How fresh data has to be right now with smart polling on: None
        means every cycle (school day, 06:00-22:00), otherwise the minimum
        gap between fetches (a day without lessons, or the night)."""
        # Imported here: school_day -> ai_summary -> coordinator.
        from .school_day import school_days  # noqa: PLC0415

        now = dt_util.as_local(now or dt_util.now())
        if now.hour >= SMART_POLLING_NIGHT_START or now.hour < SMART_POLLING_NIGHT_END:
            return timedelta(minutes=SMART_POLLING_NIGHT)
        if now.date() not in school_days(self.data):
            return timedelta(minutes=SMART_POLLING_DAY_OFF)
        return None

    def _smart_polling_skip(self) -> bool:
        if not self._feature_enabled(CONF_SMART_POLLING, DEFAULT_SMART_POLLING):
            return False
        gap = self.smart_polling_interval()
        if gap is None or self._last_fetch_at is None:
            return False
        return dt_util.utcnow() - self._last_fetch_at < gap

    def _in_quiet_hours(self) -> bool:
        """Whether `dt_util.now()` currently falls inside the configured
        quiet-hours window (off by default - see CONF_QUIET_HOURS_ENABLED).
        Handles a window that wraps midnight (e.g. 23:00 -> 06:00, the
        default) the same way any "overnight range" check has to: it's
        NOT simply start <= now <= end once start > end."""
        if not self._feature_enabled(CONF_QUIET_HOURS_ENABLED, DEFAULT_QUIET_HOURS_ENABLED):
            return False
        start = self._option_time(CONF_QUIET_HOURS_START, DEFAULT_QUIET_HOURS_START)
        end = self._option_time(CONF_QUIET_HOURS_END, DEFAULT_QUIET_HOURS_END)
        now = dt_util.now().time()
        if start <= end:
            return start <= now < end
        return now >= start or now < end

    def _option_time(self, key: str, default: str) -> time:
        raw = self.config_entry.options.get(key, default) if self.config_entry else default
        return dt_util.parse_time(raw) or dt_util.parse_time(default)

    async def _maybe(self, enabled: bool, factory: Any) -> Any:
        """Skip a network call entirely when a feature is toggled off in the
        options flow, returning `{}` instead - the same shape every parser
        in this module already treats identically to a genuinely empty
        account, so no extra special-casing was needed downstream to wire
        these toggles up. `factory` is the client's bound method itself
        (not yet called), so a disabled feature never even builds the
        coroutine for its real network call."""
        if not enabled:
            return {}
        return await factory()

    async def _async_fetch_core_payloads(
        self, week_start: date, next_week_start: date
    ) -> tuple[Any, ...]:
        """The core (non-optional) data fetch - grades/attendance/timetable/
        agenda/announcements. Factored out of `_async_update_data` so it can
        be retried once, unmodified, after a forced re-login (see there).

        Split into two tiers, on purpose - previously all 16 endpoints were
        in ONE asyncio.gather(), so a single failure on any of them (e.g. a
        newer, less-exercised endpoint like DescriptiveGrades hiccuping)
        raised and discarded every OTHER endpoint's already-successful
        result too, wiping grades/attendance/timetable for the whole cycle
        over one unrelated endpoint. TIER 1 below is the original,
        genuinely load-bearing sensors; TIER 2 is the newer supplementary
        endpoints. Both now use `return_exceptions=True` and degrade a
        CONFIRMED 403 to empty (see `_degrade_core_payload`/
        `_degrade_optional_payload`) - a genuine 401 anywhere still
        propagates and still drives the forced-relogin-and-retry-once
        logic in `_async_update_data`, unchanged.

        `Me` is deliberately fetched separately, BEFORE either tier, and
        stays fully fatal on any failure (401 or 403) - see
        CORE_ENDPOINT_LABELS' own comment for why.

        BUG FIX (issue #5, reported live): TIER 1 used to be one plain
        `asyncio.gather()` with no `return_exceptions=True` at all - a
        CONFIRMED 403 on `Attendances/Types` (a preschool-account login
        that only has the Wiadomości module enabled) took down the whole
        setup, even though it just meant "this account doesn't have the
        attendance module", the exact same class of thing `Timetables`
        already got this treatment for once (issue #4). Generalized to
        the whole tier now, not just Timetables, since the next limited-
        access account type would otherwise just report the same bug
        again with a different endpoint name.

        Announcements/BehaviourGrades/DescriptiveGrades are additionally
        gated by their own options-flow toggle via `_maybe` - disabled ones
        never hit the network at all, in either tier."""
        announcements_enabled = self._feature_enabled(
            CONF_ANNOUNCEMENTS_ENABLED, DEFAULT_ANNOUNCEMENTS_ENABLED
        )
        behaviour_grades_enabled = self._feature_enabled(
            CONF_BEHAVIOUR_GRADES_ENABLED, DEFAULT_BEHAVIOUR_GRADES_ENABLED
        )
        descriptive_grades_enabled = self._feature_enabled(
            CONF_DESCRIPTIVE_GRADES_ENABLED, DEFAULT_DESCRIPTIVE_GRADES_ENABLED
        )

        me_payload = await self._client.async_get_me()

        core_results = await asyncio.gather(
            self._client.async_get_grades(),
            self._client.async_get_grade_categories(),
            self._client.async_get_notes(),
            self._client.async_get_attendances(),
            self._client.async_get_attendance_types(),
            self._fetch_timetable_or_unpublished(week_start),
            self._fetch_timetable_or_unpublished(next_week_start),
            self._client.async_get_homeworks(),
            self._maybe(announcements_enabled, self._client.async_get_school_notices),
            return_exceptions=True,
        )
        # A genuine 401 anywhere in this tier means the session actually
        # died - propagate it immediately (before degrading any 403s)
        # so the existing forced-relogin-and-retry-once recovery still
        # runs exactly as before. A dead session can plausibly 403
        # unrelated endpoints too in the same broken cycle, so it's not
        # safe to interpret THOSE as "confirmed module-unavailable" once
        # a real 401 has shown up anywhere in the same batch.
        for result in core_results:
            if isinstance(result, LibrusSessionExpiredError) and result.status_code == 401:
                raise result
        core = (
            me_payload,
            *(
                self._degrade_core_payload(label, result)
                for label, result in zip(CORE_ENDPOINT_LABELS, core_results)
            ),
        )

        optional_results = await asyncio.gather(
            self._client.async_get_grade_comments(),
            self._client.async_get_homework_assignments(),
            self._maybe(behaviour_grades_enabled, self._client.async_get_behaviour_grade_points),
            self._maybe(
                behaviour_grades_enabled, self._client.async_get_behaviour_grade_point_comments
            ),
            self._maybe(descriptive_grades_enabled, self._client.async_get_descriptive_grades),
            self._client.async_get_parent_teacher_conferences(),
            return_exceptions=True,
        )
        optional_payloads = [
            self._degrade_optional_payload(label, result)
            for label, result in zip(OPTIONAL_ENDPOINT_LABELS, optional_results)
        ]

        return (*core, *optional_payloads)

    def _degrade_optional_payload(
        self, label: str, result: dict[str, Any] | BaseException
    ) -> dict[str, Any]:
        """Turn one `return_exceptions=True` gather result into a payload,
        degrading a LibrusError to an empty dict (so its parser sees the
        same shape as a genuinely empty account) instead of letting it take
        down the rest of the core-data fetch. Anything that ISN'T a
        LibrusError (a real bug, or asyncio.CancelledError) is re-raised -
        only confirmed API-level failures are safe to swallow here.

        Also feeds the optional-endpoint-degraded repair issue tracking -
        a `_maybe()`-skipped (disabled-in-options) endpoint arrives here as
        a plain `{}`, which counts as a "success" for that tracking (it
        clears any previously-raised issue for it - turning a feature off
        isn't a failure worth flagging)."""
        if isinstance(result, BaseException):
            if isinstance(result, LibrusError):
                _LOGGER.debug(
                    "Optional endpoint '%s' fetch failed (non-fatal): %s",
                    label,
                    result,
                    exc_info=result,
                )
                self._note_optional_endpoint_failure(label)
                return self._fallback(label)
            raise result
        self._note_optional_endpoint_recovery(label)
        self._last_good[label] = result
        return result

    def _fallback(self, label: str) -> dict[str, Any]:
        """The last good response of a failed section (`{}` if none)."""
        if label not in self._last_good:
            return {}
        self.fallback_sections.add(label)
        return self._last_good[label]

    def _degrade_core_payload(
        self, label: str, result: dict[str, Any] | BaseException
    ) -> dict[str, Any]:
        """Turn one TIER-1 `return_exceptions=True` gather result into a
        payload, degrading a CONFIRMED 403 to an empty dict - "this
        account/school type doesn't have this module" (issue #5, see
        CORE_ENDPOINT_LABELS' own comment for the full story). Every
        parser fed from TIER 1 already treats an empty payload the same
        as a genuinely empty account, so this is the same degrade path
        `_degrade_optional_payload` uses for TIER 2, just narrower:

        Unlike TIER 2 (genuinely optional endpoints, where ANY LibrusError
        is safe to swallow), TIER 1 is the load-bearing tier - only a
        CONFIRMED 403 (`LibrusSessionExpiredError` specifically, not any
        LibrusError) is treated as "module unavailable". A 401 never
        reaches here at all (the caller re-raises any 401 across the
        whole tier before calling this, see `_async_fetch_core_payloads`).
        A connection error, an unexpected-shape response, or any other
        LibrusError subtype still fails the whole cycle - those aren't a
        confirmed "this module doesn't exist for this account" signal,
        just a transient or genuinely-wrong-shaped failure that's worth
        surfacing (and retrying next cycle) rather than silently hiding.

        Shares the same repair-issue tracking as `_degrade_optional_payload`
        (`_note_optional_endpoint_failure`/`_note_optional_endpoint_recovery`)
        - a persistently-403ing core endpoint is just as worth a "hasn't
        responded in over a week" repair issue as a supplementary one."""
        if isinstance(result, BaseException):
            if isinstance(result, LibrusSessionExpiredError) and result.status_code == 403:
                _LOGGER.debug(
                    "Core endpoint '%s' returned a confirmed 403 (module "
                    "likely unavailable for this account/school) - "
                    "degrading to empty instead of failing the whole cycle: %s",
                    label,
                    result,
                )
                self._note_optional_endpoint_failure(label)
                self._last_good[label] = {}
                return {}
            if (
                isinstance(result, LibrusError)
                and not isinstance(result, LibrusSessionExpiredError)
                and label in self._last_good
            ):
                # A transient failure (timeout, 5xx, garbled response) of
                # one section: keep its last good response rather than
                # failing the whole cycle over it.
                _LOGGER.debug("Core endpoint '%s' failed - using the saved copy: %s", label, result)
                self._note_optional_endpoint_failure(label)
                return self._fallback(label)
            raise result
        self._note_optional_endpoint_recovery(label)
        self._last_good[label] = result
        return result

    # How long a supplementary endpoint must fail on EVERY attempt before a
    # repair issue is raised for it - deliberately generous. Most of these
    # endpoints are "confirmed real, empty" for entire school years at a
    # time (see this project's own README/BACKLOG), so a short window would
    # constantly flag perfectly normal accounts; a week of unbroken
    # failures is a much stronger signal that something is actually wrong
    # (a permission change, an endpoint Librus removed, etc.) rather than
    # this account simply never having that kind of data.
    _OPTIONAL_ENDPOINT_DEGRADED_AFTER = timedelta(days=7)

    def _note_optional_endpoint_failure(self, label: str) -> None:
        if self.config_entry is None:
            return
        now = dt_util.utcnow()
        first_failed = self._optional_endpoint_first_failure.setdefault(label, now)
        if now - first_failed < self._OPTIONAL_ENDPOINT_DEGRADED_AFTER:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            optional_endpoint_issue_id(self.config_entry.entry_id, label),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_OPTIONAL_ENDPOINT_DEGRADED,
            translation_placeholders={"label": label, "since": first_failed.date().isoformat()},
        )

    def _note_optional_endpoint_recovery(self, label: str) -> None:
        """Always attempt the delete (a no-op if nothing was raised) rather
        than gating it on THIS coordinator instance's own in-memory
        failure tracking - the in-memory dict resets on every reload
        (options change, HA restart, ...), so a coordinator that comes back
        up already healthy would otherwise never clear an issue a PREVIOUS
        instance raised before that reload."""
        self._optional_endpoint_first_failure.pop(label, None)
        if self.config_entry is not None:
            ir.async_delete_issue(
                self.hass, DOMAIN, optional_endpoint_issue_id(self.config_entry.entry_id, label)
            )

    async def _async_get_lucky_number(self, today: date) -> LuckyNumberData | None:
        now = dt_util.now()
        if (
            self._cached_lucky_number is not None
            and self._lucky_number_fetched_date == today
            and now.hour < LUCKY_NUMBER_PUBLISH_HOUR
        ):
            return self._cached_lucky_number
        try:
            payload = await self._client.async_get_lucky_number()
        except LibrusError:
            _LOGGER.debug("Lucky number fetch failed (non-fatal)", exc_info=True)
            self._note_optional_endpoint_failure("LuckyNumbers")
            return self._cached_lucky_number
        self._note_optional_endpoint_recovery("LuckyNumbers")
        lucky = parse_lucky_number(payload)
        if lucky is not None:
            self._cached_lucky_number = lucky
            self._lucky_number_fetched_date = today
        return self._cached_lucky_number

    def _degrade_reference_result(
        self, label: str, result: dict[str, Any] | BaseException
    ) -> dict[str, Any]:
        """Turn one `return_exceptions=True` reference-data gather result
        into a payload, degrading a LibrusError to `{}` (which every
        parser below already treats the same as a genuinely empty account)
        instead of letting one failing endpoint wipe out the other nine's
        already-successful results too - the exact same all-or-nothing
        gather bug `_degrade_optional_payload` fixed for the core-data
        fetch's own supplementary tier (code review), applied here to
        reference data instead. Anything that ISN'T a LibrusError (a real
        bug, or asyncio.CancelledError) is re-raised, same as there.

        Also feeds the SAME `degraded_endpoints`/repair-issue tracking as
        `_degrade_optional_payload`/`_degrade_core_payload` - see
        `REFERENCE_DATA_ENDPOINT_LABELS`' own comment (const.py) for why
        this wasn't wired in originally and why that was a real gap."""
        if isinstance(result, BaseException):
            if isinstance(result, LibrusError):
                _LOGGER.debug(
                    "Reference-data endpoint '%s' fetch failed (non-fatal): %s",
                    label,
                    result,
                    exc_info=result,
                )
                self._note_optional_endpoint_failure(label)
                # The last good copy - an empty dict here wiped e.g. every
                # subject name for a whole day (reference data is refetched
                # only every 24h).
                return self._fallback(label)
            raise result
        self._note_optional_endpoint_recovery(label)
        self._last_good[label] = result
        return result

    async def _async_refresh_reference_data(self) -> None:
        """Refresh near-static reference data at most once a day: subject/
        teacher/classroom name lookups, school/class identity, homework
        agenda categories, note categories, behaviour-grade categories, and
        the free-days calendar.

        The Subjects/Teachers/Classrooms endpoint names were UNVERIFIED when
        first written but are now CONFIRMED live, same as everything else
        fetched here (2026-09-05) - a failure is still treated as non-fatal
        for all of it, since none of this is core data (grades/attendance/
        timetable keep working without it; entities just fall back to a raw
        numeric id, or a missing school/class sensor/calendar).

        BUG FIX (code review): this used to be ONE plain `asyncio.gather()`
        (no `return_exceptions=True`) wrapped in a single try/except - one
        of these 10 endpoints failing raised and discarded the other nine's
        already-successful results too, and the whole refresh was skipped
        for this cycle (retried again next cycle, hammering all 10 every
        time until they all happen to succeed together). None of this is
        core data (same "supplementary" classification `_async_fetch_core_
        payloads`' own TIER 2 already uses), so it's now fetched with
        `return_exceptions=True` and each result degraded independently via
        `_degrade_reference_result` - one endpoint failing only empties
        THAT ONE cache for this cycle, never blocks the other nine, and
        `_reference_data_fetched_at` still advances (this is deliberately a
        24h-cached "confirmed real, empty" degrade, not a per-cycle retry -
        a permanently-broken endpoint no longer gets hammered every single
        coordinator cycle forever).
        """
        now = dt_util.utcnow()
        if (
            self._reference_data_fetched_at is not None
            and now - self._reference_data_fetched_at < timedelta(hours=24)
        ):
            return
        free_days_enabled = self._feature_enabled(CONF_FREE_DAYS_ENABLED, DEFAULT_FREE_DAYS_ENABLED)
        behaviour_grades_enabled = self._feature_enabled(
            CONF_BEHAVIOUR_GRADES_ENABLED, DEFAULT_BEHAVIOUR_GRADES_ENABLED
        )
        results = await asyncio.gather(
            self._client.async_get_subjects(),
            self._client.async_get_teachers(),
            self._client.async_get_classrooms(),
            self._client.async_get_schools(),
            self._client.async_get_classes(),
            self._client.async_get_homework_categories(),
            self._maybe(free_days_enabled, self._client.async_get_school_free_days),
            self._maybe(free_days_enabled, self._client.async_get_class_free_days),
            self._client.async_get_note_categories(),
            self._maybe(
                behaviour_grades_enabled, self._client.async_get_behaviour_grade_point_categories
            ),
            self._client.async_get_lessons(),
            self._client.async_get_text_grade_categories(),
            self._client.async_get_homework_assignment_categories(),
            self._client.async_get_units(),
            return_exceptions=True,
        )
        payloads = {
            label: self._degrade_reference_result(label, result)
            for label, result in zip(REFERENCE_DATA_ENDPOINT_LABELS, results)
        }
        self._apply_reference_payloads(payloads)
        if self._kindergarten_lid is not None:
            await self._async_refresh_kindergarten_reference_data(payloads["Teachers"])
        self._check_school_year_rollover()
        await self._async_refresh_student_number()
        self._reference_data_fetched_at = now

    def _apply_reference_payloads(self, payloads: dict[str, Any]) -> None:
        """Parse the reference-data responses (keyed by
        REFERENCE_DATA_ENDPOINT_LABELS) into the name lookups. Also used to
        rebuild them from the saved responses when Librus is down at start.
        A disabled toggle's payload is `{}` (via `_maybe`), which every
        parser treats the same as a genuinely empty account."""

        def payload(label: str) -> dict[str, Any]:
            return payloads.get(label) or {}

        self._cached_subjects = parse_id_name_map(payload("Subjects"), ("Subjects",))
        self._cached_teachers = parse_id_name_map(payload("Teachers"), ("Users", "Teachers"))
        self._cached_classrooms = parse_id_name_map(payload("Classrooms"), ("Classrooms",))
        self._cached_lesson_subjects = parse_lesson_subjects(payload("Lessons"))
        self._cached_school = parse_school(payload("Schools"))
        self._cached_class = parse_class(payload("Classes"))
        self._cached_homework_categories = parse_id_name_map(
            payload("HomeworkCategories"), ("Categories",)
        )
        self._cached_free_days = parse_free_days(
            payload("SchoolFreeDays"), "SchoolFreeDays"
        ) + parse_free_days(payload("ClassFreeDays"), "ClassFreeDays")
        self._cached_note_categories = parse_id_name_map(
            payload("NoteCategories"), ("Categories",)
        )
        self._cached_behaviour_grade_categories = parse_id_name_map(
            payload("BehaviourGradeCategories"), ("Categories",)
        )
        if "Units" in payloads:
            self.point_grades_enabled = point_grades_enabled(payload("Units"))
        self._cached_text_grade_categories = parse_text_grade_categories(
            payload("TextGradeCategories")
        )
        self._cached_homework_assignment_categories = parse_id_name_map(
            payload("HomeworkAssignmentCategories"), ("Categories",)
        )

    async def _async_get_point_grades(self) -> list[PointGradeData]:
        """Point grades with their categories (maximum, weight). Skipped
        when Units says the school doesn't grade in points; a failure keeps
        the last good copy, like any optional endpoint."""
        if self.point_grades_enabled is False:
            return []
        results = await asyncio.gather(
            self._client.async_get_point_grades(),
            self._client.async_get_point_grade_categories(),
            return_exceptions=True,
        )
        grades_payload, categories_payload = (
            self._degrade_optional_payload(label, result)
            for label, result in zip(_POINT_GRADE_LABELS, results)
        )
        return parse_point_grades(grades_payload, parse_point_grade_categories(categories_payload))

    async def _async_optional(
        self, label: str, factory: Any, *, every: timedelta | None = None
    ) -> dict[str, Any]:
        """One optional endpoint's payload: the last good copy on failure,
        and with `every`, the cached copy until it's that old (endpoints
        that change a few times a day, not every cycle)."""
        fetched = self._fetched_at.get(label)
        if (
            every is not None
            and fetched is not None
            and label in self._last_good
            and dt_util.utcnow() - fetched < every
        ):
            return self._last_good[label]
        try:
            result: dict[str, Any] | BaseException = await factory()
        except LibrusError as err:
            result = err
        payload = self._degrade_optional_payload(label, result)
        if not isinstance(result, BaseException):
            self._fetched_at[label] = dt_util.utcnow()
        return payload

    async def async_download_attachment(self, attachment_id: str, message_id: str) -> Any:
        """Download one message attachment for the attachment view
        (attachment_view.py), with the same one-retry Wiadomości recovery as
        `async_fetch_message`. Doesn't open (mark read) the message."""
        assert self.config_entry is not None
        try:
            return await self._client.async_download_message_attachment(attachment_id, message_id)
        except LibrusSessionExpiredError:
            await self._client.async_ensure_session_valid(
                self.config_entry.data[CONF_PASSWORD], force=True
            )
            self._messages_bootstrapped = False
            self._messages_available = await self._client.async_bootstrap_messages()
            self._messages_bootstrapped = True
            return await self._client.async_download_message_attachment(attachment_id, message_id)

    async def _async_get_justifications(self) -> list[JustificationData]:
        """The parent's submitted absence justifications. A failure keeps
        the last good copy, like any optional endpoint."""
        try:
            result: dict[str, Any] | BaseException = await self._client.async_get_justifications()
        except LibrusError as err:
            result = err
        return parse_justifications(self._degrade_optional_payload("Justifications", result))

    def _saved_point_grades(self) -> list[PointGradeData]:
        grades_label, categories_label = _POINT_GRADE_LABELS
        return parse_point_grades(
            self._last_good.get(grades_label) or {},
            parse_point_grade_categories(self._last_good.get(categories_label) or {}),
        )

    async def _async_refresh_student_number(self) -> None:
        """Read the class register number: `Users/{Me.Account.UserId}
        .ClassRegisterNumber` in JSON, falling back to Synergia's informacja
        web page. A failure keeps the last known number and is tracked like
        any other optional endpoint."""
        if self._kindergarten_lid is not None:
            return
        # The student's own Users record carries ClassRegisterNumber in JSON
        # (confirmed live 2026-10-07); the web page is the fallback.
        user_id = ((self._last_good.get("Me") or {}).get("Me") or {}).get("Account", {}).get("UserId")
        if user_id:
            try:
                number = parse_user_class_register_number(await self._client.async_get_user(user_id))
            except LibrusError as err:
                _LOGGER.debug("Student Users record fetch failed (non-fatal): %s", err)
                number = None
            if number is not None:
                self.student_number_from_librus = number
                self._note_optional_endpoint_recovery(STUDENT_INFO_LABEL)
                return
        try:
            page = await self._client.async_get_student_info_page()
        except LibrusError as err:
            _LOGGER.debug("Student info page fetch failed (non-fatal): %s", err)
            self._note_optional_endpoint_failure(STUDENT_INFO_LABEL)
            return
        self._note_optional_endpoint_recovery(STUDENT_INFO_LABEL)
        number = parse_student_number(page)
        if number is not None:
            self.student_number_from_librus = number

    async def _async_refresh_kindergarten_reference_data(
        self, teachers_payload: dict[str, Any]
    ) -> None:
        """Merge kindergarten lookups into the ordinary ones (PR #8's
        findings from Synergia's web UI): activity types act as subjects,
        `Auth/Classrooms` identifiers map to room symbols, teachers match
        `Users[].AccountId` (the LIDs in `timetableEntries[].teachers`), and
        the child's group stands in for the class."""
        group_id = self._kindergarten_group_id
        results = await asyncio.gather(
            self._client.async_get_kindergarten_activity_types(),
            self._client.async_get_kindergarten_classrooms(),
            self._maybe(
                group_id is not None,
                lambda: self._client.async_get_kindergarten_group(group_id),
            ),
            return_exceptions=True,
        )
        activity_payload, classrooms_payload, group_payload = (
            self._degrade_reference_result(label, result)
            for label, result in zip(
                ("Kindergarten/ActivityTypes", "Kindergarten/Classrooms", "Kindergarten/Group"),
                results,
            )
        )
        self._cached_subjects.update(parse_kindergarten_activity_types(activity_payload))
        self._cached_classrooms.update(parse_kindergarten_classrooms(classrooms_payload))
        self._cached_teachers.update(parse_kindergarten_teachers(teachers_payload))
        group = parse_kindergarten_group(group_payload)
        if group is not None:
            self._cached_class = group

    # How long past the cached Class record's own `end_school_year` date
    # before flagging it as possibly stale - generous on purpose. The
    # coordinator already re-fetches `Classes` every 24h (see above), so
    # this normally self-heals well within a day of the real school year
    # rolling over; this only fires if Librus itself hasn't published a new
    # Class record in over a month, which is worth a nudge to reload rather
    # than silently showing a school year that ended a month ago forever.
    _SCHOOL_YEAR_ROLLOVER_GRACE = timedelta(days=30)

    def _check_school_year_rollover(self) -> None:
        """Raise (or clear) the "school year rollover" repair issue based on
        whether the cached `ClassData.end_school_year` is well in the past.
        Called every time `_cached_class` is freshly refetched (i.e. at
        most once a day) - see `_async_refresh_reference_data`."""
        if self.config_entry is None:
            return
        issue_id = school_year_issue_id(self.config_entry.entry_id)
        end_school_year = self._cached_class.end_school_year if self._cached_class else None
        end_date: date | None = None
        if end_school_year:
            try:
                end_date = date.fromisoformat(end_school_year[:10])
            except ValueError:
                end_date = None
        if end_date is not None and dt_util.now().date() - end_date >= self._SCHOOL_YEAR_ROLLOVER_GRACE:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_SCHOOL_YEAR_ROLLOVER,
                translation_placeholders={"end_date": end_school_year},
                data={"entry_id": self.config_entry.entry_id},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    async def _async_bootstrap_and_fetch_primary_messages(
        self,
    ) -> tuple[int, dict[str, int], list[MessageData]]:
        """Ensure the Wiadomości session is bootstrapped (respecting
        `_messages_bootstrapped` - skipped if already done this login), then
        fetch the primary inbox/unread-count data. Raises `LibrusError` on
        ANY genuine failure (bootstrap or fetch) so the caller can retry;
        does NOT set `_messages_bootstrapped = False` itself on failure -
        that's the caller's call to make (it needs to happen exactly once
        across a normal-attempt-then-retry pair, not once per attempt).

        A clean `async_bootstrap_messages() == False` (module not enabled
        for this school) is NOT an error - sets `_messages_available =
        False` and returns an empty result instead of raising, same as
        always."""
        if not self._messages_bootstrapped:
            self._messages_available = await self._client.async_bootstrap_messages()
            self._messages_bootstrapped = True
        if not self._messages_available:
            return 0, {}, []
        unread_payload, inbox_payload = await asyncio.gather(
            self._client.async_get_unread_messages_count(),
            self._client.async_get_messages(limit=10),
        )
        return parse_messages(unread_payload, inbox_payload)

    async def _async_get_messages(
        self,
    ) -> tuple[
        int,
        dict[str, int],
        list[MessageData],
        list[MessageData],
        list[MessageData],
        list[MessageData],
    ]:
        """Fetch unread counts (per mailbox) + a recent-messages preview
        from the separate Wiadomości subsystem - inbox (full, as ever),
        plus full CONTENT (not just counts) for "substitutions", "alerts"
        and "justifications", the secondary mailboxes most worth actually
        reading rather than just knowing a count for. "justifications" was
        added 2026-09-06 (user request: "usprawiedliwienia") on the
        strength of the SAME architecture already confirmed for
        substitutions/alerts - all of these are sibling keys in one
        unread-count response, and share the identical
        `{mailbox}/messages` list endpoint, so this is a low-risk extension
        of a pattern already proven, not a new guess. UNVERIFIED: whether a
        submitted justification's accept/reject status is actually visible
        in this mailbox's message content, or only the school's own
        response text - first real submission will confirm.

        Bootstraps the dedicated session cookie once per login (not every
        cycle). Some schools don't have this Librus module enabled at all -
        that's a normal, non-fatal outcome (`async_bootstrap_messages`
        returns False, checked via the "Brak dostępu" marker), not an error.
        Any other failure here is also non-fatal - messages are a bonus
        feature, not core data, and must never fail the whole update cycle.

        BUG FIX (live feedback, 2026-09-23 - "też tak kurwa mam", reproduced
        on the maintainer's own account too): `_messages_bootstrapped` used
        to only ever get set `True`, never back to `False` - so once EITHER
        the bootstrap call OR the primary fetch below raised a real
        `LibrusError`, messages stayed silently empty FOREVER, confirmed
        live via a real account's own `last_reported` advancing on schedule
        while `last_updated` stayed frozen on the stale empty result. Only
        a full HA restart/reload cleared it. Fixed by resetting the flag on
        failure so the NEXT cycle gets a fresh bootstrap attempt.

        BUG FIX #2 (same day, same live account, confirmed by direct
        repeated observation): fixing #1 alone still left a real, visible
        gap - the dedicated wiadomosci.librus.pl session turned out to die
        far more often than expected (repeatedly, well within an hour, on
        a real account - unlike the main Synergia session's own ~20h
        lifetime), so "retry next cycle" meant the sensor could sit empty
        for however long the poll interval is. The main session has had an
        IMMEDIATE same-cycle retry for this exact class of problem since
        v0.4.2 (forced relogin + retry once, before ever surfacing a gap to
        the user) - messages never got the equivalent. `_async_bootstrap_
        and_fetch_primary` below is now called up to twice in a row: once
        normally, once more immediately if that raised, mirroring the main
        session's own proven pattern instead of waiting out a whole poll
        cycle.
        """
        if self.config_entry is not None and not self.config_entry.options.get(
            CONF_MESSAGES_ENABLED, DEFAULT_MESSAGES_ENABLED
        ):
            # Turned off in the options flow - don't bootstrap the separate
            # wiadomosci.librus.pl session or make any messages calls.
            self._messages_available = False
            return 0, {}, [], [], [], []

        try:
            unread_count, unread_by_mailbox, inbox_messages = (
                await self._async_bootstrap_and_fetch_primary_messages()
            )
        except LibrusError:
            _LOGGER.debug(
                "Messages primary fetch failed - retrying immediately with a "
                "fresh bootstrap before giving up for this cycle",
                exc_info=True,
            )
            # Same "force a fresh attempt, retry once, never more than once
            # per cycle" shape as _async_update_data's own recovery for the
            # main session - the dedicated wiadomosci session may have died
            # independently of it.
            self._messages_bootstrapped = False
            try:
                unread_count, unread_by_mailbox, inbox_messages = (
                    await self._async_bootstrap_and_fetch_primary_messages()
                )
            except LibrusError:
                _LOGGER.debug("Messages primary fetch failed again after retry (non-fatal)", exc_info=True)
                self._note_optional_endpoint_failure("Messages")
                # Still leave a fresh bootstrap scheduled for NEXT cycle too,
                # in case this keeps failing beyond just one retry.
                self._messages_bootstrapped = False
                return 0, {}, [], [], [], []
        if not self._messages_available:
            return 0, {}, [], [], [], []
        self._note_optional_endpoint_recovery("Messages")

        # BUG FIX (2026-09-06, found live): substitutions/alerts used to be
        # fetched in the SAME asyncio.gather() as the two calls above -
        # asyncio.gather() fails as a whole the moment ANY one of its
        # awaitables raises, so a failure fetching these two bonus
        # mailboxes was silently wiping out the otherwise-working
        # inbox/unread-count data too (confirmed live: mailbox_breakdown
        # went from real per-mailbox counts to an empty {} the moment this
        # was added in v0.4.13). Isolated into its own try/except so it can
        # only ever degrade to "no substitutions/alerts/justifications
        # shown", never take the core inbox data down with it.
        substitution_messages: list[MessageData] = []
        alert_messages: list[MessageData] = []
        justification_messages: list[MessageData] = []
        try:
            substitutions_payload, alerts_payload, justifications_payload = await asyncio.gather(
                self._client.async_get_messages(mailbox="substitutions", limit=10),
                self._client.async_get_messages(mailbox="alerts", limit=10),
                self._client.async_get_messages(mailbox="justifications", limit=10),
            )
            substitution_messages = parse_message_list(substitutions_payload, "substitutions")
            alert_messages = parse_message_list(alerts_payload, "alerts")
            justification_messages = parse_message_list(justifications_payload, "justifications")
            self._note_optional_endpoint_recovery("Messages/Secondary")
        except LibrusError:
            _LOGGER.debug(
                "Secondary mailbox (substitutions/alerts/justifications) fetch failed (non-fatal)",
                exc_info=True,
            )
            self._note_optional_endpoint_failure("Messages/Secondary")

        return (
            unread_count,
            unread_by_mailbox,
            inbox_messages,
            substitution_messages,
            alert_messages,
            justification_messages,
        )

    def _fire_change_events(self, changes: Changes, data: LibrusData) -> None:
        """Fire one bus event per new item the tracker reported.

        Resolved names are included alongside the raw ids so an automation
        (e.g. a notification blueprint) can use {{ trigger.event.data.
        subject }} directly, without its own lookup. `student` (the child's
        name, not the login/parent's - see MeData) is on every event so a
        multi-child household's blueprint can say WHOSE grade/note/etc. this
        is, since one blueprint instance's action runs for every config
        entry that fires the event."""
        base = {
            "entry_id": self.config_entry.entry_id if self.config_entry else None,
            "student": data.me.display_name,
        }

        def fire(event: str, item_id: Any, payload: dict[str, Any]) -> None:
            self.hass.bus.async_fire(event, {**base, "id": item_id, **payload})

        def subject_name(subject_id: int | str | None) -> str | None:
            if subject_id is None:
                return None
            return data.subjects.get(subject_id, str(subject_id))

        improves, _ = grade_improvements(data.grades)
        for grade in changes.grades:
            fire(
                EVENT_NEW_GRADE,
                grade.id,
                {
                    "subject_id": grade.subject_id,
                    "subject": subject_name(grade.subject_id),
                    "value": grade.value,
                    "teacher": _teacher_name(data, grade.teacher_id),
                    **_grade_event_details(grade, data.grade_categories, improves),
                },
            )
        for notice in changes.announcements:
            fire(EVENT_NEW_ANNOUNCEMENT, notice.id, {"subject": notice.subject})
        for note in changes.notes:
            fire(
                EVENT_NEW_NOTE,
                note.id,
                {
                    "positive": note.positive,
                    "sentiment": note.sentiment,
                    "teacher": data.teachers.get(note.teacher_id, str(note.teacher_id))
                    if note.teacher_id is not None
                    else None,
                    "text": note.text,
                },
            )
        for message in changes.messages:
            fire(
                EVENT_NEW_MESSAGE,
                message.id,
                {"sender": message.sender_name, "topic": message.topic},
            )
        for homework in changes.agenda:
            fire(
                EVENT_NEW_HOMEWORK,
                homework.id,
                {
                    "subject_id": homework.subject_id,
                    "subject": subject_name(homework.subject_id),
                    "category": data.homework_categories.get(homework.category_id)
                    if homework.category_id is not None
                    else None,
                    "date": homework.date,
                    "content": (homework.content or "")[:200],
                },
            )
        # Real absences only (any non-presence type); `excused` says which.
        for absence in changes.absences:
            absence_type = data.attendance_types[absence.type_id]
            fire(
                EVENT_NEW_ABSENCE,
                absence.id,
                {
                    "date": absence.date,
                    "type": absence_type.name,
                    "excused": absence_type.is_excused_absence,
                    "lesson_no": absence.lesson_no,
                },
            )
        # A lesson on today or a later date that newly turned up cancelled
        # or as a substitution. The id is the tracker's synthetic
        # date|period|kind|subject signature.
        for change in changes.timetable_changes:
            lesson = change.lesson
            day = change.date.isoformat()
            fire(
                EVENT_TIMETABLE_CHANGED,
                f"{day}|{lesson.lesson_no}|{change.kind}|{lesson.subject_id}",
                {
                    "date": day,
                    "lesson_no": lesson.lesson_no,
                    "kind": change.kind,
                    "subject_id": lesson.subject_id,
                    "subject": subject_name(lesson.subject_id),
                    "hour_from": lesson.hour_from,
                    "teacher": _teacher_name(data, lesson.teacher_id),
                    # What the substitution changes: `change` is
                    # substitution / room_change / moved (canceled for a
                    # cancelled lesson), plus the original subject,
                    # teacher, room, date and lesson number.
                    **{
                        ("change" if key == "kind" else key): value
                        for key, value in lesson_change(change.date, lesson, data).items()
                    },
                },
            )

    def _fire_extra_item_events(self, data: LibrusData) -> None:
        """New text grades (as EVENT_NEW_GRADE with `kind: text`), school
        trips and school documents - seeded silently on the first sync."""
        entry_id = self.config_entry.entry_id if self.config_entry else None
        student = data.me.display_name
        text_grades = {
            grade.id: {
                "subject_id": grade.subject_id,
                "subject": data.subjects.get(grade.subject_id) if grade.subject_id is not None else None,
                "value": grade.value,
                "teacher": _teacher_name(data, grade.teacher_id),
                "category": grade.category,
                "weight": None,
                "counts_to_average": grade.counts_to_average,
                "comments": [],
                "date": grade.date,
                "semester": grade.semester,
                "kind": "text",
                "improves": None,
            }
            for grade in data.text_grades
        }
        trips = {
            trip.id: {
                "destination": trip.destination,
                "route": trip.route,
                "transport": trip.transport,
                "date_from": trip.date_from,
                "date_to": trip.date_to,
                "coordinator": trip.coordinator,
            }
            for trip in data.school_trips
        }
        files = {
            item.id: {"name": item.name, "added": item.added, "url": school_file_url(item.download_path)}
            for item in data.school_files
        }
        for kind, event, items in (
            ("text_grades", EVENT_NEW_GRADE, text_grades),
            ("school_trips", EVENT_NEW_SCHOOL_TRIP, trips),
            ("school_files", EVENT_NEW_SCHOOL_DOCUMENT, files),
        ):
            if kind == "text_grades" and "BaseTextGrades" in self.fallback_sections:
                continue
            self._known_items[kind] = self._fire_for_new_ids(
                event, entry_id, self._known_items[kind], items, student=student
            )

    def _fire_justification_events(self, data: LibrusData) -> None:
        """EVENT_JUSTIFICATION_STATUS when a submitted justification's
        status changes (the school accepted or rejected it)."""
        current = {str(j.id): j.status for j in data.justifications}
        known = self._known_justifications
        self._known_justifications = {**(known or {}), **current}
        if known is None or "Justifications" in self.fallback_sections:
            return
        for item in data.justifications:
            before = known.get(str(item.id))
            if before is None or before == item.status:
                continue
            self.hass.bus.async_fire(
                EVENT_JUSTIFICATION_STATUS,
                {
                    "entry_id": self.config_entry.entry_id if self.config_entry else None,
                    "student": data.me.display_name,
                    "id": item.id,
                    "status": item.status,
                    "previous_status": before,
                    "accepted": item.is_accepted,
                    "rejected": item.is_rejected,
                    "date_from": item.date_from,
                    "date_to": item.date_to,
                    "justified_absences": item.justified_absences,
                    "message": item.message[:300],
                    "teachers": item.teachers,
                },
            )

    def _fire_agenda_change_events(self, data: LibrusData, today: date) -> None:
        """EVENT_AGENDA_CHANGED for an upcoming Agenda entry whose date,
        time, text, category or subject changed since the last sync, or
        that disappeared from Librus. Entries dated before today are left
        alone (old entries drop out of Librus's window, and editing them
        doesn't matter any more). New entries fire EVENT_NEW_HOMEWORK
        instead, via the change tracker."""
        current = {str(item.id): _agenda_fields(item, data) for item in data.homeworks}
        known = self._known_agenda
        self._known_agenda = current
        if known is None or "HomeWorks" in self.fallback_sections:
            return
        if known and not current:
            # Everything gone at once is a failed or emptied fetch, not a
            # school cancelling every event - don't announce it.
            self._known_agenda = known
            return
        today_iso = today.isoformat()
        base = {
            "entry_id": self.config_entry.entry_id if self.config_entry else None,
            "student": data.me.display_name,
        }

        def upcoming(fields: dict[str, Any]) -> bool:
            return (fields.get("date") or "")[:10] >= today_iso

        for item_id, fields in current.items():
            before = known.get(item_id)
            if before is None:
                continue
            changed = [key for key in _AGENDA_COMPARED if before.get(key) != fields.get(key)]
            if not changed or not (upcoming(fields) or upcoming(before)):
                continue
            self.hass.bus.async_fire(
                EVENT_AGENDA_CHANGED,
                {
                    **base,
                    "id": _restore_id(item_id),
                    "kind": "changed",
                    **fields,
                    "changed_fields": changed,
                    "previous": {key: before.get(key) for key in changed},
                },
            )
        for item_id, before in known.items():
            if item_id in current or not upcoming(before):
                continue
            self.hass.bus.async_fire(
                EVENT_AGENDA_CHANGED,
                {**base, "id": _restore_id(item_id), "kind": "removed", **before},
            )

    def _fire_new_homework_assignment_events(self, data: LibrusData) -> None:
        """EVENT_NEW_HOMEWORK_ASSIGNMENT for each newly-seen real homework
        assignment, seeded silently on the first sync."""
        by_teacher = teacher_subject_ids(data.timetable)
        items: dict[Any, dict[str, Any]] = {}
        for assignment in data.homework_assignments:
            subject_id = infer_subject_id(assignment.teacher_id, by_teacher)
            items[assignment.id] = {
                "topic": assignment.topic,
                "text": (assignment.text or "")[:500],
                "date": assignment.date,
                "due_date": assignment.due_date,
                "teacher": _teacher_name(data, assignment.teacher_id),
                "subject_id": subject_id,
                "subject": data.subjects.get(subject_id) if subject_id is not None else None,
            }
        self._known_homework_assignment_ids = self._fire_for_new_ids(
            EVENT_NEW_HOMEWORK_ASSIGNMENT,
            self.config_entry.entry_id if self.config_entry else None,
            self._known_homework_assignment_ids,
            items,
            student=data.me.display_name,
        )

    def _check_achievements(
        self,
        grades: list[GradeData],
        attendances: list[AttendanceData],
        attendance_types: dict[int, AttendanceTypeData],
        notes: list[NoteData],
        today: date,
        student: str,
    ) -> None:
        """Fires EVENT_ACHIEVEMENT_UNLOCKED for a handful of objective,
        data-derived gamification milestones - deliberately never an
        invented points/scoring system, which would have no basis in
        anything Librus actually reports and would feel arbitrary/made up.

        Each achievement KEY (e.g. "good_grade_streak_10") is treated as
        an "item id" (see `_fire_for_new_ids`) that's either currently
        unlocked or not, seeded
        silently on the first sync, and unioned (not replaced) so nothing
        re-fires once achieved even if the underlying streak later
        resets (a bad grade breaking a 10-grade streak must not "revoke"
        the achievement already earned)."""
        entry_id = self.config_entry.entry_id if self.config_entry else None
        unlocked: set[str] = set()

        day_to_day_grades = [
            g
            for g in grades
            if not g.is_semester_proposition
            and not g.is_final_proposition
            and not g.is_semester
            and not g.is_final
        ]
        if any(parse_grade_value(g.value) == 6.0 for g in day_to_day_grades):
            unlocked.add("first_six")

        streak = good_grade_streak(grades)
        for milestone in _GOOD_GRADE_STREAK_MILESTONES:
            if streak >= milestone:
                unlocked.add(f"good_grade_streak_{milestone}")

        attendance_days = days_since_last_absence(
            attendances, attendance_types, self._cached_class, today
        )
        if attendance_days is not None:
            for milestone in _STREAK_DAY_MILESTONES:
                if attendance_days >= milestone:
                    unlocked.add(f"attendance_streak_{milestone}")

        behaviour_days = days_since_last_negative_note(notes, self._cached_class, today)
        if behaviour_days is not None:
            for milestone in _STREAK_DAY_MILESTONES:
                if behaviour_days >= milestone:
                    unlocked.add(f"behaviour_streak_{milestone}")

        self._known_achievements = self._fire_for_new_ids(
            EVENT_ACHIEVEMENT_UNLOCKED,
            entry_id,
            self._known_achievements,
            {key: {"title": _ACHIEVEMENT_TITLES[key]} for key in unlocked},
            student=student,
        )

    def _fire_for_new_ids(
        self,
        event: str,
        entry_id: str | None,
        known: set[Any] | None,
        items: dict[Any, dict[str, Any]],
        *,
        student: str | None = None,
    ) -> set[Any]:
        current_ids = set(items)
        if known is None:
            # First-ever refresh for this entry: seed silently. Firing here
            # would replay the account's whole history as "new" on install.
            return current_ids
        new_ids = current_ids - known
        for item_id in new_ids:
            self.hass.bus.async_fire(
                event, {"entry_id": entry_id, "id": item_id, "student": student, **items[item_id]}
            )
        # Union, not replace: an item that later drops out of the fetch
        # window must not be re-announced if it reappears.
        return known | current_ids


# ----------------------------------------------------------------------
# Parsing. Defensive throughout (missing keys default sensibly) - Librus
# doesn't publish a schema and per-school variations are known to exist
# upstream (see RustySnek/librus-apix's README).
# ----------------------------------------------------------------------
