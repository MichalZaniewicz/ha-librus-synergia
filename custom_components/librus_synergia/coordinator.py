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
import copy
import dataclasses
import logging
import re
from collections.abc import Awaitable
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from librus_synergia import (
    LibrusApiClient,
    LibrusAuthError,
    LibrusError,
    LibrusSessionData,
    LibrusSessionExpiredError,
)
from librus_synergia.changes import Changes, ChangeTracker, SeenIds
from librus_synergia.exceptions import LibrusUnexpectedResponseError
from librus_synergia.changes import absences as tracked_absences
from librus_synergia.changes import timetable_changes as tracked_timetable_changes
from librus_synergia.models import (
    AttendanceData,
    AttendanceTypeData,
    ClassData,
    FreeDayData,
    GradeCategoryData,
    GradeData,
    GradingSystemData,
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
    extract_student_identifier,
    extract_token_user_identifier,
    merge_timetables,
    parse_attendance_types,
    parse_attendances,
    parse_behaviour_grades,
    parse_class,
    parse_comment_text_map,
    parse_descriptive_grades,
    parse_auth_subjects,
    parse_descriptive_skills,
    parse_grading_system,
    parse_partial_grades,
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
    parse_message,
    parse_message_list,
    parse_messages,
    parse_notes,
    parse_parent_teacher_conferences,
    parse_justifications,
    parse_realizations,
    parse_school_files,
    parse_school_trips,
    parse_text_grade_categories,
    parse_timetable_entries,
    plan_differences,
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
    CONF_COOKIES,
    CONF_DESCRIPTIVE_GRADES_ENABLED,
    CONF_FREE_DAYS_ENABLED,
    CONF_GRADE_THRESHOLDS,
    CONF_MESSAGES_ENABLED,
    CONF_QUIET_HOURS_ENABLED,
    CONF_QUIET_HOURS_END,
    CONF_QUIET_HOURS_START,
    CONF_SESSION_LOGGED_IN_AT,
    CONF_STUDENT_NUMBER,
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
    EVENT_MESSAGE_READ,
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
    STATE_SAVE_MAX_INTERVAL,
    STATE_STORE_VERSION,
    STATUS_DEGRADED,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_STALE,
)
from .achievements import Badge, compute_badges, key_title
from .forecast import average_sums, forecast_basis, parse_thresholds, subject_forecasts

if TYPE_CHECKING:
    from .ai_summary import LibrusWeeklySummary

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

# Label for degraded-endpoint tracking of the informacja web page (listed
# in const.MISC_DEGRADABLE_ENDPOINT_LABELS too).
STUDENT_INFO_LABEL = "Informacja"

# Kindergarten discovery (see `_async_maybe_discover_kindergarten`): how
# long to wait before trying again after finding nothing, and how many
# candidate LIDs to probe per attempt.
_KINDERGARTEN_DISCOVERY_RETRY = timedelta(hours=24)
_ARCHIVE_REFRESH_INTERVAL = timedelta(hours=24)
# Wiadomości mailbox for past school years.
ARCHIVE_MAILBOX = "archive/inbox"
_KINDERGARTEN_MAX_CANDIDATES = 6

# The order of `_async_fetch_core_payloads`' result: Me, tier 1, tier 2.
_CORE_PAYLOAD_LABELS = ("Me", *CORE_ENDPOINT_LABELS, *OPTIONAL_ENDPOINT_LABELS)
# Optional endpoints parsed in _build_data from their raw payloads.
_EXTRA_LABELS = (
    "BaseTextGrades",
    "Realizations",
    "SchoolTrips",
    "SchoolFiles",
    "TimetableEntries",
    "DescriptiveGrades/Comments",
    "DescriptiveGrades/Skills",
    "PartialGrades",
    "Auth/Subjects",
)
# Read receipts: the newest sent messages from the last this-many days.
_READ_RECEIPT_DAYS = 30
_READ_RECEIPT_MESSAGES = 5
# Lesson topics, trips and school documents change a few times a day.
_MESSAGES_QUICK_RECHECKS = 3
# Reference endpoints for a school setting that may be closed to an account
# (403/404): the default is used instead of a degraded-endpoint issue.
_SCHOOL_SETTING_LABELS = frozenset({"GradingSystem"})
# A badge earned longer ago than this is recorded without an event - a week,
# so HA being off for a few days doesn't swallow one.
_ACHIEVEMENT_NEWS_DAYS = timedelta(days=7)
_HOURLY = timedelta(hours=1)
# The standing weekly plan changes a few times a year.
_DAILY = timedelta(days=1)
# A mailbox Librus answered 404 for (the account doesn't have it) is asked
# again after this long, not every cycle.
_MISSING_MAILBOX_RECHECK = timedelta(hours=24)
# A message list is reused while its mailbox's unread count stays the same,
# for at most this long (a message that arrived and was read in between
# leaves the count unchanged).
_MESSAGE_LIST_MAX_AGE = timedelta(hours=1)
# At most this many reference-data requests in flight at once - also the
# limit for the independent requests that follow the core fetch (see
# `_async_fetch_live`), which share the same semaphore.
_REFERENCE_CONCURRENCY = 6
# Timetable weeks fetched on demand (a calendar month view, Assist) - see
# `async_get_timetable_week`. A week within _WEEK_CACHE_NEAR of today is
# trusted for _WEEK_CACHE_NEAR_TTL (a substitution there still matters),
# further ones for _WEEK_CACHE_FAR_TTL; nothing older than _WEEK_CACHE_KEEP
# is kept. At most _WEEK_FETCH_CONCURRENCY weeks are fetched at once.
_WEEK_CACHE_NEAR = timedelta(weeks=3)
_WEEK_CACHE_NEAR_TTL = timedelta(hours=2)
_WEEK_CACHE_FAR_TTL = timedelta(hours=24)
_WEEK_CACHE_KEEP = timedelta(weeks=8)
_WEEK_FETCH_CONCURRENCY = 2
# BaseTextGrades, BehaviourGrades/Points and DescriptiveGrades: most accounts
# never have any, and the ones that do get a few a month - asked hourly
# while the last answer was empty (or, for the behaviour grade, always).
_SPARSE_REFRESH = timedelta(hours=1)
# The payloads part of the saved state (every last good response, the big
# part) is written at most this often - and on unload, and when Home
# Assistant stops (the store's final write); the small tracked state (seen
# ids, ...) right after a change. See `_maybe_schedule_save`.
_PAYLOADS_SAVE_INTERVAL = timedelta(hours=6)
# On-demand timetable weeks (async_get_timetable_week): a failed fetch is not
# retried for this long (the last copy or no lessons meanwhile), a week
# further than ON_DEMAND_WEEKS from the current one is never fetched, and a
# rejected session isn't logged in again for an on-demand week within
# _ON_DEMAND_RELOGIN_GAP of the last forced login.
_WEEK_FAILURE_BACKOFF = timedelta(minutes=15)
ON_DEMAND_WEEKS = 12
_ON_DEMAND_RELOGIN_GAP = timedelta(minutes=10)
# A kindergarten child's LID whose timetable Librus has refused (403) or
# failed to give for _KINDERGARTEN_REFUSED_RESET, or that has had no lesson
# in either polled week for _KINDERGARTEN_EMPTY_RESET, is dropped and the
# ordinary Timetables asked again (a child who moved on to school, a LID that
# stopped working). The empty window is long because a school break (winter,
# Easter) is not a reason: dropped after a day, the LID was found again -
# the same one, the past 30 days had lessons - every day of a break.
_KINDERGARTEN_REFUSED_RESET = timedelta(hours=24)
_KINDERGARTEN_EMPTY_RESET = timedelta(days=21)
# Read receipts: at most this many sent messages asked for at once.
_READ_RECEIPT_CONCURRENCY = 3

# Kindergarten entry summary for diagnostics (issue #14, which it solved:
# `planned`/`cancelled`/`substitution`/`substituted`, see librus-synergia's
# kindergarten notes). Kept to catch types not seen yet - entries by type,
# field names and value shapes, never names or ids. A string is shown as is
# only when it looks like an enum (starts lowercase, letters only); a LID is
# "lid", or "ref:<type>" when it is another entry's own `identifier` (how a
# substitution would point at the block it replaces).
_KG_ENUM_RE = re.compile(r"^[a-z][A-Za-z_]{0,31}$")
_KG_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_KG_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
_KG_EXAMPLE_KEYS = ("date", "startTime", "endTime")
_KG_MAX_EXAMPLES = 3


def _kindergarten_value_shape(value: Any, own_ids: dict[str, str], own: str | None) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        if value in own_ids and value != own:
            return f"ref:{own_ids[value]}"
        if value.startswith("LID-") or value == own:
            return "lid"
        if _KG_DATE_RE.match(value):
            return "date"
        if _KG_TIME_RE.match(value):
            return "time"
        if _KG_ENUM_RE.match(value):
            return f"'{value}'"
        return "text"
    if isinstance(value, list):
        inner = sorted({_kindergarten_value_shape(item, own_ids, own) for item in value})
        return f"list[{', '.join(inner)}]"
    if isinstance(value, dict):
        return f"object{{{', '.join(sorted(str(key) for key in value))}}}"
    return type(value).__name__


def summarize_kindergarten_entries(entries: Any) -> dict[str, Any]:
    """Per entry `type`: how many, every field's value shapes, and a few
    examples (date and times only) - see `_KG_ENUM_RE`."""
    if not isinstance(entries, list):
        return {}
    items = [entry for entry in entries if isinstance(entry, dict)]
    own_ids = {
        entry["identifier"]: str(entry.get("type") or "planned")
        for entry in items
        if isinstance(entry.get("identifier"), str)
    }
    summary: dict[str, Any] = {}
    for entry in items:
        kind = str(entry.get("type") or "planned")
        bucket = summary.setdefault(kind, {"count": 0, "fields": {}, "examples": []})
        bucket["count"] += 1
        own = entry.get("identifier") if isinstance(entry.get("identifier"), str) else None
        for key, value in entry.items():
            shapes = bucket["fields"].setdefault(str(key), [])
            shape = _kindergarten_value_shape(value, own_ids, own)
            if shape not in shapes:
                shapes.append(shape)
        if len(bucket["examples"]) < _KG_MAX_EXAMPLES:
            bucket["examples"].append(
                {key: entry.get(key) for key in _KG_EXAMPLE_KEYS if isinstance(entry.get(key), str)}
            )
    return summary


def _week_start(day: date) -> date:
    """The Monday of `day`'s ISO week."""
    return day - timedelta(days=day.weekday())


_USERS_SOURCE_RE = re.compile(r"^Users/(\d+)$")


def _kindergarten_source_label(source: str | None, me_payload: Any) -> str | None:
    """A kindergarten discovery source without the account's numeric id:
    "Users/<id>" (saved by older versions) becomes "Users/UserId" or
    "Users/Id" - the `Me.Account` field it came from - and "Users/Id"
    when `/Me` no longer tells (the id is redacted everywhere else in
    diagnostics; this one was exported as is)."""
    if source is None or (match := _USERS_SOURCE_RE.match(source)) is None:
        return source
    me = me_payload.get("Me") if isinstance(me_payload, dict) else None
    account = me.get("Account") if isinstance(me, dict) else None
    number = int(match.group(1))
    if isinstance(account, dict) and account.get("UserId") == number:
        return "Users/UserId"
    return "Users/Id"


def on_demand_week_starts(first: date, last: date) -> list[date]:
    """The Mondays of the weeks from `first` to `last` (any days in them)
    that an on-demand timetable request may cover - at most ON_DEMAND_WEEKS
    weeks either side of the current one. A dashboard asking for a whole
    year (or a far-off date) gets no lessons there instead of dozens of
    requests to Librus."""
    this_week = _week_start(dt_util.now().date())
    limit = timedelta(weeks=ON_DEMAND_WEEKS)
    week = max(_week_start(first), this_week - limit)
    end = min(_week_start(last), this_week + limit)
    weeks: list[date] = []
    while week <= end:
        weeks.append(week)
        week += timedelta(days=7)
    return weeks


def _school_year(day: date) -> int:
    """The calendar year a school year starts in (September)."""
    return day.year if day.month >= 9 else day.year - 1


def _same_signature(new: tuple[Any, ...], old: tuple[Any, ...]) -> bool:
    """Two `_build_signature` results: the lookups the very same objects,
    everything else equal."""
    (new_identity, new_values), (old_identity, old_values) = new, old
    return (
        len(new_identity) == len(old_identity)
        and all(a is b for a, b in zip(new_identity, old_identity))
        and new_values == old_values
    )


def _has_lessons(payload: Any) -> bool:
    """Whether one week's raw timetable payload (either API) holds a lesson."""
    try:
        return any(merge_timetables(payload).values())
    except Exception:  # noqa: BLE001 - a garbled payload just has no lessons
        return False


# Positions in `_async_fetch_core_payloads`' result.
_TIMETABLE_THIS_WEEK = _CORE_PAYLOAD_LABELS.index("Timetable (this week)")
_TIMETABLE_NEXT_WEEK = _CORE_PAYLOAD_LABELS.index("Timetable (next week)")
# Both weeks live in `_timetable_cache` (keyed by the week's Monday, so the
# copy can't belong to the wrong week after Sunday); they aren't kept among
# the last good responses too.
_TIMETABLE_WEEK_LABELS = frozenset({"Timetable (this week)", "Timetable (next week)"})
# The comment lookups and the raw payload whose `Comments` ids they resolve.
_COMMENT_SOURCES = {
    "Grades/Comments": "Grades",
    "BehaviourGrades/Points/Comments": "BehaviourGrades/Points",
}


def _as_id(value: Any) -> Any:
    """An id from a raw payload as the lookups key it: int when it is one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def _referenced_ids(payload: Any, list_key: str, field: str) -> set[Any]:
    """Ids of `field` (`{"Id": ...}`) across the items of a raw list payload,
    e.g. every grade's category id."""
    items = payload.get(list_key) if isinstance(payload, dict) else None
    ids: set[Any] = set()
    for item in items if isinstance(items, list) else []:
        ref = item.get(field) if isinstance(item, dict) else None
        if isinstance(ref, dict) and ref.get("Id") is not None:
            ids.add(_as_id(ref["Id"]))
    return ids


def _referenced_comment_ids(payload: Any, list_key: str = "Grades") -> set[Any]:
    """Comment ids the items of a raw grades payload point at - bare ids or
    `{"Id": ...}` objects (CONFIRMED live 2026-10-09: objects)."""
    items = payload.get(list_key) if isinstance(payload, dict) else None
    ids: set[Any] = set()
    for item in items if isinstance(items, list) else []:
        comments = item.get("Comments") if isinstance(item, dict) else None
        for entry in comments if isinstance(comments, list) else []:
            comment_id = entry.get("Id") if isinstance(entry, dict) else entry
            if comment_id is not None:
                ids.add(_as_id(comment_id))
    return ids


def _comment_ids(payload: Any) -> set[Any]:
    """Every comment id a `{"Comments": [{"Id", "Text"}]}` payload holds
    (with or without text - an empty one is still known)."""
    items = payload.get("Comments") if isinstance(payload, dict) else None
    return {
        _as_id(item["Id"])
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict) and item.get("Id") is not None
    }


def school_file_url(path: str | None) -> str | None:
    """Absolute Synergia URL of a school document's download path."""
    if not path:
        return None
    return path if path.startswith("http") else f"https://synergia.librus.pl{path}"


# The reference responses that must all be saved for the lookups to be
# rebuilt from them after a restart (async_restore_state). The grade scale
# may be closed to an account - then the default one is used anyway.
_RESTORABLE_REFERENCE_LABELS = tuple(
    label for label in REFERENCE_DATA_ENDPOINT_LABELS if label not in _SCHOOL_SETTING_LABELS
)
# Labels of the two point-grade requests (const.MISC_DEGRADABLE_ENDPOINT_LABELS).
_POINT_GRADE_LABELS = ("PointGrades", "PointGrades/Categories")
# `_async_get_messages`' result when nothing was fetched.
_NO_MESSAGES: tuple[Any, ...] = (0, {}, [], [], [], [], [], [])


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
    """Storage key of one entry's saved coordinator state (the tracked
    part - see `LibrusDataUpdateCoordinator._maybe_schedule_save`)."""
    return f"{DOMAIN}.{entry_id}.state"


def payload_store_key(entry_id: str) -> str:
    """Storage key of one entry's saved responses (the big part)."""
    return f"{DOMAIN}.{entry_id}.payloads"


def _cookie_identity(cookies: Any) -> frozenset[tuple[Any, ...]]:
    """What decides whether the config entry's copy of the session cookies
    needs rewriting: each cookie's name, domain and path - and the value of
    the long-lived DeviceCookie, whose loss would bring back the captcha.
    The short-lived token values change with every refresh and live in the
    coordinator's saved state instead."""
    if not isinstance(cookies, list):
        return frozenset()
    return frozenset(
        (
            cookie.get("name"),
            cookie.get("domain"),
            cookie.get("path", "/"),
            cookie.get("value") if cookie.get("name") == "DeviceCookie" else None,
        )
        for cookie in cookies
        if isinstance(cookie, dict)
    )


_MESSAGE_FIELDS = frozenset(field.name for field in dataclasses.fields(MessageData))


def _messages_from_saved(items: list[Any]) -> list[MessageData]:
    """Message previews saved by `_throttles_to_save`; one that no longer
    fits the library's MessageData is dropped (refetched anyway)."""
    messages = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            messages.append(MessageData(**{k: v for k, v in item.items() if k in _MESSAGE_FIELDS}))
        except TypeError:
            continue
    return messages


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


def good_grade_streak(
    grades: list[GradeData], grading: GradingSystemData | None = None
) -> int:
    """Consecutive most-recent NUMERIC grades >= _GOOD_GRADE_STREAK_THRESHOLD,
    counting back from the newest until the first one below it. Semester/
    final grades (proposed OR actual) excluded (not day-to-day grades,
    same as the average calculation). A non-numeric mark (bz/np/...) is
    SKIPPED, not counted as breaking the streak - it isn't really a "bad
    grade", just an administrative mark, and penalizing it would feel
    unfair for what this is meant to be: a small, motivating "passa" a
    student can watch grow. `grading` is the school's grade scale (what
    "+"/"-" add) - the same one every average here uses; without it the
    streak used the default +0.5/-0.25 whatever the school's scale says."""
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
        value = parse_grade_value(grade.value, grading)
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
    grading: GradingSystemData | None = None,
) -> float | None:
    """Grade average, excluding semester/final entries (proposed OR
    actual - see GradeData.is_semester/is_final's own docstring for why
    the actual ones matter too, not just the propositions) and
    categories marked as not counting toward the average. Weighted by the
    grade category's weight unless `weighted=False` (plain arithmetic
    mean of the same counted grades). `semester` restricts to grades from
    that semester when given."""
    running, weight_total = average_sums(
        grades,
        categories,
        subject_id=subject_id,
        semester=semester,
        weighted=weighted,
        grading=grading,
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
        # When the LID was found (its school year is checked - a new school
        # year looks again), since when its timetable has been empty, since
        # when it has been refused (or failing) and when the discovery last
        # found the same LID again after dropping it (the empty window counts
        # from then) - see `_kindergarten_lid_stale`.
        self._kindergarten_found_on: date | None = None
        self._kindergarten_empty_since: datetime | None = None
        self._kindergarten_refused_since: datetime | None = None
        self._kindergarten_confirmed_at: datetime | None = None
        # How each polled kindergarten week went this cycle (True: answered,
        # False: refused or failed) - set by `_fetch_timetable_or_unpublished`,
        # read and cleared by `_kindergarten_lid_stale`.
        self._kindergarten_week_ok: dict[date, bool] = {}
        # The polled kindergarten weeks' entries by type, for diagnostics
        # (`summarize_kindergarten_entries`). In memory only.
        self._kindergarten_entry_summary: dict[str, dict[str, Any]] = {}
        # The LID `_forget_kindergarten` just dropped, with its empty clock and
        # the reference-data time - so the discovery finding the same LID
        # again doesn't count as a new one (no INFO log, no reference refresh,
        # the empty clock keeps running). In memory only.
        self._kindergarten_dropped: dict[str, Any] | None = None
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
        # Grade categories and attendance types - reference data too (see
        # REFERENCE_DATA_ENDPOINT_LABELS). An id that isn't in them is asked
        # for again in the same cycle (_async_resolve_missing_lookups): when
        # that was last done and which ids Librus itself didn't know then.
        self._cached_grade_categories: dict[int, GradeCategoryData] = {}
        self._cached_attendance_types: dict[int, AttendanceTypeData] = {}
        self._lookup_refetched_at: dict[str, datetime] = {}
        self._unresolved_lookup_ids: dict[str, set[Any]] = {}

        # The lucky number is normally published once a day (see
        # _async_get_lucky_number for when it is asked for).
        self._cached_lucky_number: LuckyNumberData | None = None
        self._lucky_number_fetched_at: datetime | None = None

        # Wiadomości (messages) needs a one-time-per-login bootstrap (a
        # separate session cookie on wiadomosci.librus.pl) - not every cycle,
        # and not every school has this module enabled, so failure here is
        # non-fatal (see _async_get_messages).
        self._messages_bootstrapped = False
        self._messages_available = False
        # A "no access" bootstrap answer is checked again: right after a
        # restart it can come from a session another request was just
        # replacing, not from a school without Wiadomości. The first few
        # answers are rechecked every cycle, then once an hour.
        self._messages_denials = 0
        self._messages_recheck_at: datetime | None = None
        # Forced relogins one at a time (see _async_relogin).
        self._login_lock = asyncio.Lock()
        self._login_count = 0
        # Mailboxes Librus answered 404 for - this account doesn't have
        # them (the Messages card hides their chips). Asked again once a
        # day (_MISSING_MAILBOX_RECHECK), from when each was last probed.
        self.missing_mailboxes: set[str] = set()
        self._mailbox_probed_at: dict[str, datetime] = {}
        # The last message list of each mailbox, when it was fetched and the
        # mailbox's unread count then - reused while that count stays the
        # same (see _reusable_mailbox_list). The unread counts themselves are
        # asked for every cycle.
        self._mailbox_lists: dict[str, list[MessageData]] = {}
        self._mailbox_fetched_at: dict[str, datetime] = {}
        self._mailbox_counts: dict[str, int | None] = {}
        # Mailboxes whose fetch failed this cycle (their list is empty then).
        self._failed_mailboxes: set[str] = set()
        # The archive (past school years) is read once a day.
        self._archived_messages: list[MessageData] = []
        # Read receipts of recently sent messages: message id -> who it
        # went to and when each one read it (see _async_refresh_read_receipts).
        self.read_receipts: dict[str, dict[str, Any]] = {}
        self._receipts_fetched_at: dict[str, datetime] = {}
        self._archive_fetched_at: datetime | None = None

        # New-item bus events: the library's ChangeTracker remembers what
        # has been seen and reports what's new. The first cycle only seeds
        # it instead of replaying history as "new" on install; what has been
        # seen is saved across restarts (async_restore_state).
        self._change_tracker = ChangeTracker()
        # Tracker kinds whose data wasn't there when the tracker was seeded
        # (a failed fetch, a module switched off in the options, an
        # unpublished timetable). The first time real data arrives for one
        # of them it is recorded silently instead of being announced as a
        # whole batch of "new" items.
        self._unseeded_kinds: set[str] = set()
        # Earned badge key ("sixes_10") -> date earned (YYYY-MM-DD). None =
        # not seeded yet: the first computation records everything silently
        # (badges are dated from the whole school year, so an install or an
        # upgrade must not announce them all). Never revoked once earned.
        # See _check_achievements and achievements.py.
        self._achievement_dates: dict[str, str] | None = None
        # The school year (its start date) the badges above belong to; a new
        # school year starts them over.
        self._achievement_year: str | None = None
        # Badges from the last cycle, for the Rank sensor.
        self.badges: list[Badge] = []
        # Homework ever ticked in the to-do list (the to-do itself forgets
        # ticks once Librus drops the homework) - for the homework badge.
        self.homework_done_ever: set[str] = set()
        # Badge keys saved by 0.12.4 or older (no dates), until the next
        # cycle merges them into `_achievement_dates` - saved under the old
        # "achievements" key meanwhile, so a save before that (an unload
        # while Librus is down) doesn't lose them.
        self._legacy_achievements: list[str] = []
        # The dates hold only legacy keys so far: the first pass that
        # records badges is still the quiet seeding one.
        self._achievement_seed_pending = False
        # Set on unload (async_close_state): no more delayed writes after the
        # final one.
        self._closed = False
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
        # Ids already announced per kind (text grades, descriptive grades,
        # school trips, school documents); None until the first sync, which
        # only seeds them.
        self._known_items: dict[str, set[Any] | None] = {
            "text_grades": None,
            "descriptive_grades": None,
            "school_trips": None,
            "school_files": None,
        }
        # When each throttled optional endpoint last answered (see
        # _async_optional).
        self._fetched_at: dict[str, datetime] = {}
        self._cached_text_grade_categories: dict[int, tuple[str, bool]] = {}
        # The school's grade scale ("+"/"-" values, whether 0 counts).
        self._cached_grading_system = GradingSystemData()
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
        # On-demand weeks outside the polled two (see async_get_timetable_
        # week): week Monday -> (lessons, fetched at), the fetches in flight,
        # and how many may run at once.
        self._week_cache: dict[date, tuple[dict[date, list[LessonData]], datetime]] = {}
        self._week_fetches: dict[date, asyncio.Task[dict[date, list[LessonData]]]] = {}
        self._week_fetch_limit = asyncio.Semaphore(_WEEK_FETCH_CONCURRENCY)
        # Week Monday -> when its on-demand fetch last failed (not retried
        # for _WEEK_FAILURE_BACKOFF), and when the last forced login was made.
        self._week_failed_at: dict[date, datetime] = {}
        self._last_forced_login_at: datetime | None = None
        # The shared clock of the school-day entities (school_day.py),
        # created by the first one; stopped on unload.
        self.school_day_clock: Any = None
        # The saved state is two files (see _maybe_schedule_save): the small
        # tracked state above (`_state_store` - seen ids, receipts, badges,
        # the session, throttles), written soon after it changes, and the
        # big payloads (`_payload_store` - every last good response and the
        # two timetable weeks), written at most every
        # _PAYLOADS_SAVE_INTERVAL and on unload. A stored response that
        # differs from the one before sets `_state_dirty`; the tracked state
        # is compared with a copy of what was last handed to its store
        # (`_saved_view`, see `_tracked_view`).
        self._payload_store: Store[dict[str, Any]] = Store(
            hass, STATE_STORE_VERSION, payload_store_key(entry.entry_id)
        )
        self._state_dirty = False
        self._saved_view: Any = None
        self._state_saved_at: datetime | None = None
        self._payloads_saved_at: datetime | None = None
        # When a stored response or polled timetable week last really changed
        # - saved in the tracked state (soon after it moves), so a restart can
        # tell whether the payloads file holds it (see async_restore_state).
        self._payloads_changed_at: datetime | None = None
        # A delayed payloads write is handed to the store and not written
        # yet (handing it over again would only push it later).
        self._payload_save_pending = False
        # Bumped whenever a stored response or timetable week changes - with
        # the inputs below, what tells an unchanged poll (see
        # `_build_signature`): then the last data object is kept without
        # parsing everything again.
        self._input_version = 0
        self._built_signature: tuple[Any, ...] | None = None
        self._built_data: LibrusData | None = None
        # The data object and day the forecast events / badges were last
        # worked out for (skipped while both stay the same).
        self._forecast_checked: tuple[Any, ...] | None = None
        self._achievements_checked: tuple[Any, ...] | None = None
        # Bumped whenever the change tracker's seen ids change (new items,
        # seeding, pruning) - compared instead of the id sets themselves.
        self._seen_version = 0
        # The newest session cookies (see remember_session) and the grade-
        # average statistics digests (average_history.py), both saved in
        # the tracked state.
        self._session_state: dict[str, Any] | None = None
        self.average_digests: dict[str, str] = {}
        # The last good Wiadomości result, shown while a fetch fails.
        self._last_messages: tuple[Any, ...] | None = None
        # Requests after the core fetch (reference data, extras, read
        # receipts) share this limit - see `_limited`.
        self._request_limit = asyncio.Semaphore(_REFERENCE_CONCURRENCY)
        # Health of the last cycles, for the Status / Last update sensors.
        self.last_success_at: datetime | None = None
        # The last FAILED attempt (None until a cycle fails) - a successful
        # one is `last_success_at`.
        self.last_attempt_at: datetime | None = None
        self.last_error: str | None = None
        self.failures = 0
        self.next_attempt_at: datetime | None = None
        # "live" (fetched this cycle), "stale" (Librus failed, showing the
        # last data) or "cache" (rebuilt from the saved responses at start).
        self.data_source = "live"
        # Sections shown from their saved copy this cycle (they failed).
        self.fallback_sections: set[str] = set()
        # Every endpoint label that failed this cycle, whether or not a saved
        # copy stood in for it - their new-item tracking waits for real data.
        self._failed_this_cycle: set[str] = set()
        # Whether the school grades in points (Units); None until known.
        self.point_grades_enabled: bool | None = None

    async def async_restore_state(self) -> None:
        """Load what the previous run saved: the ids already announced (so
        a grade added while HA was off still fires its event instead of
        being swallowed by the silent first-poll seeding), the last good
        responses, when Librus last answered, the newest session cookies and
        when each throttled request was last made (so a restart or an
        options change doesn't ask Librus for every daily lookup again). A
        missing or unreadable file just means a fresh start."""
        stored = await self._async_load(self._state_store)
        payload_stored = await self._async_load(self._payload_store)
        if stored is None and payload_stored is None:
            return
        stored = stored or {}
        if isinstance(seen := stored.get("seen"), dict):
            self._change_tracker = ChangeTracker(SeenIds.from_dict(seen))
            if isinstance(kinds := stored.get("unseeded_kinds"), list):
                self._unseeded_kinds = {str(k) for k in kinds}
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
        if isinstance(dates := stored.get("achievement_dates"), dict):
            self._achievement_dates = {str(k): str(v) for k, v in dates.items()}
        if isinstance(receipts := stored.get("read_receipts"), dict):
            self.read_receipts = receipts
        if isinstance(year := stored.get("achievement_year"), str):
            self._achievement_year = year
        elif self._achievement_dates is None and isinstance(
            old := stored.get("achievements"), list
        ):
            # Saved by 0.12.4 or older (keys only): kept, dated today, so an
            # achievement earned under the old rules isn't lost.
            self._legacy_achievements = [str(k) for k in old]
        self._achievement_seed_pending = stored.get("achievement_seed_pending") is True
        if isinstance(done := stored.get("homework_done_ever"), list):
            self.homework_done_ever = {str(uid) for uid in done}
        forecast = stored.get("forecast")
        if isinstance(forecast, dict) and isinstance(forecast.get("values"), dict):
            self._known_forecast = {
                _restore_id(k): v for k, v in forecast["values"].items() if isinstance(v, int)
            }
            self._known_forecast_basis = forecast.get("basis")
        if isinstance(number := stored.get("student_number"), int):
            self.student_number_from_librus = number
        if isinstance(digests := stored.get("average_digests"), dict):
            self.average_digests = {str(k): str(v) for k, v in digests.items()}
        self._restore_session(stored.get("session"))
        self._restore_kindergarten(stored.get("kindergarten"))
        if saved := stored.get("last_success_at"):
            self.last_success_at = dt_util.parse_datetime(saved)

        # The responses: their own file since 0.12.5-beta.9; older versions
        # kept them in the tracked state (moved to the new file with the next
        # save - `_state_dirty`).
        source = payload_stored if payload_stored is not None else stored
        if payload_stored is None and isinstance(stored.get("payloads"), dict):
            self._state_dirty = True
        # How fresh the saved responses are. They are written separately from
        # the tracked state (with its fetch times) and only when a response
        # changed, so their `saved_at` is often days older than the fetch
        # times (a quiet weekend) - that alone says nothing. What does: the
        # tracked state also keeps when a response last changed
        # (`payloads_changed_at`). Written after that change, the responses
        # file holds everything those fetches brought - all fetch times are
        # trusted. Written before it, a change never reached the file (HA
        # stopped without the final write): fetch times after `saved_at` are
        # not trusted - those responses are asked for again instead of an
        # older copy passing as fresh for up to a day. A file without these
        # times (older versions): fetch times after `saved_at`, or all of them
        # without one, are not trusted - the first restart may ask once more.
        # Both in one file (older versions still): consistent.
        payloads_saved_at: datetime | None = None
        trust_until: datetime | None = None
        changed = stored.get("payloads_changed_at")
        payloads_changed_at = dt_util.parse_datetime(changed) if isinstance(changed, str) else None
        self._payloads_changed_at = payloads_changed_at
        if payload_stored is not None:
            saved_at = payload_stored.get("saved_at")
            payloads_saved_at = dt_util.parse_datetime(saved_at) if isinstance(saved_at, str) else None
            if payloads_saved_at is None:
                trust_until = datetime.min.replace(tzinfo=dt_util.UTC)
            elif payloads_changed_at is None or payloads_saved_at < payloads_changed_at:
                trust_until = payloads_saved_at
        if isinstance(payloads := source.get("payloads"), dict):
            self._last_good = payloads
            if isinstance(units := payloads.get("Units"), dict):
                self.point_grades_enabled = point_grades_enabled(units)
            # Saved by 0.12.5-beta.6 or older: the two timetable weeks were
            # stored here as well as under "timetable". Dropped (and the
            # smaller file written after the next cycle).
            for label in _TIMETABLE_WEEK_LABELS:
                if self._last_good.pop(label, None) is not None:
                    self._state_dirty = True
        if isinstance(weeks := source.get("timetable"), dict):
            self._timetable_cache = weeks
        # Saved by 0.12.5-beta.10 or older: "Users/<the account's numeric
        # id>" - named after the field instead (diagnostics show the source).
        self._kindergarten_source = _kindergarten_source_label(
            self._kindergarten_source, self._last_good.get("Me")
        )
        self._restore_throttles(stored.get("throttles"), trust_until)

        # What was just read is what the files hold: nothing to write until
        # it changes (or STATE_SAVE_MAX_INTERVAL passes).
        self._saved_view = copy.deepcopy(self._tracked_view())
        self._state_saved_at = self.last_success_at
        if self._state_dirty:
            self._payloads_saved_at = None
        elif payload_stored is not None:
            self._payloads_saved_at = payloads_saved_at
        else:
            self._payloads_saved_at = self.last_success_at

    async def _async_load(self, store: Store[dict[str, Any]]) -> dict[str, Any] | None:
        try:
            stored = await store.async_load()
        except Exception:  # noqa: BLE001 - a corrupt file must not block setup
            _LOGGER.warning("Could not read the saved Librus state - starting fresh", exc_info=True)
            return None
        return stored if isinstance(stored, dict) else None

    def _restore_session(self, session: Any) -> None:
        """Re-import the session cookies saved after the last login or token
        refresh when they are newer than the ones in the config entry (which
        is rewritten only when the set of cookies changes - see
        remember_session)."""
        if not isinstance(session, dict) or not isinstance(session.get("cookies"), list):
            return
        try:
            logged_in_at = float(session.get("logged_in_at") or 0)
            entry_logged_in_at = float(
                (self.config_entry.data if self.config_entry else {}).get(
                    CONF_SESSION_LOGGED_IN_AT
                )
                or 0
            )
        except (TypeError, ValueError):
            return
        self._session_state = session
        if logged_in_at >= entry_logged_in_at:
            self._client.import_session(
                LibrusSessionData(cookies=session["cookies"], logged_in_at=logged_in_at)
            )

    def remember_session(self, session_data: LibrusSessionData) -> None:
        """The client logged in or refreshed its token: keep the cookies in
        the tracked state (written soon), and in the config entry only when
        the set of cookies changed - their names, domains and paths, and the
        long-lived DeviceCookie's value (the cookie that keeps Librus from
        asking for a captcha; the entry keeps it even if the state file is
        lost). A token refresh every ~2 h used to rewrite core.config_entries
        each time."""
        self._session_state = {
            "cookies": list(session_data.cookies),
            "logged_in_at": session_data.logged_in_at,
        }
        entry = self.config_entry
        if entry is None:
            return
        if _cookie_identity(entry.data.get(CONF_COOKIES)) != _cookie_identity(
            session_data.cookies
        ):
            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_COOKIES: session_data.cookies,
                    CONF_SESSION_LOGGED_IN_AT: session_data.logged_in_at,
                },
            )
        self._schedule_tracked_save()

    def _restore_kindergarten(self, saved: Any) -> None:
        """The kindergarten child's LID, group and how it was found - kept so
        a restart doesn't run the discovery (several requests) again."""
        if not isinstance(saved, dict) or not isinstance(lid := saved.get("lid"), str):
            return
        self._kindergarten_lid = lid
        group = saved.get("group_id")
        self._kindergarten_group_id = group if isinstance(group, str) and group else None
        source = saved.get("source")
        self._kindergarten_source = source if isinstance(source, str) else None
        # Saved since 0.12.5: when it was found and since when it has had no
        # lessons. A LID saved without the date counts from today.
        found = saved.get("found_on")
        try:
            self._kindergarten_found_on = (
                date.fromisoformat(found) if isinstance(found, str) else dt_util.now().date()
            )
        except ValueError:
            self._kindergarten_found_on = dt_util.now().date()
        def when(key: str) -> datetime | None:
            value = saved.get(key)
            return dt_util.parse_datetime(value) if isinstance(value, str) else None

        self._kindergarten_empty_since = when("empty_since")
        # Not in saves of 0.12.5-beta.10 or older (None then).
        self._kindergarten_refused_since = when("refused_since")
        self._kindergarten_confirmed_at = when("confirmed_at")

    def _restore_throttles(self, saved: Any, trust_until: datetime | None = None) -> None:
        """When each throttled request was last made, with what it brought
        (the lucky number, mailbox lists, the archive), and the reference
        lookups from the saved responses - so the first cycle after a
        restart asks only for what is due, like any other cycle.

        `trust_until` is when the saved responses were written, when that
        file may lack a change (see async_restore_state); None: every fetch
        time is trusted (the file holds every change, or the times were in
        the same file). A fetch time after it - or any, when that time is
        unknown - belongs to a response the file doesn't hold, so it is
        dropped and that request made again; the mailbox lists, the lucky
        number and the archive are saved with their own times and stay
        trusted."""
        if not isinstance(saved, dict):
            return

        def when(value: Any) -> datetime | None:
            return dt_util.parse_datetime(value) if isinstance(value, str) else None

        def times(value: Any) -> dict[str, datetime]:
            if not isinstance(value, dict):
                return {}
            return {str(k): t for k, v in value.items() if (t := when(v)) is not None}

        def trusted(moment: datetime | None) -> bool:
            return moment is not None and (trust_until is None or moment <= trust_until)

        self._fetched_at.update(
            {k: t for k, t in times(saved.get("fetched_at")).items() if trusted(t)}
        )
        lucky = saved.get("lucky_number")
        if isinstance(lucky, dict) and isinstance(lucky.get("number"), int):
            self._cached_lucky_number = LuckyNumberData(day=lucky.get("day"), number=lucky["number"])
            self._lucky_number_fetched_at = when(saved.get("lucky_number_fetched_at"))
        if isinstance(missing := saved.get("missing_mailboxes"), list):
            self.missing_mailboxes = {str(box) for box in missing}
            self._mailbox_probed_at = times(saved.get("mailbox_probed_at"))
        if isinstance(archive := saved.get("archive"), list):
            self._archived_messages = _messages_from_saved(archive)
            self._archive_fetched_at = when(saved.get("archive_fetched_at"))
        if isinstance(lists := saved.get("mailbox_lists"), dict):
            counts = saved.get("mailbox_counts") or {}
            fetched = times(saved.get("mailbox_fetched_at"))
            for box, items in lists.items():
                if isinstance(items, list) and box in fetched:
                    self._mailbox_lists[box] = _messages_from_saved(items)
                    self._mailbox_counts[box] = counts.get(box)
                    self._mailbox_fetched_at[box] = fetched[box]
        self._receipts_fetched_at = times(saved.get("receipts_fetched_at"))
        reference_at = when(saved.get("reference_data_fetched_at"))
        if reference_at is not None and all(
            label in self._last_good for label in _RESTORABLE_REFERENCE_LABELS
        ):
            # The lookups from their saved responses; refetched once the
            # day is over, as without a restart - or in the first cycle when
            # the saved responses are older than that fetch.
            self._apply_reference_payloads(
                {
                    label: self._last_good[label]
                    for label in REFERENCE_DATA_ENDPOINT_LABELS
                    if label in self._last_good
                }
            )
            if self._kindergarten_lid is not None:
                self._apply_kindergarten_payloads(
                    self._last_good.get("Kindergarten/ActivityTypes") or {},
                    self._last_good.get("Kindergarten/Classrooms") or {},
                    self._last_good.get("Kindergarten/Group") or {},
                    self._last_good.get("Teachers") or {},
                )
            if trusted(reference_at):
                self._reference_data_fetched_at = reference_at

    async def async_close_state(self) -> None:
        """The entry is unloading: write the state one last time and stop
        scheduling writes. A to-do tick or a calendar fetch still finishing
        after this could otherwise schedule a delayed write of this old
        coordinator's state over the file the reloaded entry is already
        using. A failed write (a full disk) is logged, not raised - it must
        not fail the unload.

        Also stops what would outlive the entry: on-demand timetable weeks
        still being fetched (cancelled before the client's session is
        closed - they'd fail with "Session is closed" otherwise) and the
        school-day clock's timer."""
        self._closed = True
        await self._async_cancel_week_fetches()
        if self.school_day_clock is not None:
            self.school_day_clock.async_stop()
            self.school_day_clock = None
        for store, build in (
            (self._state_store, self._tracked_to_save),
            (self._payload_store, self._payloads_to_save),
        ):
            try:
                await store.async_save(build())
            except Exception:  # noqa: BLE001 - see the docstring
                _LOGGER.warning("Could not save the Librus state on unload", exc_info=True)

    def _maybe_schedule_save(self) -> None:
        """Schedule delayed writes of the saved state after a successful
        cycle - each part only when something in it changed:

        - the tracked state (small) when `_tracked_view` differs from what
          was last handed to the store, or the last write is
          STATE_SAVE_MAX_INTERVAL old (so `last_success_at` and the
          throttle timestamps stay fresh);
        - the payloads (every last good response, the big part) when one of
          them changed, at most every _PAYLOADS_SAVE_INTERVAL - they are only
          the fallback while Librus is down, and writing the whole file
          after every changed response was most of what this integration
          wrote to disk. The write is handed to the store as a delayed one,
          so Home Assistant stopping writes it too (the store's final
          write): entries aren't unloaded on a stop, and the tracked state's
          newer fetch times next to older saved responses made those
          responses pass as fresh after a restart."""
        if self._closed:
            return
        now = dt_util.utcnow()
        if (
            self._saved_view is None
            or self._state_saved_at is None
            or now - self._state_saved_at >= STATE_SAVE_MAX_INTERVAL
            or self._tracked_view() != self._saved_view
        ):
            self._schedule_tracked_save()
        if self._state_dirty:
            self._schedule_payload_save()

    def _schedule_save(self) -> None:
        """Something outside a poll changed the tracked state (a to-do tick,
        a session refresh): write it soon."""
        self._schedule_tracked_save()

    def _schedule_tracked_save(self) -> None:
        """Hand the tracked state to its store's delayed write. The write
        calls `_tracked_to_save` when it happens, so a change made after this
        is written too (and compares as changed next cycle - a harmless
        extra write at worst). Nothing after unload (async_close_state)."""
        if self._closed:
            return
        self._saved_view = copy.deepcopy(self._tracked_view())
        self._state_saved_at = dt_util.utcnow()
        self._state_store.async_delay_save(self._tracked_to_save, STATE_SAVE_DELAY)

    def _schedule_payload_save(self) -> None:
        """Hand the payloads to their store's delayed write - once: while one
        is pending, the responses changed since are written with it (the
        store builds the data when it writes). Due _PAYLOADS_SAVE_INTERVAL
        after the last write (STATE_SAVE_DELAY when there was none)."""
        if self._closed or self._payload_save_pending:
            return
        delay = float(STATE_SAVE_DELAY)
        if self._payloads_saved_at is not None:
            due = self._payloads_saved_at + _PAYLOADS_SAVE_INTERVAL - dt_util.utcnow()
            delay = max(delay, due.total_seconds())
        self._payload_save_pending = True
        self._payload_store.async_delay_save(self._write_payloads, delay)

    def _write_payloads(self) -> dict[str, Any]:
        """The store writes the payloads (the delay passed, or Home Assistant
        is stopping)."""
        self._payload_save_pending = False
        self._state_dirty = False
        self._payloads_saved_at = dt_util.utcnow()
        return self._payloads_to_save()

    def _tracked_view(self) -> tuple[Any, ...]:
        """What decides whether the tracked state needs writing - compared
        with `==` against a copy taken at the last write. Live references
        (no copying, no JSON) - a JSON dump of the whole tracked state every
        cycle was the old check. The seen ids count as `_seen_version`;
        throttle timestamps are left out on purpose (they move every hour
        and only need the periodic write)."""
        return (
            self._seen_version,
            self._unseeded_kinds,
            self._known_agenda,
            self._known_justifications,
            self._known_items,
            self._known_homework_assignment_ids,
            self._achievement_dates,
            self._achievement_year,
            self._legacy_achievements,
            self._achievement_seed_pending,
            self.read_receipts,
            self.homework_done_ever,
            self._known_forecast,
            self._known_forecast_basis,
            self.student_number_from_librus,
            self._session_state,
            (
                self._kindergarten_lid,
                self._kindergarten_group_id,
                self._kindergarten_source,
                self._kindergarten_found_on,
                self._kindergarten_empty_since,
                self._kindergarten_refused_since,
                self._kindergarten_confirmed_at,
            ),
            self.average_digests,
            self._reference_data_fetched_at,
            self._cached_lucky_number,
            self.missing_mailboxes,
            self._archived_messages,
            # Written promptly (not with the periodic write): a restart
            # compares it with the payloads file's `saved_at`.
            self._payloads_changed_at,
        )

    def _payloads_changed(self) -> None:
        """A stored response or polled week really changed: the payloads
        file needs writing, the next poll rebuilds the data, and the change
        is dated (`_payloads_changed_at`, see async_restore_state)."""
        self._state_dirty = True
        self._input_version += 1
        self._payloads_changed_at = dt_util.utcnow()

    def _remember_payload(self, label: str, payload: Any) -> None:
        """Keep a section's last good response; a different one than before
        means the saved payloads need writing."""
        if self._last_good.get(label) != payload:
            self._payloads_changed()
        self._last_good[label] = payload

    def _tracked_to_save(self) -> dict[str, Any]:
        changed = self._payloads_changed_at
        return {
            **self._tracked_state(),
            "throttles": self._throttles_to_save(),
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "payloads_changed_at": changed.isoformat() if changed else None,
        }

    def _payloads_to_save(self) -> dict[str, Any]:
        """The responses, with when they were taken (see async_restore_state
        - a fetch time after it isn't trusted)."""
        return {
            "payloads": self._last_good,
            "timetable": self._timetable_cache,
            "saved_at": dt_util.utcnow().isoformat(),
        }

    def _throttles_to_save(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        def isos(values: dict[str, datetime]) -> dict[str, str]:
            return {key: value.isoformat() for key, value in values.items()}

        lucky = self._cached_lucky_number
        return {
            "reference_data_fetched_at": iso(self._reference_data_fetched_at),
            "fetched_at": isos(self._fetched_at),
            "lucky_number": {"day": lucky.day, "number": lucky.number} if lucky else None,
            "lucky_number_fetched_at": iso(self._lucky_number_fetched_at),
            "missing_mailboxes": sorted(self.missing_mailboxes),
            "mailbox_probed_at": isos(self._mailbox_probed_at),
            "archive": [dataclasses.asdict(m) for m in self._archived_messages],
            "archive_fetched_at": iso(self._archive_fetched_at),
            "mailbox_lists": {
                box: [dataclasses.asdict(m) for m in items]
                for box, items in self._mailbox_lists.items()
            },
            "mailbox_counts": dict(self._mailbox_counts),
            "mailbox_fetched_at": isos(self._mailbox_fetched_at),
            "receipts_fetched_at": isos(self._receipts_fetched_at),
        }

    def _tracked_state(self) -> dict[str, Any]:
        """Everything saved except the stored responses, the throttles and
        `last_success_at`."""
        tracker = self._change_tracker
        return {
            "seen": tracker.seen.to_dict() if tracker.is_seeded else None,
            "unseeded_kinds": sorted(self._unseeded_kinds),
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
            "achievement_dates": self._achievement_dates,
            "achievement_year": self._achievement_year,
            # Legacy keys not merged yet (read back by async_restore_state).
            "achievements": list(self._legacy_achievements) or None,
            "achievement_seed_pending": self._achievement_seed_pending,
            "read_receipts": self.read_receipts,
            "homework_done_ever": sorted(self.homework_done_ever),
            "forecast": (
                {
                    "basis": self._known_forecast_basis,
                    "values": {str(k): v for k, v in self._known_forecast.items()},
                }
                if self._known_forecast is not None
                else None
            ),
            "student_number": self.student_number_from_librus,
            "session": self._session_state,
            "kindergarten": (
                {
                    "lid": self._kindergarten_lid,
                    "group_id": self._kindergarten_group_id,
                    "source": self._kindergarten_source,
                    "found_on": (
                        self._kindergarten_found_on.isoformat()
                        if self._kindergarten_found_on
                        else None
                    ),
                    "empty_since": (
                        self._kindergarten_empty_since.isoformat()
                        if self._kindergarten_empty_since
                        else None
                    ),
                    "refused_since": (
                        self._kindergarten_refused_since.isoformat()
                        if self._kindergarten_refused_since
                        else None
                    ),
                    "confirmed_at": (
                        self._kindergarten_confirmed_at.isoformat()
                        if self._kindergarten_confirmed_at
                        else None
                    ),
                }
                if self._kindergarten_lid is not None
                else None
            ),
            "average_digests": self.average_digests,
        }

    def set_average_digest(self, statistic_id: str, digest: str | None) -> None:
        """The grade-average statistics digest of one series (see
        average_history.py) - saved, so a restart doesn't rewrite a whole
        school year of rows the recorder already has."""
        if digest is None:
            self.average_digests.pop(statistic_id, None)
        else:
            self.average_digests[statistic_id] = digest

    @callback
    def async_start_midnight_tick(self) -> CALLBACK_TYPE:
        """One tick a few seconds after local midnight that asks every entity
        to look again (`async_update_listeners`) - "days until", today/
        tomorrow and the streaks roll over at midnight, not with the next
        poll (which smart polling or quiet hours can put hours later).
        Returns the unsubscribe callback."""

        @callback
        def _tick(_now: datetime) -> None:
            if self.data is not None:
                self.async_update_listeners()

        return async_track_time_change(self.hass, _tick, hour=0, minute=0, second=5)

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
    def memo_owner(self) -> str | None:
        """Owner key for the shared calculation caches (forecast.DataMemo):
        the config entry id - not the coordinator itself, which the cache
        would then keep alive after an unload."""
        return self.config_entry.entry_id if self.config_entry is not None else None

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
            "empty_since": (
                self._kindergarten_empty_since.isoformat()
                if self._kindergarten_empty_since
                else None
            ),
            "refused_since": (
                self._kindergarten_refused_since.isoformat()
                if self._kindergarten_refused_since
                else None
            ),
            "next_discovery": (
                self._kindergarten_next_discovery.isoformat()
                if self._kindergarten_next_discovery
                else None
            ),
            # Week start -> entries by type (field shapes, no names or ids).
            "entries_by_week": dict(sorted(self._kindergarten_entry_summary.items())),
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

    async def _fetch_timetable_or_unpublished(
        self, week_start: date, *, track: bool = True
    ) -> dict[str, Any]:
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
        same "Timetable" label.

        `track=False` is for the on-demand weeks (a dashboard browsing
        another month, the Assist timetable tool): their answer says nothing
        about the polled weeks' health, so it leaves the degraded-endpoint
        tracking, the fallback sections and `_timetable_forbidden` (the
        kindergarten discovery trigger) alone - a far week the school hasn't
        published yet used to flag the whole timetable as degraded, and a
        published one cleared a real problem with the polled weeks. A
        transient failure is raised to the caller, which keeps its own copy
        (see `async_get_timetable_week`).

        A polled kindergarten week also records how it went
        (`_kindergarten_week_ok`): refused or failed weeks don't count as
        empty ones (see `_kindergarten_lid_stale`)."""
        kindergarten = self._kindergarten_lid is not None and track
        try:
            if self._kindergarten_lid is not None:
                payload = await self._client.async_get_kindergarten_timetable(
                    self._kindergarten_lid, week_start, week_start + timedelta(days=6)
                )
            else:
                payload = await self._client.async_get_timetable(week_start)
        except LibrusSessionExpiredError as err:
            if err.status_code == 403:
                if not track:
                    return {}
                self._note_optional_endpoint_failure("Timetable")
                if kindergarten:
                    self._kindergarten_week_ok[week_start] = False
                elif self._kindergarten_lid is None:
                    self._timetable_forbidden = True
                return {}
            raise
        except LibrusError:
            # A transient failure (timeout, 5xx, garbled response): the last
            # good copy of that week beats failing the whole cycle.
            cached = self._timetable_cache.get(week_start.isoformat()) if track else None
            if cached is None:
                raise
            _LOGGER.debug("Timetable %s fetch failed - using the saved copy", week_start)
            self._note_optional_endpoint_failure("Timetable")
            self.fallback_sections.add("Timetable")
            if kindergarten:
                self._kindergarten_week_ok[week_start] = False
            return cached
        if not track:
            return payload
        if kindergarten:
            self._kindergarten_week_ok[week_start] = True
            summaries = self._kindergarten_entry_summary
            summaries[week_start.isoformat()] = summarize_kindergarten_entries(
                payload.get("timetableEntries")
            )
            for stale in sorted(summaries)[:-2]:
                del summaries[stale]
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
        key = week_start.isoformat()
        if self._timetable_cache.get(key) != payload:
            self._payloads_changed()
        self._timetable_cache[key] = payload
        for old in [k for k in self._timetable_cache if k < this_week.isoformat()]:
            # A past week dropped: written with the next save, not dated - a
            # file still holding it vouches for every fetch time all the same.
            del self._timetable_cache[old]
            self._state_dirty = True

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
        dropped = self._kindergarten_dropped
        if not await self._async_discover_kindergarten(me_payload):
            _LOGGER.debug("Timetables is forbidden and no kindergarten timetable was found")
            return False
        if dropped is not None and dropped["lid"] == self._kindergarten_lid:
            # The LID just dropped, found again (a break: the past 30 days had
            # lessons). Not a new account: no INFO log, and the lookups stay
            # as they were - unless the ordinary ones were fetched since it
            # was dropped (they replaced the merged kindergarten ones).
            _LOGGER.debug(
                "The same kindergarten LID was found again (via %s)", self._kindergarten_source
            )
            if self._reference_data_fetched_at is None:
                self._reference_data_fetched_at = dropped["reference_at"]
            else:
                self._reference_data_fetched_at = None
            return True
        _LOGGER.info(
            "Kindergarten account detected - using the kindergarten timetable API (via %s)",
            self._kindergarten_source,
        )
        # Pick up activity names/classrooms/group in this same cycle rather
        # than up to 24h later.
        self._reference_data_fetched_at = None
        return True

    def _kindergarten_lid_stale(self, this_week: Any, next_week: Any, today: date) -> bool:
        """Whether the saved kindergarten LID should be dropped this cycle:

        - Librus has refused (403) or failed to give both polled weeks for
          _KINDERGARTEN_REFUSED_RESET (a LID that stopped working);
        - both weeks have answered without a lesson for
          _KINDERGARTEN_EMPTY_RESET (counted from the later of
          `_kindergarten_empty_since` and the last time the discovery found
          the same LID again) - long enough for a school break;
        - a new school year started since it was found.

        Keeps `_kindergarten_empty_since` / `_kindergarten_refused_since` up
        to date from this cycle's weeks (`_kindergarten_week_ok`)."""
        status = self._kindergarten_week_ok
        self._kindergarten_week_ok = {}
        if self._kindergarten_lid is None:
            return False
        now = dt_util.utcnow()
        week_start = _week_start(today)
        refused = all(
            status.get(week) is False for week in (week_start, week_start + timedelta(days=7))
        )
        if refused:
            # Says nothing about lessons: the empty clock neither starts nor
            # stops.
            if self._kindergarten_refused_since is None:
                self._kindergarten_refused_since = now
        else:
            self._kindergarten_refused_since = None
            if _has_lessons(this_week) or _has_lessons(next_week):
                self._kindergarten_empty_since = None
                self._kindergarten_confirmed_at = None
            elif self._kindergarten_empty_since is None:
                self._kindergarten_empty_since = now
        refused_since = self._kindergarten_refused_since
        empty_from = self._kindergarten_empty_since
        if empty_from is not None and self._kindergarten_confirmed_at is not None:
            empty_from = max(empty_from, self._kindergarten_confirmed_at)
        found_on = self._kindergarten_found_on
        return (
            (refused_since is not None and now - refused_since >= _KINDERGARTEN_REFUSED_RESET)
            or (empty_from is not None and now - empty_from >= _KINDERGARTEN_EMPTY_RESET)
            or (found_on is not None and _school_year(found_on) != _school_year(today))
        )

    def _forget_kindergarten(self) -> None:
        """Back to the ordinary Timetables: the next 403 there runs the
        discovery again right away (a new school year, a child who moved on
        to school, a LID whose timetable stopped working). What is dropped
        is remembered (`_kindergarten_dropped`), for the discovery finding
        the same LID again."""
        _LOGGER.info(
            "The kindergarten timetable has been refused for a day, had no "
            "lessons for three weeks, or a new school year started - asking "
            "the ordinary timetable again"
        )
        self._kindergarten_dropped = {
            "lid": self._kindergarten_lid,
            "empty_since": self._kindergarten_empty_since,
            "reference_at": self._reference_data_fetched_at,
        }
        self._kindergarten_lid = None
        self._kindergarten_group_id = None
        self._kindergarten_source = None
        self._kindergarten_found_on = None
        self._kindergarten_empty_since = None
        self._kindergarten_refused_since = None
        self._kindergarten_confirmed_at = None
        self._kindergarten_next_discovery = None
        self._kindergarten_entry_summary = {}
        # The ordinary lookups (the kindergarten ones were merged into them).
        self._reference_data_fetched_at = None

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
        for the account's own numeric ids (`Me.Account.UserId` / `.Id` -
        the source is named after the field, never the id: diagnostics show
        it).

        The LID `_forget_kindergarten` just dropped, found again, keeps its
        empty clock running (`_kindergarten_confirmed_at` restarts the
        empty window, so a long break asks again at most every
        _KINDERGARTEN_EMPTY_RESET)."""
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
        numeric_ids: dict[int, str] = {}  # id -> the Me.Account field (first wins)
        for field in ("UserId", "Id"):
            value = account.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                numeric_ids.setdefault(value, field)
        for numeric_id, field in numeric_ids.items():
            user_record = await self._async_probe(self._client.async_get_user(numeric_id))
            add(collect_lid_user_identifiers(user_record), f"Users/{field}")

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
            dropped = self._kindergarten_dropped
            self._kindergarten_dropped = None
            self._kindergarten_lid = lid
            self._kindergarten_source = source
            self._kindergarten_found_on = today
            self._kindergarten_refused_since = None
            if dropped is not None and dropped["lid"] == lid and dropped["empty_since"]:
                self._kindergarten_empty_since = dropped["empty_since"]
                self._kindergarten_confirmed_at = dt_util.utcnow()
            else:
                self._kindergarten_empty_since = None
                self._kindergarten_confirmed_at = None
            child = await self._async_probe(self._client.async_get_kindergartener(lid))
            child_data = child.get("data")
            group_id = child_data.get("groupIdentifier") if isinstance(child_data, dict) else None
            self._kindergarten_group_id = group_id if isinstance(group_id, str) and group_id else None
            return True
        self._kindergarten_source = f"not_found ({len(candidates)} candidates)"
        return False

    async def async_fetch_timetable_week(self, week_start: date, *, track: bool = True) -> Any:
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

        `track=False` for an on-demand week (see `_fetch_timetable_or_
        unpublished`); callers outside the coordinator use
        `async_get_timetable_week`, which adds the shared cache.
        """
        assert self.config_entry is not None
        seen = self._login_count
        try:
            return await self._fetch_timetable_or_unpublished(week_start, track=track)
        except LibrusSessionExpiredError:
            if not track and self._login_count == seen and self._recent_forced_login():
                # An on-demand week (a dashboard paging months) right after
                # a forced login that didn't help: not another full password
                # login for it - the caller shows the last copy meanwhile.
                raise
            await self._async_relogin(seen)
            return await self._fetch_timetable_or_unpublished(week_start, track=track)

    def _recent_forced_login(self) -> bool:
        last = self._last_forced_login_at
        return last is not None and dt_util.utcnow() - last < _ON_DEMAND_RELOGIN_GAP

    def polled_timetable_week(self, week_start: date) -> dict[date, list[LessonData]] | None:
        """This week's or next week's lessons from the coordinator's own
        timetable (fetched on every refresh), or None when `week_start` is
        another week or the data doesn't hold it - e.g. on a Monday before
        the first refresh of the new week, when the data still covers last
        week and this one."""
        data = self.data
        if data is None:
            return None
        this_week = _week_start(dt_util.now().date())
        if week_start not in (this_week, this_week + timedelta(days=7)):
            return None
        week_end = week_start + timedelta(days=7)
        days = {day: lessons for day, lessons in data.timetable.items() if week_start <= day < week_end}
        return days or None

    async def async_get_timetable_weeks(
        self, week_starts: list[date]
    ) -> dict[date, list[LessonData]]:
        """Lessons of several weeks merged (a month view, a few days for
        Assist): the polled weeks from the data, the others from the
        on-demand cache, the missing ones fetched together - at most
        _WEEK_FETCH_CONCURRENCY at a time (`_week_fetch_limit`)."""
        weeks = await asyncio.gather(
            *(self.async_get_timetable_week(week) for week in dict.fromkeys(week_starts))
        )
        merged: dict[date, list[LessonData]] = {}
        for week in weeks:
            merged.update(week)
        return merged

    async def async_get_timetable_week(self, week_start: date) -> dict[date, list[LessonData]]:
        """One week's lessons for the timetable calendar and the Assist
        timetable tool. Never raises a Librus error: a week that can't be
        fetched is its last copy (however old), or no lessons.

        Weeks outside the polled two are kept in `_week_cache` - for
        _WEEK_CACHE_NEAR_TTL when the week is within _WEEK_CACHE_NEAR of
        today (a substitution there still matters), for _WEEK_CACHE_FAR_TTL
        further away - and shared by every caller (the calendar and Assist
        used to keep separate copies). Two requests for the same week at
        once share one fetch (`_week_fetches`): a dashboard opening a month
        view and a card asking for the same weeks no longer fetched them
        twice.

        Not fetched at all (the last copy, or no lessons): a week further
        than ON_DEMAND_WEEKS from the current one, a week whose fetch failed
        less than _WEEK_FAILURE_BACKOFF ago, any week while the polls are
        backing off after failures (Librus is down), and anything once the
        entry is unloading. The fetch runs as the config entry's background
        task, cancelled on unload."""
        polled = self.polled_timetable_week(week_start)
        if polled is not None:
            return polled
        now = dt_util.utcnow()
        cached = self._week_cache.get(week_start)
        if cached is not None and now - cached[1] < self._week_ttl(week_start):
            return cached[0]
        last_copy = cached[0] if cached is not None else {}
        this_week = _week_start(dt_util.now().date())
        failed_at = self._week_failed_at.get(week_start)
        if (
            self._closed
            or abs(week_start - this_week) > timedelta(weeks=ON_DEMAND_WEEKS)
            or (failed_at is not None and now - failed_at < _WEEK_FAILURE_BACKOFF)
            or self._in_outage_backoff()
        ):
            return last_copy
        task = self._week_fetches.get(week_start)
        # A finished task can still be listed: an eagerly started fetch ends
        # before its done-callback (which drops it) gets a turn of the loop,
        # and awaiting a finished task never yields - so a later request
        # would get the old answer instead of fetching again.
        if task is None or task.done():
            assert self.config_entry is not None
            task = self.config_entry.async_create_background_task(
                self.hass,
                self._async_fetch_cached_week(week_start),
                f"{DOMAIN} timetable {week_start}",
            )
            self._week_fetches[week_start] = task
            # Dropped once done (a done-callback, so an eagerly finished task
            # is dropped too) - the next request after it reads the cache.
            # Only this task: a newer fetch may already have replaced it.
            task.add_done_callback(
                lambda done: self._week_fetches.pop(week_start, None)
                if self._week_fetches.get(week_start) is done
                else None
            )
        # Shielded: one caller giving up (a closed dashboard) must not cancel
        # the fetch another caller is waiting for.
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            # The shared fetch was cancelled (the entry unloading), not this
            # caller: no lessons rather than a failed calendar request.
            return last_copy

    async def _async_fetch_cached_week(self, week_start: date) -> dict[date, list[LessonData]]:
        cached = self._week_cache.get(week_start)
        try:
            async with self._week_fetch_limit:
                payload = await self.async_fetch_timetable_week(week_start, track=False)
        except (LibrusError, RuntimeError) as err:
            # The forced relogin inside async_fetch_timetable_week failed
            # too (or the session was closed under it - an unload) - a
            # day-old copy beats an empty week, an empty week beats failing
            # a whole calendar request (a dashboard's 500). Not asked again
            # for a while.
            self._week_failed_at[week_start] = dt_util.utcnow()
            _LOGGER.warning(
                "Failed to fetch the timetable for the week starting %s (%s) - %s",
                week_start,
                err,
                "showing the last copy" if cached is not None else "no lessons shown",
            )
            return cached[0] if cached is not None else {}
        merged = merge_timetables(payload)
        self._week_cache[week_start] = (merged, dt_util.utcnow())
        self._week_failed_at.pop(week_start, None)
        self._prune_week_cache()
        return merged

    async def _async_cancel_week_fetches(self) -> None:
        """Cancel the on-demand week fetches in flight (unload) and wait for
        them to finish."""
        tasks = [task for task in self._week_fetches.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._week_fetches.clear()

    @staticmethod
    def _week_ttl(week_start: date) -> timedelta:
        this_week = _week_start(dt_util.now().date())
        if abs(week_start - this_week) <= _WEEK_CACHE_NEAR:
            return _WEEK_CACHE_NEAR_TTL
        return _WEEK_CACHE_FAR_TTL

    def _prune_week_cache(self) -> None:
        """A dashboard paging through months would otherwise keep every week
        it ever looked at. Past its TTL an entry is only a fallback for a
        failed refetch; that fallback is kept for weeks within
        _WEEK_CACHE_KEEP of the current one, and nothing fetched longer ago
        than that is kept at all."""
        now = dt_util.utcnow()
        this_week = _week_start(dt_util.now().date())
        for week_start, (_lessons, fetched_at) in list(self._week_cache.items()):
            age = now - fetched_at
            far = abs(week_start - this_week) > _WEEK_CACHE_KEEP
            if age > _WEEK_CACHE_KEEP or (far and age >= self._week_ttl(week_start)):
                del self._week_cache[week_start]
        for week_start, failed_at in list(self._week_failed_at.items()):
            if now - failed_at >= _WEEK_FAILURE_BACKOFF:
                del self._week_failed_at[week_start]

    async def _async_relogin(self, seen: int) -> None:
        """Force a fresh login after Librus rejected the session - one at a
        time. A dashboard asking for two timetable weeks while the poll runs
        used to log in two or three times at once; each login replaced the
        session the others had just made, so all of them failed again (seen
        live right after a restart).

        `seen` is `_login_count` as it was BEFORE the caller sent the
        rejected request. When it has changed since, another login (or token
        refresh) happened after that request went out - the rejection came
        from the old cookie and the retry can use the new session. When it
        hasn't, the current session itself was rejected and a new login is
        made, however young that session is (a time window here used to skip
        the relogin after a token refresh Librus then rejected anyway)."""
        assert self.config_entry is not None
        async with self._login_lock:
            if self._login_count != seen:
                return
            self._last_forced_login_at = dt_util.utcnow()
            await self._client.async_ensure_session_valid(
                self.config_entry.data[CONF_PASSWORD], force=True
            )
            self._login_count += 1

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
        seen = self._login_count
        try:
            payload = await self._client.async_get_message(mailbox, message_id)
        except LibrusSessionExpiredError:
            await self._async_relogin(seen)
            self._messages_bootstrapped = False
            self._messages_available = await self._client.async_bootstrap_messages()
            self._messages_bootstrapped = True
            payload = await self._client.async_get_message(mailbox, message_id)
        # Opening a message marks it read, so this mailbox's cached list now
        # shows it unread. The unread count falls too and would trigger a
        # refetch anyway, but not when another message arrived meanwhile -
        # drop the copy so the next cycle asks for the list again.
        self._forget_mailbox_list(mailbox)
        return payload

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
        self.fallback_sections = set()
        self._failed_this_cycle = set()
        try:
            # Nothing changed (most polls): the old object comes back (see
            # `_async_fetch_live`). The entities see the same data object
            # and skip writing their state (SkipUnchangedUpdates), and every
            # calculation cached per data object (forecast.DataMemo) stays
            # valid.
            data = await self._async_fetch_live()
        except UpdateFailed as err:
            return self._handle_failed_cycle(err)
        self.last_success_at = self._last_fetch_at = dt_util.utcnow()
        self.last_error = None
        self.failures = 0
        self.next_attempt_at = None
        self.data_source = "live"
        self._maybe_schedule_save()
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
        self.last_attempt_at = now
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
            async with self._login_lock:
                age = self._client.session_age_seconds
                await self._client.async_ensure_session_valid(
                    self.config_entry.data[CONF_PASSWORD]
                )
                new_age = self._client.session_age_seconds
                if isinstance(new_age, (int, float)) and (
                    not isinstance(age, (int, float)) or new_age < age
                ):
                    # It logged in (or refreshed): a request still holding
                    # the old cookie must not log in over this session.
                    self._login_count += 1
        except LibrusAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except LibrusError as err:
            raise UpdateFailed(str(err)) from err

        today = dt_util.now().date()
        week_start = today - timedelta(days=today.weekday())
        next_week_start = week_start + timedelta(days=7)

        seen = self._login_count
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
                await self._async_relogin(seen)
                core_payloads = await self._async_fetch_core_payloads(week_start, next_week_start)
            except LibrusSessionExpiredError as err:
                # The login itself worked (the password is fine) but Librus
                # still rejected a request - not something to ask the user
                # about; the next cycle tries again.
                raise UpdateFailed(str(err)) from err
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
        self._remember_payload("Me", me_payload)
        timetable_this_week = core_payloads[_TIMETABLE_THIS_WEEK]
        timetable_next_week = core_payloads[_TIMETABLE_NEXT_WEEK]

        if self._kindergarten_lid_stale(timetable_this_week, timetable_next_week, today):
            # The ordinary Timetables, once: a 403 there makes the discovery
            # below look for a (new) kindergarten LID straight away.
            self._forget_kindergarten()
            try:
                timetable_this_week, timetable_next_week = await asyncio.gather(
                    self._fetch_timetable_or_unpublished(week_start),
                    self._fetch_timetable_or_unpublished(next_week_start),
                )
            except LibrusError as err:
                _LOGGER.debug("Timetable fetch after dropping the kindergarten LID failed: %s", err)

        if await self._async_maybe_discover_kindergarten(me_payload):
            try:
                timetable_this_week, timetable_next_week = await asyncio.gather(
                    self._fetch_timetable_or_unpublished(week_start),
                    self._fetch_timetable_or_unpublished(next_week_start),
                )
            except LibrusError as err:
                # Just found - the next cycle fetches it normally.
                _LOGGER.debug("Kindergarten timetable fetch failed right after discovery: %s", err)

        # Everything after the core fetch, concurrently (O7, review round 3)
        # - it used to be one `await` after another, a dozen round trips in
        # a row. Every Synergia request among them waits for the shared
        # `_request_limit` (`_limited`), so the burst stays at
        # _REFERENCE_CONCURRENCY requests at once. Dependencies stay chained: the missing-lookup check and
        # the point grades (Units says whether the school has them) follow
        # the reference data.
        #
        # BUG FIX (live feedback, 2026-09-23): messages live on another host
        # (wiadomosci.librus.pl) and run as their own chain, sequential
        # inside, outside the Synergia limit - bundling their bootstrap into
        # the reference-data burst was a live-identified suspect for that
        # session dying far more often.

        async def reference_chain() -> list[PointGradeData]:
            refreshed = await self._async_refresh_reference_data()
            await self._async_resolve_missing_lookups(core_payloads, refreshed)
            return await self._async_get_point_grades()

        had_text_grades = bool((self._last_good.get("BaseTextGrades") or {}).get("Grades"))
        (
            lucky_number,
            point_grades,
            messages_result,
            justifications,
            *extra_results,
        ) = await asyncio.gather(
            self._async_get_lucky_number(today),
            reference_chain(),
            self._async_get_messages(),
            self._async_get_justifications(),
            # Text grades are rare: hourly while the last answer had none.
            self._async_optional(
                "BaseTextGrades",
                self._client.async_get_base_text_grades,
                every=None if had_text_grades else _SPARSE_REFRESH,
            ),
            self._async_optional(
                "Realizations", self._client.async_get_realizations, every=_HOURLY
            ),
            self._async_optional(
                "SchoolTrips", self._client.async_get_school_trips, every=_HOURLY
            ),
            self._async_optional(
                "SchoolFiles", self._client.async_get_school_files, every=_HOURLY
            ),
            self._async_optional(
                "TimetableEntries", self._client.async_get_timetable_entries, every=_DAILY
            ),
            self._async_get_descriptive_grade_lookups(core_payloads),
            self._async_get_partial_grades(),
        )
        (
            text_grades_payload,
            realizations,
            school_trips,
            school_files,
            timetable_entries,
            descriptive_lookups,
            partial_grades,
        ) = extra_results
        extras = {
            "BaseTextGrades": text_grades_payload,
            "Realizations": realizations,
            "SchoolTrips": school_trips,
            "SchoolFiles": school_files,
            "TimetableEntries": timetable_entries,
            **descriptive_lookups,
            **partial_grades,
        }
        payloads = list(core_payloads)
        payloads[_TIMETABLE_THIS_WEEK] = timetable_this_week
        payloads[_TIMETABLE_NEXT_WEEK] = timetable_next_week
        core_payloads = tuple(payloads)
        signature = self._build_signature(
            timetable_this_week, timetable_next_week, lucky_number, messages_result,
            point_grades, justifications, extras,
        )
        previous = self.data
        if (
            previous is not None
            and previous is self._built_data
            and self._built_signature is not None
            and _same_signature(signature, self._built_signature)
        ):
            # Nothing that goes into the data changed (most polls): the last
            # data object as it is, without parsing every response again -
            # before the events, so they (and the forecast/badge memos) work
            # on the object that is kept.
            data = previous
        else:
            data = self._build_data(
                core_payloads, lucky_number, messages_result, point_grades, justifications, extras
            )
            if previous is not None and data == previous:
                data = previous
        self._built_signature, self._built_data = signature, data
        self._fire_change_events(self._update_change_tracker(data, today), data)
        self._fire_new_homework_assignment_events(data)
        self._fire_agenda_change_events(data, today)
        self._fire_justification_events(data)
        self._fire_extra_item_events(data)
        self._check_achievements(data, today)
        self._fire_forecast_events(data, today)
        return data

    def _build_signature(
        self,
        timetable_this_week: Any,
        timetable_next_week: Any,
        lucky_number: LuckyNumberData | None,
        messages_result: tuple[Any, ...],
        point_grades: list[PointGradeData],
        justifications: list[JustificationData],
        extras: dict[str, Any],
    ) -> tuple[Any, ...]:
        """Everything `_build_data` reads, cheaply: the stored responses by
        `_input_version` (bumped by `_remember_payload` and
        `_remember_timetable_week` whenever one changes), the lookups by
        identity (re-parsing makes new objects), and by value what doesn't
        go through either - the two timetable weeks (a refused week is `{}`
        without being stored), the lucky number, the messages, the point
        grades, the justifications, which extras came back, and whether
        Wiadomości is available. Compared with `_same_signature`."""
        identity = (
            self._cached_subjects,
            self._cached_teachers,
            self._cached_classrooms,
            self._cached_lesson_subjects,
            self._cached_school,
            self._cached_class,
            self._cached_free_days,
            self._cached_homework_categories,
            self._cached_note_categories,
            self._cached_behaviour_grade_categories,
            self._cached_grade_categories,
            self._cached_attendance_types,
            self._cached_grading_system,
            self._cached_text_grade_categories,
            self._cached_homework_assignment_categories,
        )
        values = (
            self._input_version,
            timetable_this_week,
            timetable_next_week,
            lucky_number,
            messages_result,
            point_grades,
            justifications,
            tuple(sorted(extras)),
            self._messages_available or "Messages" in self.fallback_sections,
        )
        return identity, values

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
        together with the cached reference lookups (grade categories and
        attendance types among them)."""
        (
            me_payload,
            grades_payload,
            notes_payload,
            attendances_payload,
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
            sent_messages,
            archived_messages,
        ) = messages_result

        me = parse_me(me_payload)
        grades = parse_grades(grades_payload, parse_comment_text_map(grade_comments_payload))
        school_notices = parse_school_notices(notices_payload)
        notes = parse_notes(notes_payload)
        homeworks = parse_homeworks(homeworks_payload)
        attendances = parse_attendances(attendances_payload)
        attendance_types = self._cached_attendance_types
        timetable = merge_timetables(timetable_this_week, timetable_next_week)
        grade_categories = self._cached_grade_categories
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
            # Also while the last messages stand in for a failed (or, for a
            # few cycles, refused) fetch - they are shown, not "unavailable".
            messages_available=self._messages_available or "Messages" in self.fallback_sections,
            unread_message_count=unread_count,
            unread_messages_by_mailbox=unread_by_mailbox,
            messages=messages,
            substitution_messages=substitution_messages,
            alert_messages=alert_messages,
            justification_messages=justification_messages,
            sent_messages=sent_messages,
            archived_messages=archived_messages,
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
            descriptive_grades=parse_descriptive_grades(
                descriptive_grades_payload,
                parse_descriptive_skills((extras or {}).get("DescriptiveGrades/Skills") or {}),
                parse_comment_text_map((extras or {}).get("DescriptiveGrades/Comments") or {}),
            )
            + parse_partial_grades(
                (extras or {}).get("PartialGrades") or {},
                parse_auth_subjects((extras or {}).get("Auth/Subjects") or {}),
            ),
            grading_system=self._cached_grading_system,
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
            standing_timetable=parse_timetable_entries(
                (extras or {}).get("TimetableEntries") or {}, self._cached_lesson_subjects
            ),
            homework_assignment_categories=self._cached_homework_assignment_categories,
            parent_teacher_conferences=parse_parent_teacher_conferences(
                parent_teacher_conferences_payload
            ),
        )

    @property
    def achievements(self) -> list[dict[str, str]]:
        """Every badge key earned so far with its title and date, oldest
        first (kept across restarts) - the Rank sensor's `achievements`
        attribute."""
        dates = self._achievement_dates or {}
        return [
            {"key": key, "title": key_title(key), "date": dates[key]}
            for key in sorted(dates, key=lambda k: (dates[k], k))
        ]

    @property
    def homework_assignments_complete(self) -> bool:
        """Whether the data's homework list is this cycle's real answer -
        not a saved copy standing in for a failed fetch, nor data rebuilt
        from the saved responses at start (the to-do list prunes its ticks
        against it)."""
        return (
            self.data is not None
            and self.data_source == "live"
            and "HomeWorkAssignments" not in self._failed_this_cycle
        )

    def record_homework_done(self, uid: str) -> None:
        """The homework to-do reports a tick (for the homework badge)."""
        if uid not in self.homework_done_ever:
            self.homework_done_ever.add(uid)
            self._schedule_save()

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
        semester starts), so neither looks like a jump. Skipped for the data
        object and day it was last worked out for (an unchanged poll keeps
        the data object - nothing can have moved)."""
        checked = (data, today)
        if self._forecast_checked is not None and (
            self._forecast_checked[0] is data and self._forecast_checked[1] == today
        ):
            return
        basis, _semester = forecast_basis(data, today)
        forecasts = subject_forecasts(
            data,
            today,
            self.grade_thresholds,
            weighted=self.weighted_average,
            owner=self.memo_owner,
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
        self._forecast_checked = checked

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
        if now.date() not in school_days(self.data, self.memo_owner):
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

        gathered_labels = [
            label for label in OPTIONAL_ENDPOINT_LABELS if label not in _COMMENT_SOURCES
        ]
        had_descriptive = bool((self._last_good.get("DescriptiveGrades") or {}).get("Grades"))
        sparse_fetched: dict[str, datetime] = {}
        # Both tiers at once (they don't depend on each other - only the
        # comment lookups below need the grades): one round trip less per
        # cycle. Each tier keeps its own `return_exceptions=True`.
        core_results, optional_results = await asyncio.gather(
            asyncio.gather(
                self._client.async_get_grades(),
                self._client.async_get_notes(),
                self._client.async_get_attendances(),
                self._fetch_timetable_or_unpublished(week_start),
                self._fetch_timetable_or_unpublished(next_week_start),
                self._client.async_get_homeworks(),
                self._maybe(announcements_enabled, self._client.async_get_school_notices),
                return_exceptions=True,
            ),
            asyncio.gather(
                self._client.async_get_homework_assignments(),
                # A behaviour grade comes once a month or a semester: hourly.
                self._sparse(
                    "BehaviourGrades/Points",
                    behaviour_grades_enabled,
                    self._client.async_get_behaviour_grade_points,
                    _SPARSE_REFRESH,
                    sparse_fetched,
                ),
                # Most students never have descriptive grades: hourly while
                # the last answer had none, every cycle once there are some.
                self._sparse(
                    "DescriptiveGrades",
                    descriptive_grades_enabled,
                    self._client.async_get_descriptive_grades,
                    None if had_descriptive else _SPARSE_REFRESH,
                    sparse_fetched,
                ),
                self._client.async_get_parent_teacher_conferences(),
                return_exceptions=True,
            ),
        )
        # A genuine 401 anywhere in tier 1 means the session actually
        # died - propagate it immediately (before degrading any 403s)
        # so the existing forced-relogin-and-retry-once recovery still
        # runs exactly as before. A dead session can plausibly 403
        # unrelated endpoints too in the same broken cycle, so it's not
        # safe to interpret THOSE as "confirmed module-unavailable" once
        # a real 401 has shown up anywhere in the same batch.
        for result in core_results:
            if isinstance(result, LibrusSessionExpiredError) and result.status_code == 401:
                raise result
        # Only now: a retry after the relogin above must not skip what this
        # attempt fetched and then threw away.
        self._fetched_at.update(sparse_fetched)
        core = (
            me_payload,
            *(
                self._degrade_core_payload(label, result)
                for label, result in zip(CORE_ENDPOINT_LABELS, core_results)
            ),
        )
        optional = {
            label: self._degrade_optional_payload(label, result)
            for label, result in zip(gathered_labels, optional_results, strict=True)
        }
        # The comment lookups, once the grades they belong to are known.
        sources = {"Grades": core[_CORE_PAYLOAD_LABELS.index("Grades")], **optional}
        grade_comments, behaviour_comments = await asyncio.gather(
            self._async_get_comments(
                "Grades/Comments", self._client.async_get_grade_comments, sources["Grades"]
            ),
            self._async_get_comments(
                "BehaviourGrades/Points/Comments",
                self._client.async_get_behaviour_grade_point_comments,
                sources["BehaviourGrades/Points"],
                enabled=behaviour_grades_enabled,
            ),
        )
        optional["Grades/Comments"] = grade_comments
        optional["BehaviourGrades/Points/Comments"] = behaviour_comments

        return (*core, *(optional[label] for label in OPTIONAL_ENDPOINT_LABELS))

    async def _sparse(
        self,
        label: str,
        enabled: bool,
        factory: Any,
        every: timedelta | None,
        fetched_at: dict[str, datetime],
    ) -> dict[str, Any]:
        """A tier-2 endpoint whose data rarely changes: its last good
        response while it is younger than `every` (None = every cycle),
        otherwise a real request. Off in the options: `{}`, no request (see
        `_maybe`). A failure raises into the tier's gather as before, and
        doesn't count as an answer - the next cycle asks again. When it was
        asked is put into `fetched_at`, which the caller commits once the
        cycle's responses are kept."""
        if not enabled:
            return {}
        fetched = self._fetched_at.get(label)
        if (
            every is not None
            and fetched is not None
            and label in self._last_good
            and dt_util.utcnow() - fetched < every
        ):
            return self._last_good[label]
        result = await factory()
        fetched_at[label] = dt_util.utcnow()
        return result

    async def _async_get_comments(
        self, label: str, factory: Any, source_payload: Any, *, enabled: bool = True
    ) -> dict[str, Any]:
        """A comment lookup (`Grades/Comments`, `BehaviourGrades/Points/
        Comments`, `DescriptiveGrades/Comments`): every comment of the
        school year, while a grade points
        at a few of them. Asked only when a grade references a comment id
        the saved lookup doesn't have yet, and otherwise once a day (a
        teacher can edit a comment's text). The saved copy stands in on a
        failure, like any optional endpoint."""
        if not enabled:
            # Switched off in the options: no request, and any earlier
            # degraded-endpoint issue for it is cleared.
            return self._degrade_optional_payload(label, {})
        referenced = _referenced_comment_ids(source_payload)
        missing = referenced - _comment_ids(self._last_good.get(label))
        if not missing:
            every: timedelta | None = _DAILY
        elif missing <= self._unresolved_lookup_ids.get(label, set()):
            # Asked already and Librus didn't have them either: hourly, not
            # every cycle.
            every = _HOURLY
        else:
            every = None
        payload = await self._async_optional(label, factory, every=every)
        if label not in self._failed_this_cycle:
            self._unresolved_lookup_ids[label] = referenced - _comment_ids(payload)
        return payload

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
        self._remember_payload(label, result)
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
                if label not in _TIMETABLE_WEEK_LABELS:
                    self._remember_payload(label, {})
                return {}
            if (
                isinstance(result, LibrusError)
                and not isinstance(result, LibrusSessionExpiredError)
                and label in _TIMETABLE_WEEK_LABELS
            ):
                # The week failed and `_timetable_cache` has no copy of it
                # (a week the coordinator hasn't fetched yet, e.g. the next
                # one right after Sunday): no lessons known for it this
                # cycle, rather than failing the whole cycle - and rather
                # than another week's copy.
                _LOGGER.debug("Timetable week '%s' failed with no saved copy: %s", label, result)
                self._note_optional_endpoint_failure("Timetable")
                self.fallback_sections.add("Timetable")
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
        if label not in _TIMETABLE_WEEK_LABELS:
            # The weeks are kept in `_timetable_cache` (by their Monday).
            self._remember_payload(label, result)
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
        self._failed_this_cycle.add(label)
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
        """The lucky number, asked for only when a new one can be there:
        not while the cached one is for a later day (Librus publishes the
        next school day's number the afternoon before - CONFIRMED live), not
        before LUCKY_NUMBER_PUBLISH_HOUR once today's is known, and otherwise
        at most once an hour - also when Librus has no number at all
        (holidays, a school without the feature), which used to be asked
        every cycle all day. A failed request is retried the next cycle."""
        cached = self._cached_lucky_number
        today_iso = today.isoformat()
        cached_day = ((cached.day or "")[:10]) if cached is not None else ""
        if cached_day > today_iso:
            return cached
        if cached_day == today_iso and dt_util.now().hour < LUCKY_NUMBER_PUBLISH_HOUR:
            return cached
        asked = self._lucky_number_fetched_at
        if asked is not None and dt_util.utcnow() - asked < _HOURLY:
            return cached
        try:
            payload = await self._limited(self._client.async_get_lucky_number())
        except LibrusError:
            _LOGGER.debug("Lucky number fetch failed (non-fatal)", exc_info=True)
            self._note_optional_endpoint_failure("LuckyNumbers")
            return cached
        self._note_optional_endpoint_recovery("LuckyNumbers")
        self._lucky_number_fetched_at = dt_util.utcnow()
        lucky = parse_lucky_number(payload)
        if lucky is not None:
            self._cached_lucky_number = lucky
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
            if (
                label in _SCHOOL_SETTING_LABELS
                and getattr(result, "status_code", None) in (401, 403, 404)
            ):
                # Not every school or account may read its grade scale: use
                # the default one, not a degraded-endpoint repair issue -
                # and clear one an older version raised for it.
                self._note_optional_endpoint_recovery(label)
                return {}
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
        self._remember_payload(label, result)
        return result

    async def _async_refresh_reference_data(self) -> bool:
        """Refresh near-static reference data at most once a day: subject/
        teacher/classroom name lookups, school/class identity, homework
        agenda categories, note categories, behaviour-grade categories,
        grade categories, attendance types and the free-days calendar.

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

        Returns whether it actually fetched this cycle (False within the
        day) - `_async_resolve_missing_lookups` doesn't ask again for what
        was just fetched.
        """
        now = dt_util.utcnow()
        if (
            self._reference_data_fetched_at is not None
            and now - self._reference_data_fetched_at < timedelta(hours=24)
        ):
            return False
        free_days_enabled = self._feature_enabled(CONF_FREE_DAYS_ENABLED, DEFAULT_FREE_DAYS_ENABLED)
        behaviour_grades_enabled = self._feature_enabled(
            CONF_BEHAVIOUR_GRADES_ENABLED, DEFAULT_BEHAVIOUR_GRADES_ENABLED
        )
        # Seventeen requests on one session, next to the rest of the cycle's
        # (which now run alongside): a few at a time through the shared
        # limit is gentler on Librus and on the session.
        requests = (
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
            self._client.async_get_grading_system(),
            self._client.async_get_grade_categories(),
            self._client.async_get_attendance_types(),
        )
        results = await asyncio.gather(
            *(self._limited(request) for request in requests), return_exceptions=True
        )
        payloads = {
            label: self._degrade_reference_result(label, result)
            for label, result in zip(REFERENCE_DATA_ENDPOINT_LABELS, results, strict=True)
        }
        self._apply_reference_payloads(payloads)
        if self._kindergarten_lid is not None:
            await self._async_refresh_kindergarten_reference_data(payloads["Teachers"])
        self._check_school_year_rollover()
        await self._async_refresh_student_number()
        self._reference_data_fetched_at = now
        return True

    async def _async_resolve_missing_lookups(
        self, core_payloads: tuple[Any, ...], reference_refreshed: bool
    ) -> None:
        """Ask for the grade categories / attendance types again in the same
        cycle when a grade or attendance record points at an id the cached
        lookup doesn't have (a category the teacher just created) - they are
        otherwise refreshed once a day with the rest of the reference data.

        Not repeated for ids Librus itself didn't know when last asked - one
        refetch that didn't find them is enough, they wait for the daily
        refresh (asking hourly cost a request an hour for an id the
        endpoint may never list) - nor right after the daily refresh just
        fetched them. A failed request is retried the next cycle; until then its
        label counts as failed this cycle, so the absences tracker doesn't
        record absences of a type it can't see yet."""
        now = dt_util.utcnow()
        checks = (
            (
                "Grades/Categories",
                _referenced_ids(
                    core_payloads[_CORE_PAYLOAD_LABELS.index("Grades")], "Grades", "Category"
                ),
                self._client.async_get_grade_categories,
            ),
            (
                "Attendances/Types",
                _referenced_ids(
                    core_payloads[_CORE_PAYLOAD_LABELS.index("Attendances")], "Attendances", "Type"
                ),
                self._client.async_get_attendance_types,
            ),
        )
        for label, referenced, factory in checks:
            missing = referenced - set(self._lookup_map(label))
            if reference_refreshed:
                if label not in self._failed_this_cycle:
                    self._lookup_refetched_at[label] = now
                    self._unresolved_lookup_ids[label] = missing
                continue
            if not missing:
                continue
            asked = self._lookup_refetched_at.get(label)
            if (
                asked is not None
                and missing <= self._unresolved_lookup_ids.get(label, set())
                and now - asked < _DAILY
            ):
                continue
            try:
                result: dict[str, Any] | BaseException = await self._limited(factory())
            except LibrusError as err:
                result = err
            payload = self._degrade_reference_result(label, result)
            if isinstance(result, BaseException):
                continue
            if label == "Grades/Categories":
                self._cached_grade_categories = parse_grade_categories(payload)
            else:
                self._cached_attendance_types = parse_attendance_types(payload)
            self._lookup_refetched_at[label] = now
            self._unresolved_lookup_ids[label] = referenced - set(self._lookup_map(label))

    def _lookup_map(self, label: str) -> dict[Any, Any]:
        if label == "Grades/Categories":
            return self._cached_grade_categories
        return self._cached_attendance_types

    def _apply_reference_payloads(self, payloads: dict[str, Any]) -> None:
        """Parse the reference-data responses (keyed by
        REFERENCE_DATA_ENDPOINT_LABELS) into the name lookups. Also used to
        rebuild them from the saved responses when Librus is down at start.
        A disabled toggle's payload is `{}` (via `_maybe`), which every
        parser treats the same as a genuinely empty account."""

        def payload(label: str) -> dict[str, Any]:
            return payloads.get(label) or {}

        self._cached_subjects = parse_id_name_map(payload("Subjects"), ("Subjects",))
        # Also by LID (`AccountId`) - the new descriptive grading names its
        # teachers that way, like the kindergarten timetable. A new dict (see
        # _apply_kindergarten_payloads).
        self._cached_teachers = {
            **parse_id_name_map(payload("Teachers"), ("Users", "Teachers")),
            **parse_kindergarten_teachers(payload("Teachers")),
        }
        if payload("GradingSystem"):
            self._cached_grading_system = parse_grading_system(payload("GradingSystem"))
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
        self._cached_grade_categories = parse_grade_categories(payload("Grades/Categories"))
        self._cached_attendance_types = parse_attendance_types(payload("Attendances/Types"))

    async def _async_get_point_grades(self) -> list[PointGradeData]:
        """Point grades with their categories (maximum, weight). Skipped
        when Units says the school doesn't grade in points, and asked at most
        hourly while Units hasn't said either way (most schools don't use
        them). The categories are a lookup like the grade categories: once a
        day, and in the same cycle when a point grade points at a category
        the saved copy doesn't have - after one such refetch that didn't
        find it either, daily again. A failure keeps the last good copy,
        like any optional endpoint."""
        if self.point_grades_enabled is False:
            return []
        grades_label, categories_label = _POINT_GRADE_LABELS
        grades_payload = await self._async_optional(
            grades_label,
            self._client.async_get_point_grades,
            every=None if self.point_grades_enabled else _HOURLY,
        )
        referenced = _referenced_ids(grades_payload, "Grades", "Category")
        missing = referenced - set(
            parse_point_grade_categories(self._last_good.get(categories_label) or {})
        )
        every = (
            _DAILY
            if not missing or missing <= self._unresolved_lookup_ids.get(categories_label, set())
            else None
        )
        categories_payload = await self._async_optional(
            categories_label, self._client.async_get_point_grade_categories, every=every
        )
        categories = parse_point_grade_categories(categories_payload)
        if categories_label not in self._failed_this_cycle:
            self._unresolved_lookup_ids[categories_label] = referenced - set(categories)
        return parse_point_grades(grades_payload, categories)

    async def _async_get_descriptive_grade_lookups(
        self, core_payloads: tuple[dict[str, Any], ...]
    ) -> dict[str, dict[str, Any]]:
        """Comments and skill names for the descriptive grades - skipped
        entirely when the student has none. The comments like the other
        comment lookups (when a grade points at one the saved copy doesn't
        have, otherwise once a day), the skills once a day.
        The skills list holds the whole school's skills (~330 KB, seen
        live), so only id + name are kept."""
        payload = core_payloads[_CORE_PAYLOAD_LABELS.index("DescriptiveGrades")]
        if not (payload or {}).get("Grades"):
            return {}

        async def skill_names() -> dict[str, Any]:
            skills = (await self._client.async_get_descriptive_grade_skills()).get("Skills")
            return {
                "Skills": [
                    {"Id": item.get("Id"), "Name": item.get("Name")}
                    for item in skills or []
                    if isinstance(item, dict)
                ]
            }

        return {
            "DescriptiveGrades/Comments": await self._async_get_comments(
                "DescriptiveGrades/Comments",
                self._client.async_get_descriptive_grade_comments,
                payload,
            ),
            "DescriptiveGrades/Skills": await self._async_optional(
                "DescriptiveGrades/Skills", skill_names, every=_DAILY
            ),
        }

    async def _async_get_partial_grades(self) -> dict[str, dict[str, Any]]:
        """Grades from the new descriptive grading some schools use for
        grade 1 from 2026 (a POST per cycle, CONFIRMED reachable on a grade 7
        parent account, empty there). The child's LID is looked up once a
        day, the LID -> subject lookup only when there are grades. A 403/404
        (an account without the module) counts as "no grades"."""
        if not self._feature_enabled(
            CONF_DESCRIPTIVE_GRADES_ENABLED, DEFAULT_DESCRIPTIVE_GRADES_ENABLED
        ):
            return {}

        async def student() -> dict[str, Any]:
            # A refused lookup is an answer too ("no LID"), kept for the
            # day like any other - not retried on every poll. Anything else
            # (a dead session, a timeout, a 5xx) is not an answer: it
            # propagates, so _async_optional keeps the last good LID and
            # asks again next cycle instead of hiding the grades for a day.
            try:
                token = extract_token_user_identifier(await self._client.async_get_token_info())
                if not token:
                    return {"student": None}
                return {
                    "student": extract_student_identifier(
                        await self._client.async_get_user_info(token)
                    )
                }
            except (LibrusSessionExpiredError, LibrusUnexpectedResponseError) as err:
                status = getattr(err, "status_code", None)
                if status in (403, 404) or (
                    isinstance(err, LibrusUnexpectedResponseError) and status == 401
                ):
                    return {"student": None}
                raise

        lid = (await self._async_optional("StudentIdentifier", student, every=_DAILY)).get(
            "student"
        )
        if not lid:
            return {}

        async def grades() -> dict[str, Any]:
            try:
                return await self._client.async_get_partial_grades(lid)
            except (LibrusSessionExpiredError, LibrusUnexpectedResponseError) as err:
                status = getattr(err, "status_code", None)
                if status in (403, 404) or (
                    isinstance(err, LibrusUnexpectedResponseError) and status in (401, 405)
                ):
                    return {"data": []}
                raise

        # Most accounts never have any: then asked once an hour, not every cycle.
        had_grades = bool((self._last_good.get("PartialGrades") or {}).get("data"))
        payload = await self._async_optional(
            "PartialGrades", grades, every=None if had_grades else _HOURLY
        )
        if not payload.get("data"):
            return {"PartialGrades": payload}
        return {
            "PartialGrades": payload,
            "Auth/Subjects": await self._async_optional(
                "Auth/Subjects", self._client.async_get_auth_subjects, every=_DAILY
            ),
        }

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
            result: dict[str, Any] | BaseException = await self._limited(factory())
        except LibrusError as err:
            result = err
        payload = self._degrade_optional_payload(label, result)
        if not isinstance(result, BaseException):
            self._fetched_at[label] = dt_util.utcnow()
        return payload

    async def _limited(self, request: Awaitable[_T]) -> _T:
        """Await one Synergia request within the shared `_request_limit`.
        Only around single requests (or one factory's own short chain) -
        never around something that waits for the limit itself."""
        async with self._request_limit:
            return await request

    async def async_download_attachment(self, attachment_id: str, message_id: str) -> Any:
        """Download one message attachment for the attachment view
        (attachment_view.py), with the same one-retry Wiadomości recovery as
        `async_fetch_message`. Doesn't open (mark read) the message."""
        assert self.config_entry is not None
        seen = self._login_count
        try:
            return await self._client.async_download_message_attachment(attachment_id, message_id)
        except LibrusSessionExpiredError:
            await self._async_relogin(seen)
            self._messages_bootstrapped = False
            self._messages_available = await self._client.async_bootstrap_messages()
            self._messages_bootstrapped = True
            return await self._client.async_download_message_attachment(attachment_id, message_id)

    async def async_download_homework_attachment(self, attachment_id: str) -> Any:
        """Download one homework-assignment attachment for the attachment
        view. Lives on the main Synergia session, so a rejected session gets
        one forced relogin + retry, like `async_fetch_timetable_week`."""
        assert self.config_entry is not None
        seen = self._login_count
        try:
            return await self._client.async_download_homework_attachment(attachment_id)
        except LibrusSessionExpiredError:
            await self._async_relogin(seen)
            return await self._client.async_download_homework_attachment(attachment_id)

    async def async_download_school_file(self, file_id: str) -> Any:
        """Download one school document (by its SchoolFiles id) for the
        attachment view, with one forced relogin + retry like the homework
        files. An unknown id is reported as not found."""
        assert self.config_entry is not None
        files = self.data.school_files if self.data else []
        path = next((f.download_path for f in files if str(f.id) == file_id), None)
        if not path:
            raise LibrusUnexpectedResponseError(
                f"Unknown school document {file_id}", status_code=404
            )
        seen = self._login_count
        try:
            return await self._client.async_download_school_file(path)
        except LibrusSessionExpiredError:
            await self._async_relogin(seen)
            return await self._client.async_download_school_file(path)

    async def _async_get_justifications(self) -> list[JustificationData]:
        """The parent's submitted absence justifications. A failure keeps
        the last good copy, like any optional endpoint."""
        try:
            result: dict[str, Any] | BaseException = await self._limited(
                self._client.async_get_justifications()
            )
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
                number = parse_user_class_register_number(
                    await self._limited(self._client.async_get_user(user_id))
                )
            except LibrusError as err:
                _LOGGER.debug("Student Users record fetch failed (non-fatal): %s", err)
                number = None
            if number is not None:
                self.student_number_from_librus = number
                self._note_optional_endpoint_recovery(STUDENT_INFO_LABEL)
                return
        try:
            page = await self._limited(self._client.async_get_student_info_page())
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
            self._limited(self._client.async_get_kindergarten_activity_types()),
            self._limited(self._client.async_get_kindergarten_classrooms()),
            self._limited(
                self._maybe(
                    group_id is not None,
                    lambda: self._client.async_get_kindergarten_group(group_id),
                )
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
        self._apply_kindergarten_payloads(
            activity_payload, classrooms_payload, group_payload, teachers_payload
        )

    def _apply_kindergarten_payloads(
        self,
        activity_payload: dict[str, Any],
        classrooms_payload: dict[str, Any],
        group_payload: dict[str, Any],
        teachers_payload: dict[str, Any],
    ) -> None:
        """Merge the kindergarten lookups into new dicts, never into the
        current ones: those are the very dicts the last LibrusData holds, and
        updating them in place changed the published data under the entities
        (and made it compare equal to the next cycle's)."""
        self._cached_subjects = {
            **self._cached_subjects,
            **parse_kindergarten_activity_types(activity_payload),
        }
        self._cached_classrooms = {
            **self._cached_classrooms,
            **parse_kindergarten_classrooms(classrooms_payload),
        }
        self._cached_teachers = {
            **self._cached_teachers,
            **parse_kindergarten_teachers(teachers_payload),
        }
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
        now = dt_util.utcnow()
        recheck = not self._messages_available and (
            self._messages_denials < _MESSAGES_QUICK_RECHECKS
            or self._messages_recheck_at is None
            or now >= self._messages_recheck_at
        )
        if not self._messages_bootstrapped or recheck:
            self._messages_available = await self._client.async_bootstrap_messages()
            self._messages_bootstrapped = True
            if self._messages_available:
                self._messages_denials = 0
            else:
                self._messages_denials += 1
                self._messages_recheck_at = now + _HOURLY
        if not self._messages_available:
            return 0, {}, []
        # The unread counts every cycle; the inbox list only when its count
        # changed, or the copy is an hour old (see _reusable_mailbox_list).
        unread_count, unread_by_mailbox, _ = parse_messages(
            await self._client.async_get_unread_messages_count(), {}
        )
        inbox = self._reusable_mailbox_list("inbox", unread_count)
        if inbox is None:
            inbox = parse_message_list(await self._client.async_get_messages(limit=10), "inbox")
            self._remember_mailbox_list("inbox", unread_count, inbox)
        return unread_count, unread_by_mailbox, inbox

    def _reusable_mailbox_list(self, box: str, count: int | None) -> list[MessageData] | None:
        """The last list of a mailbox, when it can stand in for a new
        request: the mailbox's unread count is the same as when it was
        fetched (a new message raises it, reading one lowers it) and it is
        less than _MESSAGE_LIST_MAX_AGE old. `count` is None for a mailbox
        without one (outbox): then only the age decides. None = fetch."""
        fetched = self._mailbox_fetched_at.get(box)
        if (
            fetched is None
            or box not in self._mailbox_lists
            or self._mailbox_counts.get(box) != count
            or dt_util.utcnow() - fetched >= _MESSAGE_LIST_MAX_AGE
        ):
            return None
        return list(self._mailbox_lists[box])

    def _remember_mailbox_list(
        self, box: str, count: int | None, messages: list[MessageData]
    ) -> None:
        self._mailbox_lists[box] = list(messages)
        self._mailbox_counts[box] = count
        self._mailbox_fetched_at[box] = dt_util.utcnow()

    def _forget_mailbox_list(self, box: str) -> None:
        """Drop a mailbox's cached list so the next cycle fetches it again
        (the archive is otherwise read once a day)."""
        self._mailbox_lists.pop(box, None)
        self._mailbox_counts.pop(box, None)
        self._mailbox_fetched_at.pop(box, None)
        if box == ARCHIVE_MAILBOX:
            self._archive_fetched_at = None

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
            return _NO_MESSAGES

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
                if self._last_messages is None:
                    return _NO_MESSAGES
                # The last good counts and lists, not 0 and empty lists: a
                # failed fetch (the Wiadomości session dies often) used to
                # show "0 unread" until the next cycle.
                self.fallback_sections.add("Messages")
                return self._last_messages
        if not self._messages_available:
            if self._last_messages is not None and self._messages_denials < _MESSAGES_QUICK_RECHECKS:
                # A "no access" answer mid-run, while it is still being
                # rechecked every cycle (often a session being replaced at
                # that moment): the last messages, not an unavailable sensor
                # for a cycle. Only a repeated answer clears them.
                self.fallback_sections.add("Messages")
                self._failed_this_cycle.add("Messages")
                return self._last_messages
            self._last_messages = None
            return _NO_MESSAGES
        self._note_optional_endpoint_recovery("Messages")

        # BUG FIX (2026-09-06, found live): the secondary mailboxes used to
        # be fetched in the SAME asyncio.gather() as the inbox -
        # asyncio.gather() fails as a whole the moment ANY one of its
        # awaitables raises, so a failure fetching a bonus mailbox wiped
        # out the otherwise-working inbox/unread-count data too (confirmed
        # live in v0.4.13). They're fetched separately, and each one on its
        # own (return_exceptions), so one mailbox can't empty the others.
        # A 404 means this account doesn't have that mailbox (confirmed
        # live for alerts/substitutions on some accounts) - empty, not a
        # failure.
        secondary = await self._async_get_secondary_mailboxes(
            ("substitutions", "alerts", "justifications", "outbox"), unread_by_mailbox
        )
        await self._async_refresh_archived_messages()
        if "outbox" not in self._failed_mailboxes:
            await self._async_refresh_read_receipts(secondary["outbox"])

        self._last_messages = (
            unread_count,
            unread_by_mailbox,
            inbox_messages,
            secondary["substitutions"],
            secondary["alerts"],
            secondary["justifications"],
            secondary["outbox"],
            list(self._archived_messages),
        )
        return self._last_messages

    async def _async_get_secondary_mailboxes(
        self, mailboxes: tuple[str, ...], unread_by_mailbox: dict[str, int]
    ) -> dict[str, list[MessageData]]:
        """The other mailboxes' lists. A mailbox the account doesn't have
        (404) is asked again only once a day - it stays in
        `missing_mailboxes` meanwhile, so the Messages card keeps hiding it.
        A list is reused while the mailbox's unread count is unchanged and
        the copy is under an hour old (outbox has no count: hourly)."""
        now = dt_util.utcnow()
        messages: dict[str, list[MessageData]] = {}
        to_fetch: list[str] = []
        for box in mailboxes:
            probed = self._mailbox_probed_at.get(box)
            if (
                box in self.missing_mailboxes
                and probed is not None
                and now - probed < _MISSING_MAILBOX_RECHECK
            ):
                messages[box] = []
                continue
            count = unread_by_mailbox.get(box)
            reused = self._reusable_mailbox_list(box, count)
            if reused is not None:
                messages[box] = reused
                continue
            to_fetch.append(box)
        results = await asyncio.gather(
            *(self._client.async_get_messages(mailbox=box, limit=10) for box in to_fetch),
            return_exceptions=True,
        )
        failed = False
        self._failed_mailboxes = set()
        for box, result in zip(to_fetch, results, strict=True):
            if isinstance(result, LibrusUnexpectedResponseError) and result.status_code == 404:
                messages[box] = []
                self.missing_mailboxes.add(box)
                self._mailbox_probed_at[box] = now
                self._mailbox_lists.pop(box, None)
            elif isinstance(result, BaseException):
                if not isinstance(result, LibrusError):
                    raise result
                _LOGGER.debug("Mailbox %s fetch failed (non-fatal)", box, exc_info=result)
                # Its last list, not an empty one (an outage made a whole
                # mailbox look empty until the next cycle).
                messages[box] = list(self._mailbox_lists.get(box, []))
                self._failed_mailboxes.add(box)
                failed = True
            else:
                messages[box] = parse_message_list(result, box)
                self._remember_mailbox_list(box, unread_by_mailbox.get(box), messages[box])
                self.missing_mailboxes.discard(box)
                self._mailbox_probed_at.pop(box, None)
        if failed:
            self._note_optional_endpoint_failure("Messages/Secondary")
            self.fallback_sections.add("Messages/Secondary")
        else:
            self._note_optional_endpoint_recovery("Messages/Secondary")
        return messages

    async def _async_refresh_read_receipts(self, sent: list[MessageData]) -> None:
        """Who has read the messages you sent (CONFIRMED live 2026-10-09:
        opening a SENT message has no side effect). The newest few sent in
        the last READ_RECEIPT_DAYS days, each at most once an hour, and not
        again once everyone has read it - or once it is known to have no
        recipients listed (`total == 0`, e.g. a message to a whole group),
        which would otherwise be asked for hourly for a month. The due ones
        are asked for together, at most _READ_RECEIPT_CONCURRENCY at once. A
        recipient who has newly read one fires EVENT_MESSAGE_READ; the first
        look at a message only records."""
        now = dt_util.utcnow()
        cutoff = (dt_util.now().date() - timedelta(days=_READ_RECEIPT_DAYS)).isoformat()
        recent = [m for m in sent if (m.send_date or "")[:10] >= cutoff][:_READ_RECEIPT_MESSAGES]
        keep = {m.id for m in recent}
        self.read_receipts = {k: v for k, v in self.read_receipts.items() if k in keep}
        self._receipts_fetched_at = {
            k: v for k, v in self._receipts_fetched_at.items() if k in keep
        }
        due = []
        for message in recent:
            known = self.read_receipts.get(message.id)
            if known and (known["total"] == 0 or known["read"] >= known["total"]):
                continue
            fetched = self._receipts_fetched_at.get(message.id)
            if known and fetched and now - fetched < _HOURLY:
                continue
            due.append(message)
        if not due:
            return
        limit = asyncio.Semaphore(_READ_RECEIPT_CONCURRENCY)

        async def fetch(message: MessageData) -> Any:
            async with limit:
                return await self._client.async_get_message("outbox", message.id)

        results = await asyncio.gather(*(fetch(m) for m in due), return_exceptions=True)
        # The student's name from this cycle's Me (stored before the
        # messages are fetched) - `self.data` is still None on the first
        # refresh after a restart, when receipts saved before it can already
        # fire events.
        student = parse_me(self._last_good.get("Me") or {}).display_name
        for message, payload in zip(due, results, strict=True):
            if isinstance(payload, BaseException):
                if not isinstance(payload, LibrusError):
                    raise payload
                _LOGGER.debug("Read receipts fetch failed (non-fatal)", exc_info=payload)
                continue
            full = parse_message(payload, "outbox", message.id)
            if full is None:
                continue
            self._receipts_fetched_at[message.id] = now
            receivers = [
                {"name": r.name, "group": r.group, "read": r.read_date} for r in full.receivers
            ]
            known = self.read_receipts.get(message.id)
            if known is not None:
                before = {r["name"] for r in known["receivers"] if r["read"]}
                for receiver in receivers:
                    if receiver["read"] and receiver["name"] not in before:
                        self.hass.bus.async_fire(
                            EVENT_MESSAGE_READ,
                            {
                                "entry_id": self.config_entry.entry_id if self.config_entry else None,
                                "id": message.id,
                                "student": student,
                                "topic": message.topic,
                                "receiver": receiver["name"],
                                "group": receiver["group"],
                                "read_date": receiver["read"],
                                "send_date": message.send_date,
                            },
                        )
            self.read_receipts[message.id] = {
                "receivers": receivers,
                "read": sum(1 for r in receivers if r["read"]),
                "total": len(receivers),
            }

    async def _async_refresh_archived_messages(self) -> None:
        """The archive of past school years barely changes - read it once a
        day. A failure keeps the last list."""
        now = dt_util.utcnow()
        if (
            self._archive_fetched_at is not None
            and now - self._archive_fetched_at < _ARCHIVE_REFRESH_INTERVAL
        ):
            return
        try:
            payload = await self._client.async_get_messages(mailbox=ARCHIVE_MAILBOX, limit=10)
        except LibrusUnexpectedResponseError as err:
            if err.status_code != 404:
                _LOGGER.debug("Archived messages fetch failed (non-fatal)", exc_info=True)
                return
            payload = {}
            self.missing_mailboxes.add(ARCHIVE_MAILBOX)
        except LibrusError:
            _LOGGER.debug("Archived messages fetch failed (non-fatal)", exc_info=True)
            return
        if payload:
            self.missing_mailboxes.discard(ARCHIVE_MAILBOX)
        self._archived_messages = parse_message_list(payload, ARCHIVE_MAILBOX)
        self._archive_fetched_at = now

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
        """New text grades and descriptive grades (as EVENT_NEW_GRADE with
        `kind: text` / `kind: descriptive`), school trips and school
        documents - seeded silently on the first sync."""
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
        descriptive_grades = {
            grade.id: {
                "subject_id": grade.subject_id,
                "subject": data.subjects.get(grade.subject_id) if grade.subject_id is not None else None,
                "value": grade.value,
                "teacher": _teacher_name(data, grade.teacher_id if grade.teacher_id is not None else grade.teacher_lid),
                # The skill the grade is for - what Synergia shows as its category.
                "category": grade.skill,
                "skill": grade.skill,
                "weight": None,
                "counts_to_average": False,
                "comments": list(grade.comments),
                "date": grade.date or grade.add_date,
                "semester": grade.semester,
                "kind": "descriptive",
                "improves": None,
            }
            for grade in data.descriptive_grades
        }
        descriptive_on = self._feature_enabled(
            CONF_DESCRIPTIVE_GRADES_ENABLED, DEFAULT_DESCRIPTIVE_GRADES_ENABLED
        )
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
            ("descriptive_grades", EVENT_NEW_GRADE, descriptive_grades),
            ("school_trips", EVENT_NEW_SCHOOL_TRIP, trips),
            ("school_files", EVENT_NEW_SCHOOL_DOCUMENT, files),
        ):
            label = {
                "text_grades": "BaseTextGrades",
                "descriptive_grades": "DescriptiveGrades",
                "school_trips": "SchoolTrips",
                "school_files": "SchoolFiles",
            }[kind]
            available = label not in self._failed_this_cycle
            if kind == "descriptive_grades":
                # Turned off in the options: no data, so nothing to seed -
                # turning it back on seeds silently instead of announcing
                # every existing grade. Same when the new descriptive
                # grading failed this cycle.
                available = (
                    available and descriptive_on and "PartialGrades" not in self._failed_this_cycle
                )
            self._known_items[kind] = self._fire_for_new_ids(
                event,
                entry_id,
                self._known_items[kind],
                items,
                student=student,
                available=available,
            )

    def _fire_justification_events(self, data: LibrusData) -> None:
        """EVENT_JUSTIFICATION_STATUS when a submitted justification's
        status changes (the school accepted or rejected it)."""
        current = {str(j.id): j.status for j in data.justifications}
        known = self._known_justifications
        # Only the justifications Librus still lists: the old merge kept
        # every one ever seen in the saved state.
        self._known_justifications = current
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
        today_iso = today.isoformat()

        def upcoming(fields: dict[str, Any]) -> bool:
            return (fields.get("date") or "")[:10] >= today_iso

        current = {str(item.id): _agenda_fields(item, data) for item in data.homeworks}
        known = self._known_agenda
        # Only the upcoming entries are kept: a past one can neither change
        # nor disappear in a way worth an event (see below), and keeping
        # every entry of the school year only grew the saved state.
        self._known_agenda = {
            item_id: fields for item_id, fields in current.items() if upcoming(fields)
        }
        if known is None or "HomeWorks" in self.fallback_sections:
            return
        if known and not current:
            # Everything gone at once is a failed or emptied fetch, not a
            # school cancelling every event - don't announce it.
            self._known_agenda = known
            return
        base = {
            "entry_id": self.config_entry.entry_id if self.config_entry else None,
            "student": data.me.display_name,
        }

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
            available="HomeWorkAssignments" not in self._failed_this_cycle,
        )

    def _check_achievements(self, data: LibrusData, today: date) -> None:
        """Badges (achievements.py) for the Rank sensor, and
        EVENT_ACHIEVEMENT_UNLOCKED for each key newly earned. A key keeps the
        date it was earned and is never revoked within the school year, even
        when its streak later breaks; a new school year starts over.

        Skipped when the last complete check was for the same data object,
        day, number of ticked homework and register number (an unchanged
        poll)."""
        student_number = None
        if self.config_entry is not None:
            raw = self.config_entry.options.get(CONF_STUDENT_NUMBER)
            student_number = int(raw) if raw is not None else self.student_number_from_librus
        checked = (data, today, len(self.homework_done_ever), student_number)
        last = self._achievements_checked
        if (
            last is not None
            and not self._legacy_achievements
            and last[0] is data
            and last[1:] == checked[1:]
        ):
            return
        if self._legacy_achievements:
            # Keys saved by 0.12.4 or older: applied on the first cycle,
            # whatever its data, so they are part of the saved dates from
            # then on - and before the honours check below, which drops a
            # legacy "honours" the same way as any other. The first pass
            # that records badges still does it quietly, as it would have
            # without them (`_achievement_seed_pending`).
            dates = dict(self._achievement_dates or {})
            if self._achievement_dates is None:
                self._achievement_seed_pending = True
            for key in self._legacy_achievements:
                dates.setdefault(key, today.isoformat())
            self._achievement_dates = dates
            self._legacy_achievements = []
        try:
            badges = compute_badges(
                data,
                today,
                thresholds=self.grade_thresholds,
                weighted=self.weighted_average,
                student_number=student_number,
                homework_done=len(self.homework_done_ever),
                owner=self.memo_owner,
            )
        except Exception:  # noqa: BLE001 - badges must never fail the update
            _LOGGER.exception("Could not compute the badges")
            return
        self.badges = badges
        # Badges come from grades, notes and attendance; a failed fetch of
        # any of them says nothing about what's earned.
        if {"Grades", "Notes", "Attendances", "Attendances/Types"} & self._failed_this_cycle:
            return
        year = data.school_class.begin_school_year if data.school_class else None
        if year and self._achievement_year and year != self._achievement_year:
            # A new school year: start over, quietly.
            self._achievement_dates = None
            self.homework_done_ever.clear()
        if year:
            self._achievement_year = year
        known = self._achievement_dates
        seeding = known is None or self._achievement_seed_pending
        dates = dict(known or {})
        honours = next((b for b in badges if b.key == "honours"), None)
        if honours is not None and not honours.earned:
            # 0.12.5-beta.3..5 awarded it from the forecast during the year;
            # it's only earned once the year is over.
            dates.pop("honours", None)
        new: list[str] = []
        for item in badges:
            for key, earned_on in item.earned.items():
                if key not in dates:
                    dates[key] = earned_on or today.isoformat()
                    new.append(key)
        self._achievement_dates = dates
        self._achievement_seed_pending = False
        self._achievements_checked = checked
        if seeding:
            return
        # Only a badge earned in the last few days is news. An older date
        # means the data behind it only arrived now (a section that failed
        # before, an option switched on) - recorded quietly.
        cutoff = (today - _ACHIEVEMENT_NEWS_DAYS).isoformat()
        entry_id = self.config_entry.entry_id if self.config_entry else None
        for key in new:
            if dates[key] < cutoff:
                continue
            self.hass.bus.async_fire(
                EVENT_ACHIEVEMENT_UNLOCKED,
                {
                    "entry_id": entry_id,
                    "id": key,
                    "student": data.me.display_name,
                    "title": key_title(key),
                    "date": dates[key],
                },
            )

    def _unavailable_kinds(self, data: LibrusData) -> set[str]:
        """ChangeTracker kinds whose data this cycle can't be trusted to be
        complete: the endpoint failed, or the module is switched off."""
        failed = self._failed_this_cycle
        options = self.config_entry.options if self.config_entry else {}
        kinds = set()
        if "Grades" in failed:
            kinds.add("grades")
        if "Notes" in failed:
            kinds.add("notes")
        if "SchoolNotices" in failed or not options.get(
            CONF_ANNOUNCEMENTS_ENABLED, DEFAULT_ANNOUNCEMENTS_ENABLED
        ):
            kinds.add("announcements")
        if (
            "Messages" in failed
            or not data.messages_available
            or not options.get(CONF_MESSAGES_ENABLED, DEFAULT_MESSAGES_ENABLED)
        ):
            kinds.add("messages")
        if "HomeWorks" in failed:
            kinds.add("agenda")
        if {"Attendances", "Attendances/Types"} & failed:
            kinds.add("absences")
        if "Timetable" in failed:
            kinds.add("timetable_changes")
        return kinds

    def _update_change_tracker(self, data: LibrusData, today: date) -> Changes:
        """`ChangeTracker.update`, minus the batch of "new" items a kind would
        otherwise produce the first time its data shows up after seeding (a
        failed first fetch, messages or announcements switched on later, a
        timetable published later): that first real data is recorded
        silently."""
        tracker = self._change_tracker
        unavailable = self._unavailable_kinds(data)
        if not tracker.is_seeded:
            changes = tracker.update(data, today=today)
            self._unseeded_kinds = unavailable
            self._seen_version += 1
            return changes
        # Timetable changes are keyed "YYYY-MM-DD|...": the tracker only
        # reports them from today on, so older keys can never matter again
        # and only grew the saved state (every substitution all year).
        today_iso = today.isoformat()
        seen_changes = tracker.seen.timetable_changes
        stale = {key for key in seen_changes if key[:10] < today_iso}
        if stale:
            seen_changes -= stale
            self._seen_version += 1
        ready = self._unseeded_kinds - unavailable
        if ready:
            current = {
                "grades": {str(g.id) for g in data.grades},
                "notes": {str(n.id) for n in data.notes},
                "announcements": {str(n.id) for n in data.school_notices},
                "messages": {str(m.id) for m in data.messages},
                "agenda": {str(h.id) for h in data.homeworks},
                "absences": {str(a.id) for a in tracked_absences(data)},
                "timetable_changes": set(tracked_timetable_changes(data, today)),
            }
            for kind in ready:
                getattr(tracker.seen, kind).update(current.get(kind, set()))
            self._unseeded_kinds -= ready
            self._seen_version += 1
        changes = tracker.update(data, today=today)
        if changes:
            # Exactly the reported items were added to the seen ids.
            self._seen_version += 1
        return changes

    def _fire_for_new_ids(
        self,
        event: str,
        entry_id: str | None,
        known: set[Any] | None,
        items: dict[Any, dict[str, Any]],
        *,
        student: str | None = None,
        available: bool = True,
    ) -> set[Any] | None:
        if not available:
            # The fetch failed this cycle: nothing to compare against, and
            # seeding from an empty list would announce everything later.
            return known
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
