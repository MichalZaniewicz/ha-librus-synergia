"""Copy school events into another calendar - the `sync_calendar` action.

Tests, trips, meetings (the Agenda) and days off go into a calendar of the
family's choice, e.g. Google Calendar, so everyone sees them outside Home
Assistant. Unlike a plain "add if missing" copy, every event written here
carries a tag at the end of its description:

    [librus-sync:3f9a1c:a1234]

(student, Librus item). That makes the copy keep up with Librus: a test
moved to another day is moved in the target calendar, a cancelled one is
removed, and nothing without our tag - events people added by hand, or
another child's - is ever touched. Two children can share one calendar.

Only days from today on are looked at; past events are left alone. A
target calendar that can't update or delete events (some providers only
allow adding) still gets new events; moved or cancelled ones are then
counted as `not_removable` and logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.components.calendar import CalendarEntityFeature, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from librus_synergia.models import LibrusData

from .calendar import (
    _free_day_to_event,
    _homework_to_event,
    _pt_conference_to_event,
    _pt_conferences_not_in_agenda,
)
from .subject_emoji import fold

_LOGGER = logging.getLogger(__name__)

_TAG_RE = re.compile(r"\[librus-sync:([0-9a-f]{6}):([A-Za-z0-9_.:-]+)\]")

# One run at a time per (target calendar, student): two runs side by side
# would both see an event missing and both add it.
_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


@dataclass(frozen=True, slots=True)
class SyncItem:
    """One event as it should look in the target calendar."""

    key: str
    summary: str
    start: date
    end: date  # exclusive, as for any all-day event
    description: str  # without the tag

    def tagged_description(self, student: str) -> str:
        tag = f"[librus-sync:{student}:{self.key}]"
        return f"{self.description}\n\n{tag}" if self.description else tag


def student_tag(entry_id: str) -> str:
    """Six hex characters per config entry (student) - short enough not to
    clutter the description, enough to tell a family's children apart."""
    return hashlib.sha1(entry_id.encode()).hexdigest()[:6]


def parse_categories(value: str | Iterable[str] | None) -> list[str]:
    """"sprawdzian, Kartkówka" or a list -> folded words; empty = all."""
    if value is None:
        return []
    parts = value.split(",") if isinstance(value, str) else [p for v in value for p in v.split(",")]
    return [folded for p in parts if (folded := fold(p.strip()))]


def _covers_weekday(start: date, end: date) -> bool:
    """A holiday on a Saturday or Sunday is no day off from school."""
    days = max((end - start).days, 1)
    return any((start + timedelta(days=i)).weekday() < 5 for i in range(days))


def sync_items(
    data: LibrusData,
    *,
    today: date,
    days: int,
    categories: list[str],
    free_days: bool,
    prefix: str = "",
    emoji: bool = False,
) -> list[SyncItem]:
    """What the target calendar should hold from `today` for `days` days.

    `categories` are folded words matched against the Agenda category
    (part of the name is enough, "kartk" matches "Kartkówka"); empty keeps
    every entry. Parent-teacher meetings count as "zebranie"."""
    window_end = today + timedelta(days=days)
    head = f"{prefix.strip()} " if prefix and prefix.strip() else ""
    items: list[SyncItem] = []

    def add(key: str, event: CalendarEvent | None, title_prefix: str = "") -> None:
        if event is None:
            return
        start, end = event.start, event.end
        if isinstance(start, datetime) or isinstance(end, datetime):
            return
        if not (start < window_end and end > today):
            return
        items.append(
            SyncItem(
                key=key,
                summary=f"{head}{title_prefix}{event.summary}",
                start=start,
                end=end,
                description=(event.description or "").strip(),
            )
        )

    def wanted(category: str | None) -> bool:
        if not categories:
            return True
        folded = fold(category or "")
        return any(word in folded for word in categories)

    for item in data.homeworks:
        category = (
            data.homework_categories.get(item.category_id) if item.category_id is not None else None
        )
        if wanted(category):
            add(f"a{item.id}", _homework_to_event(item, data, emoji))
    if wanted("zebranie z rodzicami"):
        for conference in _pt_conferences_not_in_agenda(data):
            add(f"p{conference.id}", _pt_conference_to_event(conference, data, emoji))
    if free_days:
        for free_day in data.free_days:
            event = _free_day_to_event(free_day)
            if event is None or not _covers_weekday(event.start, event.end):
                continue
            # A day with only some lessons off already says so in its title
            # ("Rekolekcje (lekcje 3–4)") - it isn't a day off.
            add(
                f"f{free_day.id}-{free_day.date_from[:10]}",
                event,
                "Dzień wolny: " if free_day.whole_day else "",
            )
    return sorted(items, key=lambda i: (i.start, i.summary))


def _day(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _strip_tag(description: str | None) -> str:
    return _TAG_RE.sub("", description or "").strip()


def _same(item: SyncItem, event: CalendarEvent) -> bool:
    return (
        event.summary == item.summary
        and _day(event.start) == item.start
        and _day(event.end) == item.end
        and " ".join(_strip_tag(event.description).split()) == " ".join(item.description.split())
    )


def _event_fields(item: SyncItem, student: str) -> dict[str, Any]:
    return {
        "summary": item.summary,
        "dtstart": item.start,
        "dtend": item.end,
        "description": item.tagged_description(student),
    }


async def async_sync_calendar(
    hass: HomeAssistant,
    *,
    entry_id: str,
    target_entity_id: str,
    items: list[SyncItem],
    today: date,
    days: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Bring `target_entity_id` in line with `items` for [today, today +
    days). Returns counts (and with `dry_run` the planned changes, without
    making them)."""
    component = hass.data.get("calendar")
    entity = component.get_entity(target_entity_id) if component is not None else None
    if entity is None:
        raise ServiceValidationError(f"Unknown calendar: {target_entity_id}")
    features = entity.supported_features or 0
    if not features & CalendarEntityFeature.CREATE_EVENT:
        raise ServiceValidationError(f"{target_entity_id} doesn't allow adding events.")
    can_update = bool(features & CalendarEntityFeature.UPDATE_EVENT)
    can_delete = bool(features & CalendarEntityFeature.DELETE_EVENT)
    student = student_tag(entry_id)

    lock = _LOCKS.setdefault((target_entity_id, entry_id), asyncio.Lock())
    async with lock:
        start = dt_util.start_of_local_day(today)
        end = dt_util.start_of_local_day(today + timedelta(days=days + 1))
        existing = await entity.async_get_events(hass, start, end)

        ours: dict[str, list[CalendarEvent]] = {}
        untagged: dict[tuple[str, date], CalendarEvent] = {}
        for event in existing:
            match = _TAG_RE.search(event.description or "")
            if match is None:
                untagged.setdefault((event.summary, _day(event.start)), event)
            elif match.group(1) == student:
                ours.setdefault(match.group(2), []).append(event)

        created = updated = deleted = unchanged = adopted = not_removable = errors = 0
        planned: list[dict[str, Any]] = []

        async def run(action: str, call: Callable[[], Awaitable[Any]], item_summary: str) -> bool:
            nonlocal errors
            planned.append({"action": action, "summary": item_summary})
            if dry_run:
                return True
            try:
                await call()
            except (HomeAssistantError, ValueError) as err:
                errors += 1
                _LOGGER.warning(
                    "Calendar sync to %s: could not %s %r: %s",
                    target_entity_id, action, item_summary, err,
                )
                return False
            return True

        async def remove(event: CalendarEvent) -> None:
            nonlocal deleted, not_removable
            if not can_delete or not event.uid:
                not_removable += 1
                _LOGGER.info(
                    "Calendar sync: %s can't delete events - %r on %s stays",
                    target_entity_id, event.summary, _day(event.start),
                )
                return
            uid = event.uid
            if await run("delete", partial(entity.async_delete_event, uid), event.summary):
                deleted += 1

        async def create(item: SyncItem) -> None:
            nonlocal created
            fields = _event_fields(item, student)
            if await run("create", partial(entity.async_create_event, **fields), item.summary):
                created += 1

        for item in items:
            matches = ours.pop(item.key, [])
            current, extra = (matches[0], matches[1:]) if matches else (None, [])
            for duplicate in extra:
                await remove(duplicate)
            if current is None:
                # Copied before this action existed (by the old blueprint,
                # same title and day): take it over instead of adding a
                # second one, when the calendar lets us tag it.
                legacy = untagged.pop((item.summary, item.start), None)
                if legacy is not None:
                    if can_update and legacy.uid:
                        fields = _event_fields(item, student)
                        uid = legacy.uid
                        if await run(
                            "adopt", partial(entity.async_update_event, uid, fields), item.summary
                        ):
                            adopted += 1
                    else:
                        unchanged += 1
                    continue
                await create(item)
                continue
            if _same(item, current):
                unchanged += 1
                continue
            if can_update and current.uid:
                fields = _event_fields(item, student)
                uid = current.uid
                if await run("update", partial(entity.async_update_event, uid, fields), item.summary):
                    updated += 1
            else:
                await remove(current)
                await create(item)

        # Tagged for this student but no longer in Librus (cancelled, moved
        # out of the window, or a category no longer copied).
        for leftovers in ours.values():
            for event in leftovers:
                await remove(event)

    result: dict[str, Any] = {
        "created": created,
        "updated": updated,
        "deleted": deleted,
        "adopted": adopted,
        "unchanged": unchanged,
        "not_removable": not_removable,
        "errors": errors,
    }
    if dry_run:
        result["planned"] = planned
    return result
