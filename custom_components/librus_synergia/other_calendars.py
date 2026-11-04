"""Other Home Assistant calendars picked under Configure (Other calendars with
activities): activities Librus doesn't know about - an after-school club,
day-care, a sports class (cards issue #13). Read by the timetable calendar
and the School day end sensor."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.calendar import CalendarEntity
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# How long one of the other calendars may take to answer.
OTHER_CALENDAR_TIMEOUT = 15


@dataclass(frozen=True, slots=True)
class OtherActivity:
    """A timed event of another calendar, in local time."""

    entity_id: str
    source: str
    start: datetime
    end: datetime
    summary: str
    location: str | None
    uid: str | None


async def async_other_activities(
    hass: HomeAssistant, entity_ids: Iterable[str], start: datetime, end: datetime
) -> list[OtherActivity]:
    """The timed events in [start, end) of the given calendars. All-day
    events are left out, and so is any calendar of this integration (no
    loops). A calendar that's gone, fails or doesn't answer in time is
    skipped."""
    component = hass.data.get("calendar")
    if component is None:
        return []
    found: list[OtherActivity] = []
    for entity_id in entity_ids:
        entity = component.get_entity(entity_id)
        platform = getattr(entity, "platform", None)
        if not isinstance(entity, CalendarEntity) or (
            platform is not None and platform.platform_name == DOMAIN
        ):
            continue
        try:
            async with asyncio.timeout(OTHER_CALENDAR_TIMEOUT):
                events = await entity.async_get_events(hass, start, end)
        except Exception as err:  # noqa: BLE001 - another integration's calendar
            _LOGGER.debug("Skipping the other calendar %s: %r", entity_id, err)
            continue
        state = hass.states.get(entity_id)
        source = state.name if state is not None else entity_id
        for event in events:
            if not isinstance(event.start, datetime) or not isinstance(event.end, datetime):
                continue
            event_start = dt_util.as_local(event.start)
            event_end = dt_util.as_local(event.end)
            if event_end <= event_start or not (event_start < end and event_end > start):
                continue
            found.append(
                OtherActivity(
                    entity_id=entity_id,
                    source=source,
                    start=event_start,
                    end=event_end,
                    summary=event.summary,
                    location=event.location,
                    uid=event.uid,
                )
            )
    return found
