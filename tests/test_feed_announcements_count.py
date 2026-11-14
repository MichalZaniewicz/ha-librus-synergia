"""The announcements calendar, the calendar feed (.ics link) and the
count_lessons action."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.calendar import CalendarEvent
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.librus_synergia.calendar_feed import build_ics, feed_path
from custom_components.librus_synergia.const import CONF_CALENDAR_FEED_TOKEN, DOMAIN

from .conftest import build_mock_client, setup_integration

# ~05:00 in the test time zone (US/Pacific): clear of both midnights.
_FROZEN = "2026-09-09T12:00:00+00:00"


def _calendar(hass, entry, key: str):
    entity_id = er.async_get(hass).async_get_entity_id("calendar", DOMAIN, f"{entry.entry_id}_{key}")
    return entity_id, hass.data["calendar"].get_entity(entity_id) if entity_id else None


def _notice(notice_id: str, subject: str, start: date, end: date) -> dict:
    return {
        "Id": notice_id,
        "Subject": subject,
        "Content": "Treść ogłoszenia",
        "StartDate": start.isoformat(),
        "EndDate": end.isoformat(),
        "CreationDate": f"{start.isoformat()} 08:00:00",
        "WasRead": True,
    }


async def test_announcements_calendar_spans_the_notice_days(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    today = dt_util.now().date()
    tomorrow = today + timedelta(days=1)
    client = build_mock_client(
        async_get_school_notices={
            "SchoolNotices": [
                _notice("LID-1", "Upały - skrócone lekcje", tomorrow, tomorrow + timedelta(days=2)),
                _notice("LID-2", "Regulamin szkoły", today, today + timedelta(days=200)),
            ]
        }
    )
    entry = await setup_integration(hass, client)
    entity_id, entity = _calendar(hass, entry, "announcements")
    assert entity is not None

    events = await entity.async_get_events(
        hass,
        dt_util.start_of_local_day(today),
        dt_util.start_of_local_day(today + timedelta(days=30)),
    )
    by_summary = {event.summary: event for event in events}
    heat = by_summary["Upały - skrócone lekcje"]
    assert (heat.start, heat.end) == (tomorrow, tomorrow + timedelta(days=3))
    assert heat.uid == "librus-notice-LID-1"
    # Valid for months: its first day only, the end date in the text.
    rule = by_summary["Regulamin szkoły"]
    assert (rule.start, rule.end) == (today, today + timedelta(days=1))
    assert "Ważne do" in rule.description
    assert hass.states.get(entity_id).attributes["message"] == "Regulamin szkoły"


async def test_announcements_calendar_follows_the_announcements_option(hass) -> None:
    entry = await setup_integration(hass, build_mock_client(), options={"announcements_enabled": False})
    assert _calendar(hass, entry, "announcements")[0] is None


def test_ics_text_is_escaped_and_folded() -> None:
    start = datetime(2026, 9, 10, 8, 0, tzinfo=dt_util.get_time_zone("Europe/Warsaw"))
    events = [
        (
            "timetable",
            "Plan lekcji",
            CalendarEvent(
                start=start,
                end=start + timedelta(minutes=45),
                summary="Matematyka; zastępstwo, sala 12",
                description="Anna Nowak\nTemat: " + "ułamki zwykłe i dziesiętne " * 6,
                location="12",
                uid="x-1",
            ),
        ),
        (
            "free_days",
            "Dni wolne",
            CalendarEvent(start=date(2026, 9, 11), end=date(2026, 9, 12), summary="Dzień wolny"),
        ),
    ]
    text = build_ics("Szkoła Oli", events, start)

    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    unfolded = text.replace("\r\n ", "")
    assert "SUMMARY:Matematyka\\; zastępstwo\\, sala 12" in unfolded
    assert "DESCRIPTION:Anna Nowak\\nTemat: ułamki" in unfolded
    assert "DTSTART:20260910T060000Z" in unfolded
    assert "DTSTART;VALUE=DATE:20260911" in unfolded
    assert "DTEND;VALUE=DATE:20260912" in unfolded
    assert "UID:x-1@librus_synergia" in unfolded
    assert "X-WR-CALNAME:Szkoła Oli" in unfolded


async def test_calendar_feed_link_serves_the_calendar(hass, hass_client_no_auth, freezer) -> None:
    freezer.move_to(_FROZEN)
    tomorrow = dt_util.now().date() + timedelta(days=1)
    client = build_mock_client(
        async_get_school_free_days={
            "SchoolFreeDays": [
                {"Id": 1, "Name": "Dzień Edukacji", "DateFrom": tomorrow.isoformat(), "DateTo": tomorrow.isoformat()}
            ]
        }
    )
    entry = await setup_integration(hass, client, options={"calendar_feed_enabled": True})
    token = entry.data[CONF_CALENDAR_FEED_TOKEN]
    assert len(token) >= 40

    sensor_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_calendar_feed")
    assert hass.states.get(sensor_id).state.endswith(feed_path(token))

    http = await hass_client_no_auth()
    response = await http.get(feed_path(token))
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("text/calendar")
    body = await response.text()
    assert "BEGIN:VCALENDAR" in body
    assert "SUMMARY:Dzień Edukacji" in body

    assert (await http.get(feed_path("wrong-token"))).status == 404

    # Off: the token goes, the old link stops working, the sensor goes.
    with patch("custom_components.librus_synergia.LibrusApiClient", return_value=client):
        hass.config_entries.async_update_entry(entry, options={"calendar_feed_enabled": False})
        await hass.async_block_till_done()
    assert CONF_CALENDAR_FEED_TOKEN not in entry.data
    assert (await http.get(feed_path(token))).status == 404
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_calendar_feed") is None


async def test_no_feed_and_no_link_by_default(hass) -> None:
    entry = await setup_integration(hass, build_mock_client())
    assert CONF_CALENDAR_FEED_TOKEN not in entry.data
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_calendar_feed") is None


def _lesson(no: int, subject_id: int, *, canceled: bool = False) -> dict:
    return {
        "LessonNo": str(no),
        "HourFrom": f"{7 + no:02d}:00",
        "HourTo": f"{7 + no:02d}:45",
        "Subject": {"Id": str(subject_id)},
        "Teacher": {"Id": "500"},
        "Classroom": {"Id": "12"},
        "IsCanceled": canceled,
        "IsSubstitutionClass": False,
    }


async def test_count_lessons_uses_the_timetable_then_the_plan(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    today = dt_util.now().date()
    client = build_mock_client(
        async_get_timetable={
            "Timetable": {
                today.isoformat(): [[_lesson(1, 100, canceled=True)], [_lesson(2, 100)], [_lesson(3, 300)]]
            }
        },
        async_get_subjects={
            "Subjects": [{"Id": 100, "Name": "Matematyka"}, {"Id": 300, "Name": "Historia"}]
        },
        async_get_lessons={"Lessons": [{"Id": 501, "Subject": {"Id": 100}}]},
        async_get_timetable_entries={
            "TimetableEntries": [
                {"Id": 1, "Lesson": {"Id": 501}, "DayOfTheWeek": today.isoweekday(), "LessonNo": 1,
                 "DateFrom": "2026-09-01", "DateTo": "2027-06-25"}
            ]
        },
    )
    entry = await setup_integration(hass, client)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    call = {"device_id": device.id, "subject": "matem", "end_date": (today + timedelta(days=14)).isoformat()}

    result = await hass.services.async_call(DOMAIN, "count_lessons", call, blocking=True, return_response=True)
    assert result["subjects"] == ["Matematyka"]
    # Today from the timetable (the cancelled one left out), the same
    # weekday in the next two weeks from the standing plan.
    assert (result["count"], result["from_timetable"], result["from_plan"]) == (3, 1, 2)
    assert result["lessons"][0] == {
        "date": today.isoformat(),
        "lesson_no": 2,
        "subject": "Matematyka",
        "start": "09:00",
        "end": "09:45",
        "source": "timetable",
        "cancelled": False,
    }

    with_cancelled = await hass.services.async_call(
        DOMAIN, "count_lessons", {**call, "include_cancelled": True}, blocking=True, return_response=True
    )
    assert (with_cancelled["count"], with_cancelled["cancelled"]) == (4, 1)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "count_lessons", {**call, "subject": "chemia"}, blocking=True, return_response=True
        )
