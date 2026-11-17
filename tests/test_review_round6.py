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


# --- optimizations ---------------------------------------------------------


def _bare_coordinator(hass, client):
    from homeassistant.config_entries import ConfigEntryState as _State

    from custom_components.librus_synergia.coordinator import LibrusDataUpdateCoordinator

    from .conftest import make_config_entry

    entry = make_config_entry()
    entry.add_to_hass(hass)
    entry.mock_state(hass, _State.SETUP_IN_PROGRESS)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


async def test_hourly_endpoints_wait_longer_at_night(hass, freezer) -> None:
    from .conftest import ORIGINAL_STRETCH

    coordinator = _bare_coordinator(hass, build_mock_client())
    hour = timedelta(hours=1)

    freezer.move_to(_FROZEN)  # ~05:00 test time: night
    assert ORIGINAL_STRETCH(coordinator, hour) == timedelta(hours=6)
    assert ORIGINAL_STRETCH(coordinator, None) is None
    assert ORIGINAL_STRETCH(coordinator, timedelta(days=1)) == timedelta(days=1)

    freezer.move_to("2026-09-09T19:00:00+00:00")  # ~12:00, no timetable known
    assert ORIGINAL_STRETCH(coordinator, hour) == hour


def test_read_receipts_are_asked_less_often_as_a_message_ages() -> None:
    from librus_synergia.models import MessageData

    from custom_components.librus_synergia.coordinator import _receipt_interval

    now = dt_util.utcnow()

    def sent(days: float) -> MessageData:
        when = dt_util.as_local(now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        return MessageData(
            id="m", sender_name="", topic="", content="", send_date=when, read_date=None,
            has_attachment=False,
        )

    assert _receipt_interval(sent(1), now) == timedelta(hours=1)
    assert _receipt_interval(sent(3), now) == timedelta(hours=6)
    assert _receipt_interval(sent(10), now) == timedelta(days=1)


async def test_conferences_and_the_far_polled_week_are_not_asked_every_cycle(
    hass, freezer
) -> None:
    freezer.move_to(_FROZEN)  # a Wednesday
    client = build_mock_client()
    coordinator = _bare_coordinator(hass, client)
    await coordinator._async_update_data()
    conferences = client.async_get_parent_teacher_conferences.await_count
    weeks = [call.args[0] for call in client.async_get_timetable.await_args_list]

    freezer.tick(timedelta(minutes=20))
    await coordinator._async_update_data()

    assert client.async_get_parent_teacher_conferences.await_count == conferences
    later = [call.args[0] for call in client.async_get_timetable.await_args_list][len(weeks):]
    today = dt_util.now().date()
    assert later == [today - timedelta(days=today.weekday())]  # this week only


async def test_status_hides_the_poll_times_while_all_is_well(hass) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.librus_synergia.const import DOMAIN

    entry = await setup_integration(hass, build_mock_client())
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_status")
    attributes = hass.states.get(entity_id).attributes

    assert hass.states.get(entity_id).state == "ok"
    assert attributes["last_success"] is None
    assert attributes["next_attempt"] is None


async def test_new_school_year_forgets_last_years_seen_ids(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    client = build_mock_client(
        async_get_grades={
            "Grades": [
                {"Id": 1, "Grade": "5", "Category": {"Id": 10}, "Subject": {"Id": 42005},
                 "Semester": 1, "AddDate": "2026-09-01"},
            ]
        }
    )
    coordinator = _bare_coordinator(hass, client)
    data = await coordinator._async_update_data()
    seen = coordinator._change_tracker.seen
    seen.grades |= {"999"}
    coordinator._known_items["late"] = {"old-1"}

    coordinator._prune_seen_for_new_year(data)

    assert coordinator._change_tracker.seen.grades == {"1"}
    assert coordinator._known_items["late"] == set()


def test_point_subject_weighs_like_a_typical_subject() -> None:
    """Four 6s in two 1-6 subjects (two grades a subject) and one point
    subject forecast at 4: it weighs two grades, not one per point grade
    and not one in all."""
    from types import SimpleNamespace

    from custom_components.librus_synergia.forecast import overall_with_points

    def grade(subject_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            value="6",
            subject_id=subject_id,
            is_semester_proposition=False,
            is_final_proposition=False,
            is_semester=False,
            is_final=False,
        )

    data = SimpleNamespace(
        grades=[grade(1), grade(1), grade(2), grade(2)],
        point_grades=[SimpleNamespace(subject_id=9, percentage=p) for p in (80, 82, 84, 86, 88)],
        grading_system=None,
    )
    forecasts = [SimpleNamespace(subject_id=9, percentage=84.0, predicted=4)]

    assert overall_with_points(6.0, data, forecasts) == round((6 * 4 + 4 * 2) / 6, 2)
    assert overall_with_points(6.0, data, []) == 6.0


async def test_average_sensors_name_their_daily_statistics(hass) -> None:
    """The Grade trend card charts the integration's daily average series -
    the recorder's state history only covers its retention window."""
    from homeassistant.helpers import entity_registry as er

    from custom_components.librus_synergia.average_history import statistic_id_prefix
    from custom_components.librus_synergia.const import DOMAIN

    client = build_mock_client(
        async_get_subjects={"Subjects": [{"Id": 42005, "Name": "Matematyka"}]},
        async_get_grades={
            "Grades": [
                {"Id": 1, "Grade": "5", "Category": {"Id": 10}, "Subject": {"Id": 42005},
                 "Semester": 1, "AddDate": "2026-09-01"},
            ]
        },
    )
    entry = await setup_integration(hass, client)
    registry = er.async_get(hass)
    prefix = statistic_id_prefix(entry.entry_id)

    overall = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_overall_average")
    maths = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_subject_42005_average")
    assert hass.states.get(overall).attributes["statistic_id"] == prefix
    assert hass.states.get(maths).attributes["statistic_id"] == f"{prefix}_42005"
