"""Constants for the Librus Synergia (unofficial) integration."""

from __future__ import annotations

from datetime import timedelta

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

# How long "seen id" bookkeeping for new-item events is kept before pruning.
SEEN_ID_RETENTION = timedelta(days=700)  # ~2 school years

EVENT_NEW_GRADE = f"{DOMAIN}_new_grade"
EVENT_NEW_ANNOUNCEMENT = f"{DOMAIN}_new_announcement"
EVENT_NEW_NOTE = f"{DOMAIN}_new_note"

ATTR_SUBJECT_ID = "subject_id"
