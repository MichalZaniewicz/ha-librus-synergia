"""Readable logbook entries for the integration's bus events.

"Ola: new grade 5 - Matematyka (Sprawdzian)" in the logbook and on the
student's device page (the logbook matches events to a device by the
`device_id` in their data), instead of nothing. Polish when Home
Assistant's language is Polish, English otherwise - the logbook has no
translation step for these lines.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ICON,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant, callback

from .const import (
    DOMAIN,
    EVENT_ACHIEVEMENT_UNLOCKED,
    EVENT_AGENDA_CHANGED,
    EVENT_FORECAST_CHANGED,
    EVENT_GRADE_CHANGED,
    EVENT_JUSTIFICATION_STATUS,
    EVENT_LIBRARY_CHANGED,
    EVENT_MESSAGE_READ,
    EVENT_NEW_ABSENCE,
    EVENT_NEW_ANNOUNCEMENT,
    EVENT_NEW_GRADE,
    EVENT_NEW_HOMEWORK,
    EVENT_NEW_HOMEWORK_ASSIGNMENT,
    EVENT_NEW_MESSAGE,
    EVENT_NEW_NOTE,
    EVENT_NEW_SCHOOL_DOCUMENT,
    EVENT_NEW_SCHOOL_TRIP,
    EVENT_TIMETABLE_CHANGED,
    EVENT_WEEKLY_SUMMARY,
)

_MAX_TEXT = 80


def _short(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1].rstrip() + "…"


def _day(value: Any, pl: bool) -> str:
    """'02.11' in Polish, '2026-11-02' otherwise; anything else as it came."""
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value or "")
    return parsed.strftime("%d.%m") if pl else parsed.isoformat()


def _join(*parts: Any) -> str:
    return " - ".join(str(p) for p in parts if p not in (None, ""))


def _paren(value: Any) -> str:
    return f" ({value})" if value not in (None, "") else ""


def _grade(d: Mapping[str, Any], pl: bool) -> str:
    kind = d.get("kind")
    if kind == "behaviour":
        grade = d.get("name") or d.get("value")
        return f"ocena z zachowania: {grade}" if pl else f"behaviour grade: {grade}"
    value = d.get("value")
    if kind == "point" and d.get("max_points") is not None:
        value = f"{value}/{d.get('max_points')}"
    head = "nowa ocena" if pl else "new grade"
    return f"{head} {_join(value, d.get('subject'))}{_paren(d.get('category'))}"


def _grade_change(d: Mapping[str, Any], pl: bool) -> str:
    def shown(fields: Mapping[str, Any]) -> Any:
        value = fields.get("value")
        if d.get("kind") == "behaviour":
            return fields.get("name") or value
        if d.get("kind") == "point" and fields.get("max_points") is not None:
            return f"{value}/{fields.get('max_points')}"
        return value

    what = f"{_join(d.get('subject'))}{_paren(d.get('category'))}"
    if d.get("change") == "removed":
        head = "usunięta ocena" if pl else "grade removed"
        return f"{head} {_join(shown(d), what)}".rstrip()
    previous = {**d, **(d.get("previous") or {})}
    head = "zmieniona ocena" if pl else "grade changed"
    if shown(previous) != shown(d):
        return f"{head} {shown(previous)} → {_join(shown(d), what)}"
    return f"{head} {_join(shown(d), what)}".rstrip()


def _note(d: Mapping[str, Any], pl: bool) -> str:
    sentiment = d.get("sentiment") if d.get("sentiment") in ("positive", "negative") else "neutral"
    label = {
        "positive": ("uwaga pozytywna", "positive note"),
        "negative": ("uwaga negatywna", "negative note"),
        "neutral": ("uwaga", "note"),
    }[sentiment][0 if pl else 1]
    return f"{label}: {_short(d.get('text'))}"


def _absence(d: Mapping[str, Any], pl: bool) -> str:
    if pl:
        label = "nieobecność usprawiedliwiona" if d.get("excused") else "nieobecność nieusprawiedliwiona"
        lesson = f", lekcja {d['lesson_no']}" if d.get("lesson_no") else ""
    else:
        label = "excused absence" if d.get("excused") else "unexcused absence"
        lesson = f", lesson {d['lesson_no']}" if d.get("lesson_no") else ""
    return f"{label}: {_day(d.get('date'), pl)}{lesson}"


def _message(d: Mapping[str, Any], pl: bool) -> str:
    sender = d.get("sender")
    if pl:
        head = f"nowa wiadomość od {sender}" if sender else "nowa wiadomość"
    else:
        head = f"new message from {sender}" if sender else "new message"
    return f"{head}: {_short(d.get('topic'))}"


def _announcement(d: Mapping[str, Any], pl: bool) -> str:
    return f"{'nowe ogłoszenie' if pl else 'new announcement'}: {_short(d.get('subject'))}"


def _agenda(d: Mapping[str, Any], pl: bool) -> str:
    head = "nowy wpis w terminarzu" if pl else "new Agenda entry"
    what = _join(d.get("category"), d.get("subject")) or _short(d.get("content"))
    return f"{head}: {what}, {_day(d.get('date'), pl)}"


def _homework(d: Mapping[str, Any], pl: bool) -> str:
    head = "nowe zadanie domowe" if pl else "new homework"
    due = d.get("due_date")
    due_text = f" ({'na' if pl else 'due'} {_day(due, pl)})" if due else ""
    return f"{head}: {_join(d.get('subject'), _short(d.get('topic') or d.get('text')))}{due_text}"


_CHANGES = {
    "canceled": ("odwołana lekcja", "lesson cancelled"),
    "substitution": ("zastępstwo", "substitution"),
    "room_change": ("zmiana sali", "room change"),
    "moved": ("przeniesiona lekcja", "lesson moved"),
}


def _timetable(d: Mapping[str, Any], pl: bool) -> str:
    change = d.get("change") or d.get("kind") or "substitution"
    label = _CHANGES.get(change, _CHANGES["substitution"])[0 if pl else 1]
    lesson = d.get("lesson_no")
    when = _day(d.get("date"), pl)
    if lesson:
        when += f", {'lekcja' if pl else 'lesson'} {lesson}"
    return f"{label}: {_join(d.get('subject'), when)}"


def _agenda_change(d: Mapping[str, Any], pl: bool) -> str:
    removed = d.get("kind") == "removed"
    if pl:
        head = "usunięty wpis w terminarzu" if removed else "zmieniony wpis w terminarzu"
    else:
        head = "Agenda entry removed" if removed else "Agenda entry changed"
    what = _join(d.get("category"), d.get("subject")) or _short(d.get("content"))
    return f"{head}: {what}, {_day(d.get('date'), pl)}"


def _justification(d: Mapping[str, Any], pl: bool) -> str:
    if d.get("accepted"):
        label = ("usprawiedliwienie przyjęte", "justification accepted")
    elif d.get("rejected"):
        label = ("usprawiedliwienie odrzucone", "justification rejected")
    else:
        label = ("usprawiedliwienie: nowy status", "justification status changed")
    start, end = _day(d.get("date_from"), pl), _day(d.get("date_to"), pl)
    span = start if not end or end == start else f"{start}–{end}"
    return f"{label[0 if pl else 1]}: {span}"


def _forecast(d: Mapping[str, Any], pl: bool) -> str:
    head = "prognoza" if pl else "forecast"
    move = f"{d.get('old')} → {d.get('new')}"
    return f"{head}: {_join(d.get('subject'), move)}"


def _trip(d: Mapping[str, Any], pl: bool) -> str:
    head = "nowa wycieczka" if pl else "new school trip"
    return f"{head}: {_short(d.get('destination'))}, {_day(d.get('date_from'), pl)}"


def _document(d: Mapping[str, Any], pl: bool) -> str:
    return f"{'nowy dokument szkoły' if pl else 'new school document'}: {_short(d.get('name'))}"


def _library(d: Mapping[str, Any], pl: bool) -> str:
    returned = d.get("action") == "returned"
    if pl:
        head = "oddana do biblioteki" if returned else "wypożyczona z biblioteki"
    else:
        head = "returned to the library" if returned else "borrowed from the library"
    return f"{head}: {_short(d.get('title'))}"


def _read(d: Mapping[str, Any], pl: bool) -> str:
    who = d.get("receiver")
    if pl:
        head = f"wiadomość przeczytana przez {who}" if who else "wiadomość przeczytana"
    else:
        head = f"{who} read your message" if who else "your message was read"
    return f"{head}: {_short(d.get('topic'))}"


def _achievement(d: Mapping[str, Any], pl: bool) -> str:
    return f"{'zdobyta odznaka' if pl else 'badge earned'}: {d.get('title') or d.get('id')}"


def _summary(d: Mapping[str, Any], pl: bool) -> str:
    audience = d.get("audience")
    if pl:
        who = {"parent": " (dla rodzica)", "student": " (dla ucznia)"}.get(audience, "")
        head = f"podsumowanie tygodnia gotowe{who}"
    else:
        who = {"parent": " (for the parent)", "student": " (for the student)"}.get(audience, "")
        head = f"weekly summary ready{who}"
    return f"{head}: {_short(d.get('headline'))}" if d.get("headline") else head


# event -> (message builder, icon)
DESCRIBERS: dict[str, tuple[Callable[[Mapping[str, Any], bool], str], str]] = {
    EVENT_NEW_GRADE: (_grade, "mdi:numeric-6-box-outline"),
    EVENT_NEW_NOTE: (_note, "mdi:note-text-outline"),
    EVENT_NEW_ABSENCE: (_absence, "mdi:account-cancel-outline"),
    EVENT_NEW_MESSAGE: (_message, "mdi:email-outline"),
    EVENT_NEW_ANNOUNCEMENT: (_announcement, "mdi:bullhorn-outline"),
    EVENT_NEW_HOMEWORK: (_agenda, "mdi:calendar-plus"),
    EVENT_NEW_HOMEWORK_ASSIGNMENT: (_homework, "mdi:notebook-edit-outline"),
    EVENT_TIMETABLE_CHANGED: (_timetable, "mdi:calendar-alert"),
    EVENT_AGENDA_CHANGED: (_agenda_change, "mdi:calendar-edit"),
    EVENT_GRADE_CHANGED: (_grade_change, "mdi:pencil-circle-outline"),
    EVENT_JUSTIFICATION_STATUS: (_justification, "mdi:file-check-outline"),
    EVENT_FORECAST_CHANGED: (_forecast, "mdi:crystal-ball"),
    EVENT_NEW_SCHOOL_TRIP: (_trip, "mdi:bus-school"),
    EVENT_NEW_SCHOOL_DOCUMENT: (_document, "mdi:file-document-outline"),
    EVENT_LIBRARY_CHANGED: (_library, "mdi:bookshelf"),
    EVENT_MESSAGE_READ: (_read, "mdi:email-open-outline"),
    EVENT_ACHIEVEMENT_UNLOCKED: (_achievement, "mdi:trophy-outline"),
    EVENT_WEEKLY_SUMMARY: (_summary, "mdi:robot-outline"),
}


def describe(event_type: str, data: Mapping[str, Any], language: str | None) -> dict[str, str]:
    """The logbook entry for one of the integration's events."""
    build, icon = DESCRIBERS[event_type]
    pl = (language or "en").lower().startswith("pl")
    return {
        LOGBOOK_ENTRY_NAME: str(data.get("student") or "Librus"),
        LOGBOOK_ENTRY_MESSAGE: build(data, pl),
        LOGBOOK_ENTRY_ICON: icon,
    }


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe the integration's events for the logbook."""

    @callback
    def _describe(event: Event) -> dict[str, str]:
        return describe(event.event_type, event.data, hass.config.language)

    for event_type in DESCRIBERS:
        async_describe_event(DOMAIN, event_type, _describe)
