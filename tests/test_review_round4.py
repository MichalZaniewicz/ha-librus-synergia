"""Review round 4: the saved responses' freshness after a restart, the
school-day clock's lifetime, on-demand timetable weeks (back-off, range,
unload), diagnostics redaction, the kindergarten LID, the to-do ticks, a
refused Wiadomości bootstrap and fewer recalculations."""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.librus_synergia import coordinator as coordinator_module, school_day
from custom_components.librus_synergia.average_history import LibrusAverageHistory
from custom_components.librus_synergia.const import DOMAIN, STATE_STORE_VERSION
from custom_components.librus_synergia.coordinator import (
    ON_DEMAND_WEEKS,
    LibrusDataUpdateCoordinator,
    on_demand_week_starts,
    payload_store_key,
    state_store_key,
)
from custom_components.librus_synergia.diagnostics import async_get_config_entry_diagnostics
from custom_components.librus_synergia.school_day import SchoolDayClock
from librus_synergia import LibrusConnectionError, LibrusSessionExpiredError

from .conftest import build_mock_client, make_config_entry, setup_integration

# ~05:00 in the test time zone (US/Pacific): clear of both midnights.
_FROZEN = "2026-09-09T12:00:00+00:00"


def _coordinator(hass, client, entry=None, *, options=None) -> LibrusDataUpdateCoordinator:
    entry = entry or make_config_entry(options=options)
    if entry.state is not ConfigEntryState.SETUP_IN_PROGRESS:
        entry.add_to_hass(hass)
        entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


def _store(hass_storage, key: str, data: dict, version: int = STATE_STORE_VERSION) -> None:
    hass_storage[key] = {"version": version, "minor_version": 1, "key": key, "data": data}


def _entity_id(hass, entry, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")


def _this_week() -> date:
    today = dt_util.now().date()
    return today - timedelta(days=today.weekday())


def _forced_logins(client) -> int:
    return sum(1 for c in client.async_ensure_session_valid.call_args_list if c.kwargs.get("force"))


def _lesson(no: int, start: str, end: str, subject: int, **extra) -> dict:
    return {
        "LessonNo": str(no),
        "HourFrom": start,
        "HourTo": end,
        "Subject": {"Id": str(subject)},
        "Teacher": {"Id": "5"},
        "Classroom": {"Id": "12"},
        "IsCanceled": False,
        "IsSubstitutionClass": False,
        **extra,
    }


# ----------------------------------------------------------------------
# Saved responses after a restart (finding 1)
# ----------------------------------------------------------------------


async def test_restart_doesnt_trust_fetch_times_newer_than_the_saved_responses(
    hass, hass_storage, freezer
) -> None:
    """The tracked state (fetch times) is written soon, the responses up to
    six hours later: HA stopping in between used to leave older responses
    next to newer fetch times, and they passed as fresh after a restart. A
    fetch time after the responses file's own `saved_at` is dropped - that
    request is made again."""
    freezer.move_to(_FROZEN)
    first = _coordinator(hass, build_mock_client())
    await first._async_update_data()
    entry = make_config_entry()
    payloads = json.loads(json.dumps(first._payloads_to_save()))
    # The responses were written an hour before the last fetches.
    payloads["saved_at"] = (dt_util.utcnow() - timedelta(hours=1)).isoformat()
    _store(hass_storage, payload_store_key(entry.entry_id), payloads)
    _store(
        hass_storage,
        state_store_key(entry.entry_id),
        json.loads(json.dumps(first._tracked_to_save())),
    )

    client = build_mock_client()
    second = _coordinator(hass, client, entry)
    await second.async_restore_state()
    assert second.reference_data_fetched_at is None
    assert "Realizations" not in second._fetched_at
    await second._async_update_data()

    client.async_get_subjects.assert_called_once()
    client.async_get_realizations.assert_called_once()


async def test_restart_trusts_fetch_times_covered_by_the_saved_responses(
    hass, hass_storage, freezer
) -> None:
    freezer.move_to(_FROZEN)
    first = _coordinator(hass, build_mock_client())
    await first._async_update_data()
    entry = make_config_entry()
    _store(
        hass_storage,
        payload_store_key(entry.entry_id),
        json.loads(json.dumps(first._payloads_to_save())),
    )
    _store(
        hass_storage,
        state_store_key(entry.entry_id),
        json.loads(json.dumps(first._tracked_to_save())),
    )

    client = build_mock_client()
    second = _coordinator(hass, client, entry)
    await second.async_restore_state()
    await second._async_update_data()

    client.async_get_subjects.assert_not_called()
    client.async_get_realizations.assert_not_called()


async def test_responses_file_without_a_time_trusts_no_fetch_time(hass, hass_storage) -> None:
    """A responses file written before `saved_at` existed: its age is
    unknown, so the first cycle asks for the daily lookups again."""
    first = _coordinator(hass, build_mock_client())
    await first._async_update_data()
    entry = make_config_entry()
    payloads = json.loads(json.dumps(first._payloads_to_save()))
    del payloads["saved_at"]
    _store(hass_storage, payload_store_key(entry.entry_id), payloads)
    _store(
        hass_storage,
        state_store_key(entry.entry_id),
        json.loads(json.dumps(first._tracked_to_save())),
    )

    second = _coordinator(hass, build_mock_client(), entry)
    await second.async_restore_state()

    assert second.reference_data_fetched_at is None
    assert second._fetched_at == {}


async def test_ha_stop_writes_pending_payloads(hass, hass_storage, freezer) -> None:
    """Entries aren't unloaded when Home Assistant stops: the pending
    payloads write goes out with the store's final write."""
    freezer.move_to(_FROZEN)
    entry = make_config_entry()
    coordinator = _coordinator(hass, build_mock_client(), entry)
    await coordinator._async_update_data()
    assert coordinator._payload_save_pending

    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()

    saved = hass_storage[payload_store_key(entry.entry_id)]["data"]
    assert "Me" in saved["payloads"]
    assert saved["saved_at"]
    assert not coordinator._payload_save_pending


# ----------------------------------------------------------------------
# The school-day clock (finding 2)
# ----------------------------------------------------------------------


async def test_school_day_clock_lives_on_the_coordinator_and_stops_on_unload(hass) -> None:
    """A module-level map from coordinator to clock kept every unloaded
    coordinator alive (the clock refers back to it)."""
    entry = await setup_integration(hass, build_mock_client())
    coordinator = entry.runtime_data
    clock = coordinator.school_day_clock
    assert isinstance(clock, SchoolDayClock)
    assert not hasattr(school_day, "_CLOCKS")

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert coordinator.school_day_clock is None
    assert clock._listeners == []
    assert clock._unsub_timer is None
    assert clock._unsub_coordinator is None


# ----------------------------------------------------------------------
# On-demand timetable weeks (findings 3 and 6)
# ----------------------------------------------------------------------


async def test_failed_on_demand_week_is_not_asked_again_for_a_while(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    client = build_mock_client()
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()
    client.async_get_timetable.reset_mock()
    client.async_get_timetable.side_effect = LibrusConnectionError("timeout")
    later = _this_week() + timedelta(weeks=3)

    assert await coordinator.async_get_timetable_week(later) == {}
    assert await coordinator.async_get_timetable_week(later) == {}
    assert client.async_get_timetable.call_count == 1

    freezer.tick(timedelta(minutes=16))
    client.async_get_timetable.side_effect = None
    client.async_get_timetable.return_value = {"Timetable": {}}
    await coordinator.async_get_timetable_week(later)
    assert client.async_get_timetable.call_count == 2


async def test_on_demand_weeks_wait_while_the_polls_back_off(hass, freezer) -> None:
    """Librus is down (the polls are backing off): a dashboard paging
    months doesn't send a request (and a login) per week meanwhile."""
    freezer.move_to(_FROZEN)
    client = build_mock_client()
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()
    client.async_get_timetable.reset_mock()
    coordinator.next_attempt_at = dt_util.utcnow() + timedelta(hours=1)

    assert await coordinator.async_get_timetable_week(_this_week() + timedelta(weeks=3)) == {}
    client.async_get_timetable.assert_not_called()


async def test_on_demand_week_skips_a_second_forced_login(hass, freezer) -> None:
    """A rejected session right after a forced login that didn't help: no
    second password login for an on-demand week."""
    freezer.move_to(_FROZEN)
    client = build_mock_client()
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()
    coordinator._last_forced_login_at = dt_util.utcnow() - timedelta(minutes=2)
    client.async_get_timetable.side_effect = LibrusSessionExpiredError(
        "Session rejected (HTTP 401).", status_code=401
    )

    week = await coordinator.async_get_timetable_week(_this_week() + timedelta(weeks=3))

    assert week == {}
    assert _forced_logins(client) == 0


async def test_on_demand_weeks_are_limited_to_a_range_around_today(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    this_week = _this_week()
    weeks = on_demand_week_starts(this_week - timedelta(days=400), this_week + timedelta(days=400))
    assert len(weeks) == 2 * ON_DEMAND_WEEKS + 1
    assert weeks[0] == this_week - timedelta(weeks=ON_DEMAND_WEEKS)
    assert weeks[-1] == this_week + timedelta(weeks=ON_DEMAND_WEEKS)
    assert on_demand_week_starts(this_week + timedelta(weeks=40), this_week + timedelta(weeks=41)) == []

    client = build_mock_client()
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()
    client.async_get_timetable.reset_mock()
    far = this_week + timedelta(weeks=ON_DEMAND_WEEKS + 1)
    assert await coordinator.async_get_timetable_week(far) == {}
    client.async_get_timetable.assert_not_called()


async def test_calendar_year_range_fetches_at_most_the_allowed_weeks(hass, freezer) -> None:
    """A whole year asked for at once: only the weeks within the limit are
    fetched (the polled two come from the data when it has lessons for
    them - this account has none, so they may be asked for too)."""
    freezer.move_to(_FROZEN)
    client = build_mock_client()
    entry = await setup_integration(hass, client)
    entity_id = _entity_id(hass, entry, "calendar", "timetable")
    client.async_get_timetable.reset_mock()
    start = dt_util.start_of_local_day() - timedelta(days=365)

    await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": entity_id, "start_date_time": start, "end_date_time": start + timedelta(days=730)},
        blocking=True,
        return_response=True,
    )

    requested = [call.args[0] for call in client.async_get_timetable.call_args_list]
    allowed = set(on_demand_week_starts(start.date(), (start + timedelta(days=730)).date()))
    assert requested
    assert set(requested) <= allowed
    assert len(requested) == len(set(requested)) <= 2 * ON_DEMAND_WEEKS + 1


async def test_unload_cancels_on_demand_weeks_in_flight(hass, freezer) -> None:
    """A week still being fetched when the entry unloads is cancelled before
    the client's session is closed; the waiting calendar request gets no
    lessons instead of an error."""
    freezer.move_to(_FROZEN)
    client = build_mock_client()
    entry = await setup_integration(hass, client)
    coordinator = entry.runtime_data
    started = asyncio.Event()

    async def slow(_week):
        started.set()
        await asyncio.Event().wait()

    client.async_get_timetable.side_effect = slow
    caller = hass.async_create_task(
        coordinator.async_get_timetable_week(_this_week() + timedelta(weeks=3))
    )
    await started.wait()

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert await caller == {}
    assert coordinator._week_fetches == {}
    # Nothing is fetched any more after the unload.
    client.async_get_timetable.reset_mock()
    assert await coordinator.async_get_timetable_week(_this_week() + timedelta(weeks=4)) == {}
    client.async_get_timetable.assert_not_called()


async def test_closed_session_during_an_on_demand_week_shows_no_lessons(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    client = build_mock_client()
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()
    client.async_get_timetable.side_effect = RuntimeError("Session is closed")

    assert await coordinator.async_get_timetable_week(_this_week() + timedelta(weeks=3)) == {}


# ----------------------------------------------------------------------
# Diagnostics (finding 4)
# ----------------------------------------------------------------------


async def test_diagnostics_redact_free_text_names_and_lids_in_errors(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    today = dt_util.now().date().isoformat()
    client = build_mock_client(
        async_get_teachers={"Users": [{"Id": 5, "FirstName": "Anna", "LastName": "Nowak"}]},
        async_get_justifications={
            "data": [
                {
                    "id": 7,
                    "justificationStatus": "new",
                    "messageFromParent": "Choroba z gorączką",
                    "notifiedTeachers": [{"name": "Jan Wiśniewski"}],
                }
            ]
        },
        async_get_timetable={
            "Timetable": {
                today: [
                    [
                        _lesson(
                            1,
                            "08:00",
                            "08:45",
                            100,
                            IsSubstitutionClass=True,
                            SubstitutionNote="Za nieobecną wychowawczynię",
                        )
                    ]
                ]
            }
        },
    )
    entry = await setup_integration(hass, client)
    coordinator = entry.runtime_data
    coordinator.last_error = "Session rejected on https://x/LID-AUTH-USER-4242/Timetable"

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(diagnostics, default=str, ensure_ascii=False)

    assert "LID-AUTH-USER-4242" not in dumped
    assert "Choroba z gorączką" not in dumped
    assert "Jan Wiśniewski" not in dumped
    assert "Nowak" not in dumped
    assert "Za nieobecną wychowawczynię" not in dumped
    assert "LID-**REDACTED**" in diagnostics["connection"]["last_error"]


# ----------------------------------------------------------------------
# Kindergarten LID (finding 5)
# ----------------------------------------------------------------------


async def test_empty_kindergarten_timetable_for_a_day_drops_the_lid(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    client = build_mock_client()
    coordinator = _coordinator(hass, client)
    coordinator._kindergarten_lid = "LID-AUTH-USER-1-CHILD"
    coordinator._kindergarten_found_on = dt_util.now().date()

    await coordinator._async_update_data()
    assert coordinator.is_kindergarten
    assert coordinator._kindergarten_empty_since is not None
    client.async_get_timetable.assert_not_called()

    freezer.tick(timedelta(hours=25))
    await coordinator._async_update_data()

    assert not coordinator.is_kindergarten
    # The ordinary timetable, asked once for both polled weeks.
    assert client.async_get_timetable.call_count == 2


async def test_kindergarten_lid_from_last_school_year_is_dropped(hass, freezer) -> None:
    freezer.move_to(_FROZEN)  # September 2026
    today = dt_util.now().date()
    client = build_mock_client()
    client.async_get_kindergarten_timetable.return_value = {
        "timetableEntries": [
            {
                "identifier": "L1",
                "activityTypeIdentifier": "ACT1",
                "type": "planned",
                "date": today.isoformat(),
                "startTime": "07:00",
                "endTime": "12:00",
                "teachers": [],
            }
        ]
    }
    coordinator = _coordinator(hass, client)
    coordinator._kindergarten_lid = "LID-AUTH-USER-1-CHILD"
    coordinator._kindergarten_found_on = date(2026, 3, 1)  # school year 2025/26

    await coordinator._async_update_data()

    assert not coordinator.is_kindergarten
    client.async_get_timetable.assert_called()


async def test_saved_kindergarten_lid_without_a_date_counts_from_today(hass, hass_storage) -> None:
    entry = make_config_entry()
    _store(
        hass_storage,
        state_store_key(entry.entry_id),
        {"kindergarten": {"lid": "LID-AUTH-USER-1-CHILD", "group_id": None, "source": "Me"}},
    )
    coordinator = _coordinator(hass, build_mock_client(), entry)

    await coordinator.async_restore_state()

    assert coordinator.is_kindergarten
    assert coordinator._kindergarten_found_on == dt_util.now().date()
    saved = coordinator._tracked_state()["kindergarten"]
    assert saved["found_on"] == dt_util.now().date().isoformat()


# ----------------------------------------------------------------------
# Homework to-do ticks (finding 7)
# ----------------------------------------------------------------------


async def test_todo_drops_ticks_of_homework_librus_no_longer_lists_on_start(
    hass, hass_storage
) -> None:
    """After a new school year cleared the homework badge's count, every
    restart put last year's ticks back into it."""
    client = build_mock_client(
        async_get_homework_assignments={
            "HomeWorkAssignments": [
                {"Id": 1, "Topic": "Ćwiczenia", "Text": "s. 12", "Date": "2026-09-01", "DueDate": "2026-09-08"}
            ]
        }
    )
    entry = make_config_entry()
    entry.add_to_hass(hass)
    key = f"{DOMAIN}.{entry.entry_id}.homework_done"
    _store(hass_storage, key, {"done": ["1", "99"]}, version=1)
    with patch("custom_components.librus_synergia.LibrusApiClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass_storage[key]["data"]["done"] == ["1"]
    assert entry.runtime_data.homework_done_ever == {"1"}


async def test_todo_keeps_ticks_while_the_homework_list_is_a_saved_copy(hass, hass_storage) -> None:
    client = build_mock_client()
    client.async_get_homework_assignments.side_effect = LibrusConnectionError("timeout")
    entry = make_config_entry()
    entry.add_to_hass(hass)
    key = f"{DOMAIN}.{entry.entry_id}.homework_done"
    _store(hass_storage, key, {"done": ["1", "99"]}, version=1)
    with patch("custom_components.librus_synergia.LibrusApiClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass_storage[key]["data"]["done"] == ["1", "99"]
    assert entry.runtime_data.homework_done_ever == {"1", "99"}


# ----------------------------------------------------------------------
# A refused Wiadomości bootstrap mid-run (finding 8)
# ----------------------------------------------------------------------


async def test_refused_messages_bootstrap_keeps_the_last_messages_for_a_few_cycles(hass) -> None:
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 1}}
    client.async_get_messages.return_value = {
        "data": [{"messageId": "1", "senderName": "Anna Nowak", "topic": "Zebranie"}]
    }
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    # The Wiadomości session is set up again (after a relogin) and refused.
    coordinator._messages_bootstrapped = False
    client.async_bootstrap_messages.return_value = False
    second = await coordinator._async_update_data()
    assert second.messages_available is True
    assert [m.id for m in second.messages] == ["1"]
    assert "Messages" in coordinator.fallback_sections

    third = await coordinator._async_update_data()
    assert third.messages_available is True

    # Refused on every quick recheck: no Wiadomości after all.
    fourth = await coordinator._async_update_data()
    assert fourth.messages_available is False
    assert fourth.messages == []


# ----------------------------------------------------------------------
# Fewer recalculations (findings 9-11)
# ----------------------------------------------------------------------


async def test_unchanged_poll_skips_parsing_forecasts_and_badges(hass) -> None:
    client = build_mock_client(
        async_get_grades={
            "Grades": [{"Id": 1, "Grade": "5", "Subject": {"Id": 100}, "AddDate": "2026-09-02"}]
        }
    )
    entry = await setup_integration(hass, client)
    coordinator = entry.runtime_data
    data = coordinator.data
    with (
        patch.object(coordinator, "_build_data", wraps=coordinator._build_data) as build,
        patch.object(
            coordinator_module, "subject_forecasts", wraps=coordinator_module.subject_forecasts
        ) as forecasts,
        patch.object(
            coordinator_module, "compute_badges", wraps=coordinator_module.compute_badges
        ) as badges,
    ):
        coordinator._force_next_fetch = True
        await coordinator.async_refresh()
        assert client.async_get_grades.call_count == 2  # a real fetch
        assert coordinator.data is data
        assert (build.call_count, forecasts.call_count, badges.call_count) == (0, 0, 0)

        client.async_get_grades.return_value = {
            "Grades": [
                {"Id": 1, "Grade": "5", "Subject": {"Id": 100}, "AddDate": "2026-09-02"},
                {"Id": 2, "Grade": "4", "Subject": {"Id": 100}, "AddDate": "2026-09-03"},
            ]
        }
        coordinator._force_next_fetch = True
        await coordinator.async_refresh()

    assert build.call_count == 1
    assert forecasts.call_count == 1
    assert badges.call_count == 1
    assert len(coordinator.data.grades) == 2


async def test_agenda_and_free_days_events_come_sorted(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    today = dt_util.now().date()
    client = build_mock_client(
        async_get_homeworks={
            "HomeWorks": [
                {"Id": 3, "Content": "Trzecie", "Date": (today + timedelta(days=3)).isoformat()},
                {"Id": 1, "Content": "Pierwsze", "Date": (today + timedelta(days=1)).isoformat()},
                {"Id": 2, "Content": "Drugie", "Date": (today + timedelta(days=2)).isoformat()},
            ]
        }
    )
    entry = await setup_integration(hass, client)
    entity_id = _entity_id(hass, entry, "calendar", "agenda")

    assert hass.states.get(entity_id).attributes["message"] == "Pierwsze"
    start = dt_util.start_of_local_day()
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": entity_id, "start_date_time": start, "end_date_time": start + timedelta(days=7)},
        blocking=True,
        return_response=True,
    )
    assert [e["summary"] for e in response[entity_id]["events"]] == ["Pierwsze", "Drugie", "Trzecie"]


async def test_average_history_skips_the_same_data_on_the_same_day(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    client = build_mock_client(
        async_get_grades={
            "Grades": [{"Id": 1, "Grade": "5", "Subject": {"Id": 100}, "AddDate": "2026-09-02"}]
        }
    )
    entry = await setup_integration(hass, client)
    history = LibrusAverageHistory(hass, entry.runtime_data)
    with patch.object(history, "_async_write") as write:
        history._async_update()
        history._async_update()
        assert write.call_count == 1

        freezer.tick(timedelta(days=1))
        history._async_update()
        assert write.call_count == 2
