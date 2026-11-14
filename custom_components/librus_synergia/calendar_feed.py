"""A subscribable calendar feed (.ics) per student.

`GET /api/librus_synergia/calendar/<token>.ics` answers with an iCalendar file
anyone with the link can add to their phone or Google Calendar - a
grandparent or the other parent, without a Home Assistant account. It holds
this week's and next week's timetable, the Agenda (tests, trips, meetings),
the days off and the school announcements. Not the absences, the grades or
the messages, and not the events of the family calendars picked under
"Other calendars with activities" (they're the family's own calendars).

Only while "Calendar link for others" is on under Configure. The token is a
random 32-byte secret in the entry's data (CONF_CALENDAR_FEED_TOKEN): no
login, so the link itself is the key. Turning the option off removes the
token (the link answers 404); turning it on again makes a new one.

Built from the integration's own calendar entities (so the merge/emoji
options apply) - no request to Librus: the timetable is limited to the two
weeks the integration polls anyway, so a phone checking the feed every hour
never makes it fetch other weeks.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from http import HTTPStatus

from aiohttp import web
from homeassistant.components.calendar import CalendarEvent
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import dt as dt_util

from .const import CONF_CALENDAR_FEED_ENABLED, CONF_CALENDAR_FEED_TOKEN, DOMAIN

_LOGGER = logging.getLogger(__name__)

URL = f"/api/{DOMAIN}/calendar/{{token}}"
_SUFFIX = ".ics"

# (calendar key, label in CATEGORIES, days back, days ahead). The timetable
# window is cut to the polled weeks below.
_SOURCES = (
    ("timetable", "Plan lekcji", 0, 0),
    ("agenda", "Terminarz", 30, 180),
    ("free_days", "Dni wolne", 30, 365),
    ("announcements", "Ogłoszenia", 30, 120),
)
# Events of the "Other calendars with activities" (calendar.py) - the
# family's own calendars, not school data.
_OTHER_CALENDAR_UID = "librus-extra-calendar-"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def feed_path(token: str) -> str:
    return f"/api/{DOMAIN}/calendar/{token}{_SUFFIX}"


def feed_url(hass: HomeAssistant, token: str) -> str:
    """The full link: the external address when Home Assistant has one (a
    phone outside the home network needs it), else the internal one."""
    try:
        base = get_url(hass, allow_internal=False, prefer_external=True)
    except NoURLAvailableError:
        try:
            base = get_url(hass)
        except NoURLAvailableError:
            base = ""
    return f"{base}{feed_path(token)}"


# --- iCalendar text -------------------------------------------------------


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Lines longer than 75 octets go on in a continuation line starting
    with a space (RFC 5545 3.1), never splitting a UTF-8 character."""
    out: list[str] = []
    current = ""
    size = 0
    for char in line:
        width = len(char.encode("utf-8"))
        limit = 75 if not out else 74
        if size + width > limit:
            out.append(current)
            current, size = "", 0
        current += char
        size += width
    out.append(current)
    return "\r\n ".join(out)


def _when(value: date | datetime) -> str:
    if isinstance(value, datetime):
        return ":" + dt_util.as_utc(value).strftime("%Y%m%dT%H%M%SZ")
    return ";VALUE=DATE:" + value.strftime("%Y%m%d")


def _uid(source: str, event: CalendarEvent) -> str:
    if event.uid:
        return f"{event.uid}@{DOMAIN}"
    raw = f"{source}|{event.start}|{event.summary}"
    return f"{source}-{hashlib.sha1(raw.encode()).hexdigest()[:16]}@{DOMAIN}"


def build_ics(name: str, events: Iterable[tuple[str, str, CalendarEvent]], now: datetime) -> str:
    """The whole .ics text. `events` holds (source key, category, event)."""
    stamp = dt_util.as_utc(now).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Librus Synergia (unofficial)//Home Assistant//PL",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(name)}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    for source, category, event in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{_uid(source, event)}",
            f"DTSTAMP:{stamp}",
            f"DTSTART{_when(event.start)}",
            f"DTEND{_when(event.end)}",
            f"SUMMARY:{_escape(event.summary or '')}",
            f"CATEGORIES:{_escape(category)}",
        ]
        if event.description:
            lines.append(f"DESCRIPTION:{_escape(event.description)}")
        if event.location:
            lines.append(f"LOCATION:{_escape(event.location)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


# --- the feed --------------------------------------------------------------


def _feed_name(hass: HomeAssistant, entry) -> str:
    """The student's device name as the user sees it (renamed or not)."""
    for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id):
        if (DOMAIN, entry.entry_id) in device.identifiers and (device.name_by_user or device.name):
            return device.name_by_user or device.name
    return entry.title


async def async_feed_events(
    hass: HomeAssistant, entry_id: str, today: date
) -> list[tuple[str, str, CalendarEvent]]:
    """Every event the feed holds, from this entry's calendar entities."""
    component = hass.data.get("calendar")
    registry = er.async_get(hass)
    found: list[tuple[str, str, CalendarEvent]] = []
    monday = today - timedelta(days=today.weekday())
    for key, category, back, ahead in _SOURCES:
        entity_id = registry.async_get_entity_id("calendar", DOMAIN, f"{entry_id}_{key}")
        entity = component.get_entity(entity_id) if component and entity_id else None
        if entity is None:
            continue
        if key == "timetable":
            start_day, end_day = monday, monday + timedelta(days=14)
        else:
            start_day, end_day = today - timedelta(days=back), today + timedelta(days=ahead)
        try:
            events = await entity.async_get_events(
                hass, dt_util.start_of_local_day(start_day), dt_util.start_of_local_day(end_day)
            )
        except Exception:  # noqa: BLE001 - one calendar must not break the feed
            _LOGGER.warning("Calendar feed: could not read %s", entity_id, exc_info=True)
            continue
        found.extend(
            (key, category, event)
            for event in events
            if not (event.uid or "").startswith(_OTHER_CALENDAR_UID)
        )
    return found


def _entry_for_token(hass: HomeAssistant, token: str):
    for entry in hass.config_entries.async_entries(DOMAIN):
        stored = entry.data.get(CONF_CALENDAR_FEED_TOKEN)
        if (
            stored
            and secrets.compare_digest(str(stored), token)
            and entry.options.get(CONF_CALENDAR_FEED_ENABLED)
            and entry.state is ConfigEntryState.LOADED
        ):
            return entry
    return None


class LibrusCalendarFeedView(HomeAssistantView):
    """The .ics feed. No Home Assistant login: the token in the link is the
    key, and an unknown or switched-off one gets a plain 404."""

    url = URL
    name = f"api:{DOMAIN}:calendar_feed"
    requires_auth = False

    async def get(self, request: web.Request, token: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        token = token.removesuffix(_SUFFIX)
        entry = _entry_for_token(hass, token) if token else None
        if entry is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="Not found")
        now = dt_util.now()
        events = await async_feed_events(hass, entry.entry_id, now.date())
        body = build_ics(_feed_name(hass, entry), events, now)
        return web.Response(
            text=body,
            content_type="text/calendar",
            charset="utf-8",
            headers={
                "Content-Disposition": 'inline; filename="librus.ics"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )


def async_register_calendar_feed_view(hass: HomeAssistant) -> None:
    """Once per Home Assistant run (views can't be unregistered)."""
    if hass.data.get(f"{DOMAIN}_calendar_feed_view"):
        return
    hass.http.register_view(LibrusCalendarFeedView())
    hass.data[f"{DOMAIN}_calendar_feed_view"] = True
