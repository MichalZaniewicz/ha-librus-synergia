"""Constants for the Librus Synergia (unofficial) integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "librus_synergia"

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.CALENDAR,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.TODO,
    Platform.EVENT,
]

# The session (see librus_synergia.LibrusSessionData) is cookie-based. It is
# renewed through Librus's /refreshToken, but once it lapses only a fresh
# login gets back in - unlike a bearer-token API, staying logged in
# unattended requires the password, so (unlike ha-suunto's
# revocable-session-key-only model) it is persisted here too. Only the
# cookie jar and login timestamp are the model's *addition* over a plain
# password store - see the librus-synergia library's LibrusApiClient docstring.
CONF_COOKIES = "cookies"
CONF_SESSION_LOGGED_IN_AT = "session_logged_in_at"

DEFAULT_SCAN_INTERVAL_MINUTES = 20
MIN_SCAN_INTERVAL_MINUTES = 10
MAX_SCAN_INTERVAL_MINUTES = 180

# Optional overnight window (off by default) where the coordinator skips
# the network round-trip entirely and just returns its last-known data -
# nothing meaningful changes while a family is asleep, and this is an
# unofficial API worth being modest with (same spirit as the scan_interval
# option's own "keep this modest" description). Off by default so nobody's
# entities silently go stale overnight without having opted in.
CONF_QUIET_HOURS_ENABLED = "quiet_hours_enabled"
DEFAULT_QUIET_HOURS_ENABLED = False
CONF_QUIET_HOURS_START = "quiet_hours_start"
DEFAULT_QUIET_HOURS_START = "23:00:00"
CONF_QUIET_HOURS_END = "quiet_hours_end"
DEFAULT_QUIET_HOURS_END = "06:00:00"

# Smart polling (off by default): the poll interval above applies on school
# days 06:00-22:00; on a day without lessons at most hourly, and at night
# (22:00-06:00) at most every 3 hours. A manual refresh always fetches.
CONF_SMART_POLLING = "smart_polling"
DEFAULT_SMART_POLLING = False
SMART_POLLING_DAY_OFF = 60  # minutes
SMART_POLLING_NIGHT = 180  # minutes
SMART_POLLING_NIGHT_START = 22
SMART_POLLING_NIGHT_END = 6

# Saved coordinator state (coordinator.async_restore_state): what was already
# seen, so a grade added while HA was off still fires its event, and the last
# good response of every endpoint, the fallback while Librus is down.
STATE_STORE_VERSION = 1
STATE_SAVE_DELAY = 300  # seconds
# The state is only written when something in it changed (a new response, a
# newly seen item, ...) - but at least this often, so `last_success_at` (how
# old the saved data is after a restart) doesn't drift far behind.
STATE_SAVE_MAX_INTERVAL = timedelta(hours=6)
# How old the last good data may get and still be shown while Librus keeps
# failing (and be rebuilt from the saved responses at HA start). Past this the
# entities go unavailable.
LAST_GOOD_DATA_MAX_AGE = timedelta(days=3)
# After the second failed cycle in a row the next attempts back off: twice the
# update interval, then four times, ... up to this.
OUTAGE_BACKOFF_MAX = timedelta(hours=2)

# Status sensor states.
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_STALE = "stale"
STATUS_ERROR = "error"
STATUS_OPTIONS = [STATUS_OK, STATUS_DEGRADED, STATUS_STALE, STATUS_ERROR]

# Hide subject average sensors for subjects without a single grade yet
# (e.g. Religia early in the year). Off by default; the sensor appears as
# soon as the subject gets its first grade.
CONF_HIDE_EMPTY_SUBJECTS = "hide_empty_subjects"
DEFAULT_HIDE_EMPTY_SUBJECTS = False

# Lessons held at the same time (a subject plus "Wspomaganie", split groups)
# become one timetable calendar event - issue #14. On by default.
CONF_MERGE_PARALLEL_LESSONS = "merge_parallel_lessons"
DEFAULT_MERGE_PARALLEL_LESSONS = True

# When False (set via the options flow), the coordinator skips the whole
# Wiadomości (private messages) subsystem - its separate wiadomosci.librus.pl
# session bootstrap plus the per-cycle unread-count/list calls. The Unread
# messages sensor then reports `unavailable`, exactly as it already does for
# a school that hasn't enabled the module.
CONF_MESSAGES_ENABLED = "messages_enabled"
DEFAULT_MESSAGES_ENABLED = True

# Same "skip the fetch, sensor goes unavailable" shape as CONF_MESSAGES_
# ENABLED above, extended to the other optional (non-core) data groups.
# `coordinator.py`'s `_maybe()` helper skips the network call entirely when
# one of these is False - the existing defensive parsers already treat an
# empty `{}` payload identically to a genuinely-empty account, so no extra
# special-casing was needed to wire these up.
CONF_ANNOUNCEMENTS_ENABLED = "announcements_enabled"
DEFAULT_ANNOUNCEMENTS_ENABLED = True
CONF_BEHAVIOUR_GRADES_ENABLED = "behaviour_grades_enabled"
DEFAULT_BEHAVIOUR_GRADES_ENABLED = True
CONF_DESCRIPTIVE_GRADES_ENABLED = "descriptive_grades_enabled"
DEFAULT_DESCRIPTIVE_GRADES_ENABLED = True
CONF_FREE_DAYS_ENABLED = "free_days_enabled"
DEFAULT_FREE_DAYS_ENABLED = True

# Which average the Overall/Subject average sensors (and Rank) report as
# their STATE. Weighted (by grade-category weight) is what Librus itself
# shows and stays the default; some schools don't use weights at all and
# want the plain arithmetic mean instead. Both figures stay available as
# attributes (`average_weighted`/`average_arithmetic`) either way.
CONF_AVERAGE_MODE = "average_mode"
AVERAGE_MODE_WEIGHTED = "weighted"
AVERAGE_MODE_ARITHMETIC = "arithmetic"
DEFAULT_AVERAGE_MODE = AVERAGE_MODE_WEIGHTED

# Minimum averages for a 2, 3, 4, 5 and 6, used by the grade forecast
# (forecast.py). Free text so any school's statute fits; an unusable value
# falls back to forecast.DEFAULT_GRADE_THRESHOLDS.
CONF_GRADE_THRESHOLDS = "grade_thresholds"

# The student's own number in the class register ("numer w dzienniku") -
# an optional manual override. The number is normally read from Librus
# (`Users/{Me.Account.UserId}.ClassRegisterNumber`, with the `informacja`
# web page as a fallback - see coordinator._async_refresh_student_number).
# Genuinely optional and unset by default (no DEFAULT_* - absent means "not
# configured", distinct from any real roster number) so the Lucky number
# sensor's `is_yours` attribute can stay `None` ("unknown, not configured")
# rather than falsely reporting `False` for a family that never set this.
CONF_STUDENT_NUMBER = "student_number"

# Weekly AI summary through Home Assistant's AI Task (see ai_summary.py) -
# same model as ha-suunto's daily AI insight: no API key here, the user
# picks an `ai_task` entity from any AI provider set up in HA. Unset = the
# feature is off and its sensor/button/switch are not created.
CONF_AI_TASK_ENTITY = "ai_task_entity"
# Who the summary is written to: the parent (third person, parent's to-dos)
# or the student (second person, encouraging).
CONF_AI_AUDIENCE = "ai_audience"
AI_AUDIENCE_PARENT = "parent"
AI_AUDIENCE_STUDENT = "student"
DEFAULT_AI_AUDIENCE = AI_AUDIENCE_PARENT
# ISO weekday as a string ("1" = Monday .. "7" = Sunday) - a SelectSelector
# value - plus the local time of the automatic run.
CONF_AI_WEEKDAY = "ai_weekday"
DEFAULT_AI_WEEKDAY = "7"
CONF_AI_TIME = "ai_time"
DEFAULT_AI_TIME = "18:00:00"
# Free-text notes added to the prompt ("egzamin ósmoklasisty w tym roku").
CONF_AI_CONTEXT = "ai_extra_context"
# Private messages and announcements are school/child data sent to an
# external provider - opt-in, off by default.
CONF_AI_INCLUDE_MESSAGES = "ai_include_messages"
DEFAULT_AI_INCLUDE_MESSAGES = False
AI_OPTION_KEYS = (
    CONF_AI_TASK_ENTITY,
    CONF_AI_AUDIENCE,
    CONF_AI_WEEKDAY,
    CONF_AI_TIME,
    CONF_AI_CONTEXT,
    CONF_AI_INCLUDE_MESSAGES,
)

# Labels for the SUPPLEMENTARY (tier 2, `return_exceptions=True`) endpoints
# fetched by `coordinator.py::_async_fetch_core_payloads`, in the order of
# that method's tier-2 result - used for the warning logged when one fails,
# and for the matching repair-issue id (see `coordinator.py::
# optional_endpoint_issue_id`). The two comment lookups are not in the
# tier-2 gather itself: they are asked only when a grade references a
# comment id that isn't known yet (or once a day), which needs the grades
# first - see `coordinator.py::_async_get_comments`. Public (not
# underscore-prefixed) since `__init__.py` also needs it, to clear any
# repair issues for a removed config entry.
OPTIONAL_ENDPOINT_LABELS = (
    "Grades/Comments",
    "HomeWorkAssignments",
    "BehaviourGrades/Points",
    "BehaviourGrades/Points/Comments",
    "DescriptiveGrades",
    "ParentTeacherConferences",
)

# Labels for TIER 1's own degradable endpoints - `Me` is deliberately NOT
# here (kept as a separate, always-fatal fetch ahead of this tier - if we
# can't even identify who the account belongs to, something is genuinely
# wrong, not just "a module this account type doesn't have"). In the
# exact order passed to `_async_fetch_core_payloads`' first
# `asyncio.gather()` call (Me excluded). Public for the same reason as
# OPTIONAL_ENDPOINT_LABELS - `__init__.py` also needs it, and it shares
# that same repair-issue tracking/translation key (see
# `coordinator.py::_degrade_core_payload`).
#
# BUG FIX (issue #5, reported live): a preschool-account login only has
# the Wiadomości module enabled - `Attendances/Types` 403'd (a module this
# account type genuinely doesn't have, confirmed by the reporter checking
# the real Synergia web UI independently) and took down the ENTIRE setup,
# even though Grades/Attendances/etc. simply don't apply to that account
# and Wiadomości (what the reporter actually needed) would have worked
# fine. Previously only `Timetables` had this "confirmed 403 = module
# unavailable, degrade gracefully" treatment (issue #4); generalized here
# to the whole core tier since the SAME class of report would otherwise
# just recur with a different endpoint name for the next limited-access
# account type. (`Attendances/Types` and `Grades/Categories` have since
# moved to the daily reference data - lookups that change a few times a
# year - see REFERENCE_DATA_ENDPOINT_LABELS.)
#
# NOTE: the two "Timetable (this week/next week)" entries below
# structurally can never show up as degraded - `_fetch_timetable_or_
# unpublished` already catches its own confirmed 403 and returns `{}`
# BEFORE `_degrade_core_payload` ever sees an exception for those two
# gather positions, so they'll only ever be observed as a (harmless,
# no-op) "recovery". The REAL Timetable tracking happens under the
# plain "Timetable" label (singular, no week suffix) inside
# `_fetch_timetable_or_unpublished` itself - see
# MISC_DEGRADABLE_ENDPOINT_LABELS below. Kept here anyway (rather than
# reworking the gather's positional zip) since a stray "recovered" call
# for a label that was never marked failing is a complete no-op.
CORE_ENDPOINT_LABELS = (
    "Grades",
    "Notes",
    "Attendances",
    "Timetable (this week)",
    "Timetable (next week)",
    "HomeWorks",
    "SchoolNotices",
)

# Labels for `_async_refresh_reference_data`'s own 10-call gather, in the
# exact order that gather lists them - subject/teacher/classroom/school/
# class/homework-category/free-days-x2/note-category/behaviour-grade-
# category lookups. Public (moved here from coordinator.py, code review)
# for the same __init__.py-cleanup reason as the other *_LABELS tuples -
# this tier's degrades are now tracked the same way as core/supplementary
# (see coordinator.py::_degrade_reference_result) after a real gap: an
# account with several reference-data endpoints failing (e.g. Classes,
# while Schools worked) was invisible in diagnostics before this, which
# is exactly the account this whole degrade-tracking feature was built to
# debug (issue #5's live feedback round).
REFERENCE_DATA_ENDPOINT_LABELS = (
    "Subjects",
    "Teachers",
    "Classrooms",
    "Schools",
    "Classes",
    "HomeworkCategories",
    "SchoolFreeDays",
    "ClassFreeDays",
    "NoteCategories",
    "BehaviourGradeCategories",
    "Lessons",
    # Text-grade and homework-assignment category names.
    "TextGradeCategories",
    "HomeworkAssignmentCategories",
    # School configuration - only `GradesSettings.PointGradesEnabled` is
    # read, to skip the point-grade requests at schools without them.
    "Units",
    # The school's grade scale: what "+" and "-" add or take away.
    "GradingSystem",
    # Grade categories (name, weight, counts to the average) and attendance
    # types (absence / present / late ...). Read every cycle until 0.12.5;
    # they change a few times a year. A grade or attendance record with an
    # id that isn't known yet makes the coordinator ask again in the same
    # cycle (`_async_resolve_missing_lookups`), so a new category doesn't
    # wait for the next day.
    "Grades/Categories",
    "Attendances/Types",
)

# The handful of degradable fetches that don't belong to any of the three
# gather-based tiers above - each one guards its own single call (or
# small asyncio.gather()) with its own try/except rather than a shared
# gather, but still feeds the SAME degraded_endpoints/repair-issue
# tracking. "Timetable" is the ACTUAL tracked label for timetable
# degrades (see CORE_ENDPOINT_LABELS' own note above for why the two
# per-week labels there are dead weight). "Messages" covers the bootstrap
# + inbox/unread-count fetch; "Messages/Secondary" covers the
# substitutions/alerts/justifications/outbox mailboxes, tracked separately since
# `_async_get_messages` already isolates that failure from the primary
# inbox fetch (see its own docstring - the v0.4.13 all-or-nothing bug).
MISC_DEGRADABLE_ENDPOINT_LABELS = (
    "Timetable",
    "LuckyNumbers",
    "Messages",
    "Messages/Secondary",
    # Synergia's informacja web page (class register number).
    "Informacja",
    # Point grades (0-100 / points out of a maximum) - fetched every cycle
    # only where Units doesn't say the school has them off.
    "PointGrades",
    "PointGrades/Categories",
    # Absence justifications the parent submitted.
    "Justifications",
    # Text grades, lesson topics, school trips and documents.
    "BaseTextGrades",
    "Realizations",
    "SchoolTrips",
    "SchoolFiles",
    # Descriptive grades' comments and skill names - only fetched when the
    # student has descriptive grades.
    "DescriptiveGrades/Comments",
    "DescriptiveGrades/Skills",
    # The new descriptive grading (grade 1 at some schools from 2026), the
    # child's LID it needs and the LID -> subject lookup.
    "PartialGrades",
    "StudentIdentifier",
    "Auth/Subjects",
)

# Repair issue translation keys - see repairs.py for what each one means and
# when it's raised/cleared.
ISSUE_SCHOOL_YEAR_ROLLOVER = "school_year_rollover"
ISSUE_OPTIONAL_ENDPOINT_DEGRADED = "optional_endpoint_degraded"

# The daily lucky number ("szczęśliwy numerek") is normally published by this
# local hour; the coordinator avoids re-polling it before then once today's
# value is already cached. Mirrors ha-suunto's approach of special-casing a
# single slow-moving field inside the normal coordinator instead of adding a
# second one.
LUCKY_NUMBER_PUBLISH_HOUR = 15

# New-item bus events. What has been announced is saved in the entry's
# state Store (see coordinator.async_restore_state), so items that arrive
# while Home Assistant is off still fire after a restart.
EVENT_NEW_GRADE = f"{DOMAIN}_new_grade"
EVENT_NEW_ANNOUNCEMENT = f"{DOMAIN}_new_announcement"
EVENT_NEW_NOTE = f"{DOMAIN}_new_note"
EVENT_NEW_MESSAGE = f"{DOMAIN}_new_message"
# Fires when a recipient reads a message you sent (Wiadomości read
# receipts, `outbox/messages/<id>` -> `receivers[].readed`). Only for
# messages sent in the last READ_RECEIPT_DAYS days.
EVENT_MESSAGE_READ = f"{DOMAIN}_message_read"
# Fires for a new entry in the Agenda ("HomeWorks") feed - tests, trips,
# events. Carries the resolved subject + category name so an automation
# can filter e.g. category == "Sprawdzian" without its own lookup.
EVENT_NEW_HOMEWORK = f"{DOMAIN}_new_homework"
# Fires when an upcoming Agenda entry is changed (`kind: changed`, with
# `changed_fields` and the `previous` values - e.g. a test moved to another
# day) or disappears from Librus (`kind: removed` - e.g. a cancelled trip).
# Only for entries dated today or later; seeded silently on the first sync.
EVENT_AGENDA_CHANGED = f"{DOMAIN}_agenda_changed"
# Fires when the school decides on a submitted absence justification (its
# status changes, e.g. to accepted or rejected). Seeded silently.
EVENT_JUSTIFICATION_STATUS = f"{DOMAIN}_justification_status"
# A new school trip / a new document the school shared with parents.
# Seeded silently on the first sync.
EVENT_NEW_SCHOOL_TRIP = f"{DOMAIN}_new_school_trip"
EVENT_NEW_SCHOOL_DOCUMENT = f"{DOMAIN}_new_school_document"
# Fires for a new real homework assignment ("zadanie domowe", the
# `HomeWorkAssignments` endpoint) - distinct from EVENT_NEW_HOMEWORK, which
# despite its name covers the Agenda feed. Carries topic/text/due date, the
# teacher, and the subject inferred from the teacher (see coordinator.py::
# infer_subject_id). Seeded silently on the first sync.
EVENT_NEW_HOMEWORK_ASSIGNMENT = f"{DOMAIN}_new_homework_assignment"
# Fires for a newly-seen real absence record (excused or not - `excused`
# in the payload says which). Seeded silently on the first sync.
EVENT_NEW_ABSENCE = f"{DOMAIN}_new_absence"
# Fires when a lesson on today's date or later newly turns up cancelled or
# as a substitution vs. the previous poll (seeded silently on the first
# sync, same as the *_new_* events). Signature-keyed on date+period+kind,
# so re-announcing the same known disruption every cycle doesn't happen.
EVENT_TIMETABLE_CHANGED = f"{DOMAIN}_timetable_changed"
# Fires for a handful of objective, data-derived gamification milestones
# (first 6, N good grades in a row, N days without an absence/negative
# note - see coordinator.py::_check_achievements) - deliberately NOT an
# invented points/scoring system, which would have no basis in anything
# Librus actually reports. Each achievement key fires at most once (seeded
# silently on the first sync, same as every other *_new_*/_changed event).
EVENT_ACHIEVEMENT_UNLOCKED = f"{DOMAIN}_achievement_unlocked"
# Fires when a subject's forecast grade (forecast.py) moves up or down a
# grade vs. the previous poll. Seeded silently on the first sync and again
# when the basis switches from the first semester to the school year.
EVENT_FORECAST_CHANGED = f"{DOMAIN}_forecast_changed"

# Fired after every successful weekly AI summary (see ai_summary.py),
# carrying the whole result plus labels in the HA language for a report.
EVENT_WEEKLY_SUMMARY = f"{DOMAIN}_weekly_summary"

ATTR_SUBJECT_ID = "subject_id"
