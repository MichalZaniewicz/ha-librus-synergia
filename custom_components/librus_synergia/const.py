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
# Fires when a lesson on today's date or later newly turns up cancelled or
# as a substitution vs. the previous poll (seeded silently on the first
# sync, same as the *_new_* events). Signature-keyed on date+period+kind,
# so re-announcing the same known disruption every cycle doesn't happen.
EVENT_TIMETABLE_CHANGED = f"{DOMAIN}_timetable_changed"

ATTR_SUBJECT_ID = "subject_id"
