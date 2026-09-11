"""Constants for the Librus Synergia (unofficial) integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "librus_synergia"

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.CALENDAR]

# The session (see librus_api.LibrusSessionData) is cookie-based with a
# ~24h lifetime and no separate refresh grant - unlike a bearer-token API,
# staying logged in silently requires the password, so (unlike ha-suunto's
# revocable-session-key-only model) it is persisted here too. Only the
# cookie jar and login timestamp are the model's *addition* over a plain
# password store - see librus_api/client.py's class docstring.
CONF_COOKIES = "cookies"
CONF_SESSION_LOGGED_IN_AT = "session_logged_in_at"

DEFAULT_SCAN_INTERVAL_MINUTES = 20
MIN_SCAN_INTERVAL_MINUTES = 10
MAX_SCAN_INTERVAL_MINUTES = 180

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

# Labels for the SUPPLEMENTARY (tier 2, `return_exceptions=True`) endpoints
# fetched by `coordinator.py::_async_fetch_core_payloads`, in the exact
# order passed to that method's second `asyncio.gather()` call - used for
# the warning logged when one fails, and for the matching repair-issue id
# (see `coordinator.py::optional_endpoint_issue_id`). Keep in sync with
# that gather() call. Public (not underscore-prefixed) since `__init__.py`
# also needs it, to clear any repair issues for a removed config entry.
OPTIONAL_ENDPOINT_LABELS = (
    "Grades/Comments",
    "HomeWorkAssignments",
    "BehaviourGrades/Points",
    "BehaviourGrades/Points/Comments",
    "DescriptiveGrades",
    "ParentTeacherConferences",
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

# New-item bus events. Seen-id bookkeeping is in-memory only (see
# coordinator.py's `_fire_for_new_ids`) - a HA restart just re-seeds
# quietly, so there's nothing to prune across restarts.
EVENT_NEW_GRADE = f"{DOMAIN}_new_grade"
EVENT_NEW_ANNOUNCEMENT = f"{DOMAIN}_new_announcement"
EVENT_NEW_NOTE = f"{DOMAIN}_new_note"
EVENT_NEW_MESSAGE = f"{DOMAIN}_new_message"
# Fires for a new entry in the Agenda ("HomeWorks") feed - tests, trips,
# events. Carries the resolved subject + category name so an automation
# can filter e.g. category == "Sprawdzian" without its own lookup.
EVENT_NEW_HOMEWORK = f"{DOMAIN}_new_homework"
# Fires for a newly-seen real absence record (excused or not - `excused`
# in the payload says which). Seeded silently on the first sync.
EVENT_NEW_ABSENCE = f"{DOMAIN}_new_absence"
# Fires when a lesson on today's date or later newly turns up cancelled or
# as a substitution vs. the previous poll (seeded silently on the first
# sync, same as the *_new_* events). Signature-keyed on date+period+kind,
# so re-announcing the same known disruption every cycle doesn't happen.
EVENT_TIMETABLE_CHANGED = f"{DOMAIN}_timetable_changed"

ATTR_SUBJECT_ID = "subject_id"
