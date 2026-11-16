"""Fixes from the review after 0.12.50: the calendar link's secret in the
diagnostics, the link with a non-ASCII token, duplicate UIDs, a reloading
entry, the Names step keeping what it doesn't show, midnight in synced
copies and how far count_lessons fetches."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from unittest.mock import patch

from homeassistant.components.calendar import CalendarEvent
from homeassistant.config_entries import ConfigEntryState
from homeassistant.util import dt as dt_util

from custom_components.librus_synergia.calendar_feed import build_ics, feed_path
from custom_components.librus_synergia.calendar_sync import _hhmm
from custom_components.librus_synergia.config_flow import kept_name_lines
from custom_components.librus_synergia.const import (
    CONF_CALENDAR_FEED_TOKEN,
    CONF_GRADE_CATEGORY_NAMES,
    CONF_SUBJECT_NAMES,
)
from custom_components.librus_synergia.coordinator import COUNT_FETCH_WEEKS
from custom_components.librus_synergia.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import build_mock_client, setup_integration

# ~05:00 in the test time zone (US/Pacific): clear of both midnights.
_FROZEN = "2026-09-09T12:00:00+00:00"


async def test_diagnostics_hide_the_calendar_link_token(hass) -> None:
    entry = await setup_integration(
        hass, build_mock_client(), options={"calendar_feed_enabled": True}
    )
    token = entry.data[CONF_CALENDAR_FEED_TOKEN]

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry_data"][CONF_CALENDAR_FEED_TOKEN] != token
    assert token not in repr(diagnostics)


async def test_calendar_link_with_a_non_ascii_token_is_not_found(
    hass, hass_client_no_auth
) -> None:
    await setup_integration(hass, build_mock_client(), options={"calendar_feed_enabled": True})
    http = await hass_client_no_auth()

    assert (await http.get(feed_path("%C5%BC"))).status == 404


async def test_calendar_link_answers_try_later_while_the_entry_reloads(
    hass, hass_client_no_auth
) -> None:
    entry = await setup_integration(
        hass, build_mock_client(), options={"calendar_feed_enabled": True}
    )
    token = entry.data[CONF_CALENDAR_FEED_TOKEN]
    http = await hass_client_no_auth()

    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    response = await http.get(feed_path(token))
    # Back to loaded, so the test's teardown unloads it (and its timers).
    entry.mock_state(hass, ConfigEntryState.LOADED)

    assert response.status == 503
    assert response.headers["Retry-After"]


def test_ics_never_repeats_a_uid() -> None:
    start = datetime(2026, 9, 10, 8, 0, tzinfo=dt_util.get_time_zone("Europe/Warsaw"))
    twin = CalendarEvent(start=start, end=start + timedelta(minutes=45), summary="Język angielski")
    text = build_ics("Szkoła Oli", [("timetable", "Plan lekcji", twin)] * 2, start)

    uids = [line for line in text.split("\r\n") if line.startswith("UID:")]
    assert len(uids) == 2
    assert len(set(uids)) == 2


def test_kept_name_lines() -> None:
    text = "projekt: Project\nkartkówka: Quiz\nwycieczka: Trip\n42006: Physics\n42007: Biology"
    kept = kept_name_lines(
        text,
        shown=["kartkówka", "Fizyka"],
        unmatched=["wycieczka: Trip"],
        shown_ids=[42006],
    )
    # Not shown and matching: kept. Shown (by name or id) or unmatched: not.
    assert kept == ["projekt: Project", "42007: Biology"]


async def test_names_step_keeps_names_it_does_not_show(hass) -> None:
    """A renamed category without grades yet (not in the form) survives a
    save; a subject written by its id fills its field."""
    client = build_mock_client(
        async_get_subjects={
            "Subjects": [{"Id": 42005, "Name": "Matematyka"}, {"Id": 42006, "Name": "Fizyka"}]
        },
        async_get_grades={
            "Grades": [
                {"Id": 1, "Grade": "5", "Category": {"Id": 10}, "Subject": {"Id": 42005},
                 "Semester": 1, "AddDate": "2026-09-01"},
                {"Id": 2, "Grade": "4", "Category": {"Id": 10}, "Subject": {"Id": 42006},
                 "Semester": 1, "AddDate": "2026-09-02"},
            ]
        },
        async_get_grade_categories={
            "Categories": [
                {"Id": 10, "Name": "kartkówka", "CountToTheAverage": True, "Weight": 1},
                {"Id": 11, "Name": "projekt", "CountToTheAverage": True, "Weight": 1},
            ]
        },
    )
    entry = await setup_integration(
        hass,
        client,
        options={
            CONF_SUBJECT_NAMES: "42006: Physics",
            CONF_GRADE_CATEGORY_NAMES: "projekt: Project\nwycieczka: Trip",
        },
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "names"}
    )
    subjects = next(v for k, v in result["data_schema"].schema.items() if str(k) == "subjects")
    fizyka = next(k for k in subjects.schema.schema if str(k) == "Fizyka")
    assert fizyka.description == {"suggested_value": "Physics"}

    with patch("custom_components.librus_synergia.LibrusApiClient", return_value=client):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"subjects": {"Fizyka": "Physics"}, "categories": {"kartkówka": "Quiz"}},
        )
        await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert entry.options[CONF_SUBJECT_NAMES] == "Fizyka: Physics"
    # The unused category's name stays; the line that matched nothing goes.
    assert entry.options[CONF_GRADE_CATEGORY_NAMES] == "projekt: Project\nkartkówka: Quiz"


def test_synced_copy_takes_midnight_as_no_time() -> None:
    assert _hhmm("00:00:00") is None
    assert _hhmm("00:00") is None
    assert _hhmm("17:30:00") == time(17, 30)


async def test_count_lessons_fetches_only_the_near_weeks(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    client = build_mock_client()
    entry = await setup_integration(hass, client)
    coordinator = entry.runtime_data
    today = dt_util.now().date()
    this_week = today - timedelta(days=today.weekday())
    calls = client.async_get_timetable.await_count

    await coordinator.async_get_timetable_weeks(
        [this_week + timedelta(weeks=COUNT_FETCH_WEEKS + 2)], fetch_weeks=COUNT_FETCH_WEEKS
    )
    assert client.async_get_timetable.await_count == calls

    await coordinator.async_get_timetable_weeks(
        [this_week + timedelta(weeks=COUNT_FETCH_WEEKS)], fetch_weeks=COUNT_FETCH_WEEKS
    )
    await hass.async_block_till_done()
    assert client.async_get_timetable.await_count == calls + 1
