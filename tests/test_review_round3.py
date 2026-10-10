"""Review round 3: calendars, the on-demand week cache, saved state, clocks
and fewer writes/requests."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import date, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
)

from custom_components.librus_synergia import async_remove_entry
from custom_components.librus_synergia.average_history import LibrusAverageHistory
from custom_components.librus_synergia.calendar import _lesson_topic
from custom_components.librus_synergia.const import (
    DOMAIN,
    EVENT_NEW_GRADE,
    STATE_SAVE_DELAY,
    STATE_STORE_VERSION,
)
from custom_components.librus_synergia.coordinator import (
    LibrusDataUpdateCoordinator,
    good_grade_streak,
    payload_store_key,
    state_store_key,
)
from custom_components.librus_synergia.diagnostics import async_get_config_entry_diagnostics
from custom_components.librus_synergia.forecast import DataMemo
from librus_synergia import LibrusConnectionError, LibrusSessionExpiredError
from librus_synergia.client import LibrusSessionData
from librus_synergia.models import (
    AttachmentFileData,
    GradingSystemData,
    LessonData,
    LibrusData,
    MeData,
    OriginalLessonData,
)
from librus_synergia.parsers import merge_timetables, parse_grades, parse_realizations

from .conftest import build_mock_client, make_config_entry, messages_by_mailbox, setup_integration, saved_state

# ~05:00 in the test time zone (US/Pacific): clear of both midnights.
_FROZEN = "2026-09-09T12:00:00+00:00"


def _coordinator(hass, client, entry=None, *, options=None) -> LibrusDataUpdateCoordinator:
    entry = entry or make_config_entry(options=options)
    if entry.state is not ConfigEntryState.SETUP_IN_PROGRESS:
        entry.add_to_hass(hass)
        entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


def _store(hass_storage, key: str, data: dict) -> None:
    hass_storage[key] = {
        "version": STATE_STORE_VERSION,
        "minor_version": 1,
        "key": key,
        "data": data,
    }


def _entity_id(hass, entry, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")


def _hhmm(offset_minutes: int) -> str:
    return (dt_util.now() + timedelta(minutes=offset_minutes)).strftime("%H:%M")


def _lesson(no: int, start: str, end: str, subject: int, **extra) -> dict:
    return {
        "LessonNo": str(no),
        "HourFrom": start,
        "HourTo": end,
        "Subject": {"Id": str(subject)},
        "Teacher": {"Id": "500"},
        "Classroom": {"Id": "12"},
        "IsCanceled": False,
        "IsSubstitutionClass": False,
        **extra,
    }


def _plain_data(**fields) -> LibrusData:
    values = {
        "me": MeData(account_id=1, first_name="Ola", last_name="Kowalska"),
        "grades": [],
        "grade_categories": {},
        "notes": [],
        "attendances": [],
        "attendance_types": {},
        "timetable": {},
        "homeworks": [],
        "school_notices": [],
        "lucky_number": None,
        "subjects": {},
        "teachers": {},
        "classrooms": {},
    }
    values.update(fields)
    return LibrusData(**values)


# ----------------------------------------------------------------------
# Calendars
# ----------------------------------------------------------------------


async def test_agenda_calendar_skips_yesterdays_all_day_entry(hass, freezer) -> None:
    """An all-day event's `end` is exclusive: yesterday's entry ends today
    at 00:00 and must not be the current event all day long."""
    freezer.move_to(_FROZEN)
    today = dt_util.now().date()
    client = build_mock_client(
        async_get_homeworks={
            "HomeWorks": [
                {"Id": 1, "Content": "Wczoraj", "Date": (today - timedelta(days=1)).isoformat()},
                {"Id": 2, "Content": "Pojutrze", "Date": (today + timedelta(days=2)).isoformat()},
            ]
        }
    )
    entry = await setup_integration(hass, client)

    state = hass.states.get(_entity_id(hass, entry, "calendar", "agenda"))
    assert state.attributes["message"] == "Pojutrze"


def test_lesson_topics_are_kept_per_entry() -> None:
    """The topic index used to be keyed by a bare `id()` of the topics list
    and shared by every student: a recycled id gave one student another's
    topics, and two students evicted each other's index on every call."""
    day = date(2026, 10, 1)
    lessons = merge_timetables({"Timetable": {day.isoformat(): [[_lesson(2, "08:00", "08:45", 100)]]}})
    lesson = lessons[day][0]

    def data(topic: str) -> LibrusData:
        return _plain_data(
            lesson_topics=parse_realizations(
                {"Realizations": [{"Id": 1, "LessonNo": 2, "Date": day.isoformat(), "Topic": topic}]},
                {},
            )
        )

    first, second = data("Ułamki"), data("Procenty")
    assert _lesson_topic(day, lesson, first, "entry-a") == "Ułamki"
    assert _lesson_topic(day, lesson, second, "entry-b") == "Procenty"
    assert _lesson_topic(day, lesson, first, "entry-a") == "Ułamki"


async def test_on_demand_week_leaves_the_timetable_health_alone(hass) -> None:
    """A far week the school hasn't published (403) isn't a problem with the
    polled timetable: no degraded endpoint, no kindergarten discovery."""
    client = build_mock_client()
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()
    client.async_get_timetable.side_effect = LibrusSessionExpiredError(
        "Session rejected (HTTP 403).", status_code=403
    )

    today = dt_util.now().date()
    later = today - timedelta(days=today.weekday()) + timedelta(weeks=4)
    week = await coordinator.async_get_timetable_week(later)

    assert week == {}
    client.async_get_timetable.assert_called_with(later)
    assert coordinator._timetable_forbidden is False
    assert "Timetable" not in coordinator.degraded_endpoints


async def test_on_demand_weeks_are_fetched_once_and_shared(hass, freezer) -> None:
    """Two requests for the same week share one fetch; a near week is kept
    for two hours, then asked again."""
    freezer.move_to(_FROZEN)
    client = build_mock_client()
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()
    client.async_get_timetable.reset_mock()
    calls = 0

    async def timetable(_week):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"Timetable": {}}

    client.async_get_timetable.side_effect = timetable
    near = date(2026, 9, 21)  # two weeks after this one

    await asyncio.gather(
        coordinator.async_get_timetable_week(near), coordinator.async_get_timetable_week(near)
    )
    assert calls == 1
    await coordinator.async_get_timetable_week(near)
    assert calls == 1

    freezer.tick(timedelta(hours=2, minutes=1))
    await coordinator.async_get_timetable_week(near)
    assert calls == 2


async def test_assist_timetable_reports_a_room_change_not_a_substitution(hass, freezer) -> None:
    """Librus flags a room change as a substitution too."""
    from custom_components.librus_synergia.llm_api import TimetableTool

    freezer.move_to(_FROZEN)
    today = dt_util.now().date()
    lesson = LessonData(
        lesson_no=1,
        hour_from="08:00",
        hour_to="08:45",
        subject_id=1,
        teacher_id=10,
        classroom_id=21,
        is_canceled=False,
        is_substitution=True,
        original=OriginalLessonData(
            date=today.isoformat(),
            lesson_no=1,
            hour_from="08:00",
            hour_to="08:45",
            subject_id=1,
            teacher_id=10,
            classroom_id=12,
        ),
    )
    entry = await setup_integration(hass, build_mock_client())
    coordinator = entry.runtime_data
    coordinator.data = dataclasses.replace(
        coordinator.data,
        timetable={today: [lesson]},
        subjects={1: "Matematyka"},
        classrooms={12: "12", 21: "21"},
    )

    body = await TimetableTool()._async_for_student(coordinator, coordinator.data, today, {})

    (row,) = body["days"][0]["lessons"]
    assert row["change"] == "room change"


# ----------------------------------------------------------------------
# Attachment view, diagnostics, services
# ----------------------------------------------------------------------


async def test_attachment_view_rejects_odd_ids_and_hides_librus_errors(hass, hass_client) -> None:
    client = build_mock_client()
    client.async_download_message_attachment.side_effect = LibrusConnectionError(
        "secret upstream detail"
    )
    client.async_download_homework_attachment.return_value = AttachmentFileData(
        filename="plan\r\nX-Evil: 1.pdf", content_type="text/html", content=b"<p>"
    )
    entry = await setup_integration(hass, client)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    http = await hass_client()

    odd = await http.get(f"/api/{DOMAIN}/attachment/{device.id}/99/a.b")
    assert odd.status == 404
    client.async_download_message_attachment.assert_not_called()

    failed = await http.get(f"/api/{DOMAIN}/attachment/{device.id}/99/55")
    assert failed.status == 502
    assert "secret upstream detail" not in await failed.text()

    ok = await http.get(f"/api/{DOMAIN}/homework_attachment/{device.id}/31")
    assert ok.status == 200
    assert ok.headers["X-Content-Type-Options"] == "nosniff"
    assert "\r" not in ok.headers["Content-Disposition"]
    assert "X-Evil" not in ok.headers


async def test_diagnostics_redact_notes_number_and_lids(hass) -> None:
    lid = "LID-AUTH-USER-777"
    client = build_mock_client(
        async_get_teachers={
            "Users": [{"Id": 5, "AccountId": lid, "FirstName": "Anna", "LastName": "Nowak"}]
        }
    )
    entry = make_config_entry(options={"ai_extra_context": "egzamin w maju", "student_number": 12})
    entry.add_to_hass(hass)
    with patch("custom_components.librus_synergia.LibrusApiClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(diagnostics, default=str)

    assert "egzamin w maju" not in dumped
    assert diagnostics["options"]["student_number"] != 12
    assert lid not in dumped


async def test_unload_drops_services_when_no_other_entry_is_loaded(hass) -> None:
    """A second entry that isn't loaded (disabled, failed) can't serve the
    services - unloading the last loaded one removes them."""
    entry = await setup_integration(hass, build_mock_client())
    make_config_entry().add_to_hass(hass)  # never set up

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert not hass.services.has_service(DOMAIN, "get_message")


async def test_remove_entry_clears_the_summary_store_and_statistics(hass, hass_storage) -> None:
    entry = make_config_entry()
    entry.add_to_hass(hass)
    _store(hass_storage, f"{DOMAIN}.weekly_summary.{entry.entry_id}", {"result": {"headline": "x"}})
    _store(hass_storage, payload_store_key(entry.entry_id), {"payloads": {}})
    hass.config.components.add("recorder")
    prefix = f"{DOMAIN}:{entry.entry_id.lower()}_average"
    recorder = Mock()
    with (
        patch(
            "homeassistant.components.recorder.statistics.async_list_statistic_ids",
            AsyncMock(
                return_value=[
                    {"statistic_id": prefix},
                    {"statistic_id": f"{prefix}_100"},
                    {"statistic_id": "sensor.other"},
                ]
            ),
        ),
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
    ):
        await async_remove_entry(hass, entry)
        await hass.async_block_till_done()

    assert f"{DOMAIN}.weekly_summary.{entry.entry_id}" not in hass_storage
    assert payload_store_key(entry.entry_id) not in hass_storage
    recorder.async_clear_statistics.assert_called_once_with([prefix, f"{prefix}_100"])


# ----------------------------------------------------------------------
# Messages, receipts, saved state
# ----------------------------------------------------------------------


async def test_failed_messages_fetch_keeps_the_last_counts(hass) -> None:
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 2}}
    client.async_get_messages.side_effect = messages_by_mailbox(
        {"inbox": {"data": [{"messageId": "1", "senderName": "Anna Nowak", "topic": "Zebranie"}]}}
    )
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    client.async_get_unread_messages_count.side_effect = LibrusConnectionError("timeout")
    data = await coordinator._async_update_data()

    assert data.unread_message_count == 2
    assert [m.id for m in data.messages] == ["1"]
    assert "Messages" in coordinator.fallback_sections


async def test_read_receipts_without_recipients_are_not_asked_again(hass) -> None:
    today = dt_util.now().date().isoformat()
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 0}}
    client.async_get_messages.side_effect = messages_by_mailbox(
        {
            "outbox": {
                "data": [
                    {"messageId": "9", "receiverName": "Klasa 7d", "topic": "Wycieczka",
                     "content": "", "sendDate": f"{today} 08:00:00"}
                ]
            }
        }
    )
    client.async_get_message.return_value = {"data": {"messageId": "9", "topic": "Wycieczka", "Message": ""}}
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    assert coordinator.read_receipts["9"]["total"] == 0
    coordinator._receipts_fetched_at.clear()
    await coordinator._async_update_data()

    assert client.async_get_message.await_count == 1


async def test_session_cookies_go_to_the_state_not_the_config_entry(hass) -> None:
    """A token refresh (same cookies, new values) doesn't rewrite the config
    entry; a new cookie (or a new DeviceCookie) does."""
    cookies = [{"name": "oauth_token", "value": "a", "domain": "synergia.librus.pl"}]
    entry = make_config_entry(cookies=cookies)
    coordinator = _coordinator(hass, build_mock_client(), entry)

    with patch.object(hass.config_entries, "async_update_entry") as update:
        coordinator.remember_session(
            LibrusSessionData(
                cookies=[{**cookies[0], "value": "b"}], logged_in_at=2000.0
            )
        )
        update.assert_not_called()
        coordinator.remember_session(
            LibrusSessionData(
                cookies=[
                    {**cookies[0], "value": "c"},
                    {"name": "DeviceCookie", "value": "d", "domain": "api.librus.pl", "path": "/OAuth"},
                ],
                logged_in_at=3000.0,
            )
        )
        update.assert_called_once()
    assert coordinator._tracked_state()["session"]["logged_in_at"] == 3000.0


async def test_newer_saved_session_is_imported_on_restore(hass, hass_storage) -> None:
    entry = make_config_entry(cookies=[], session_logged_in_at=1000.0)
    newer = [{"name": "oauth_token", "value": "new", "domain": "synergia.librus.pl"}]
    _store(
        hass_storage,
        state_store_key(entry.entry_id),
        {"session": {"cookies": newer, "logged_in_at": 5000.0}},
    )
    client = build_mock_client()
    coordinator = _coordinator(hass, client, entry)

    await coordinator.async_restore_state()

    client.import_session.assert_called_once_with(
        LibrusSessionData(cookies=newer, logged_in_at=5000.0)
    )


async def test_saved_state_stays_small(hass, freezer) -> None:
    """Old timetable-change ids, past Agenda entries and justifications
    Librus no longer lists aren't kept."""
    freezer.move_to(_FROZEN)
    today = dt_util.now().date()
    client = build_mock_client(
        async_get_homeworks={
            "HomeWorks": [
                {"Id": 1, "Content": "Stare", "Date": (today - timedelta(days=3)).isoformat()},
                {"Id": 2, "Content": "Nowe", "Date": (today + timedelta(days=3)).isoformat()},
            ]
        },
        async_get_justifications={"data": [{"id": 7, "justificationStatus": "new"}]},
    )
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()
    coordinator._change_tracker.seen.timetable_changes.add(
        f"{(today - timedelta(days=1)).isoformat()}|3|canceled|100"
    )
    coordinator._known_justifications["99"] = "accept"

    await coordinator._async_update_data()

    assert coordinator._change_tracker.seen.timetable_changes == set()
    assert set(coordinator._known_agenda) == {"2"}
    assert set(coordinator._known_justifications) == {"7"}


async def test_state_and_payloads_are_saved_separately(hass, freezer) -> None:
    """A newly seen grade writes the small tracked state soon; the big
    payloads file is handed to its store once and written at most every few
    hours (or when Home Assistant stops - the store's final write)."""
    freezer.move_to(_FROZEN)
    grade = {"Id": 1, "Grade": "5", "Subject": {"Id": 100}, "AddDate": "2026-09-01"}
    client = build_mock_client(async_get_grades={"Grades": [grade]})
    coordinator = _coordinator(hass, client)
    with (
        patch.object(coordinator._state_store, "async_delay_save") as state_save,
        patch.object(coordinator._payload_store, "async_delay_save") as payload_save,
    ):
        await coordinator._async_update_data()
        assert (state_save.call_count, payload_save.call_count) == (1, 1)
        assert payload_save.call_args.args[1] == STATE_SAVE_DELAY  # never written yet

        client.async_get_grades.return_value = {"Grades": [grade, {**grade, "Id": 2}]}
        freezer.tick(timedelta(minutes=20))
        await coordinator._async_update_data()
        # Still pending: the change goes out with the write already scheduled.
        assert (state_save.call_count, payload_save.call_count) == (2, 1)

        # The store writes it (what the delayed write calls).
        written = payload_save.call_args.args[0]()
        assert written["payloads"]["Grades"]["Grades"][1]["Id"] == 2
        assert written["saved_at"] == dt_util.utcnow().isoformat()

        client.async_get_grades.return_value = {"Grades": [grade]}
        freezer.tick(timedelta(minutes=20))
        await coordinator._async_update_data()
        assert payload_save.call_count == 2
        # Due six hours after the last write, not sooner.
        assert payload_save.call_args.args[1] == pytest.approx(
            (timedelta(hours=6) - timedelta(minutes=20)).total_seconds()
        )


async def test_restart_reuses_the_daily_lookups_and_the_kindergarten(hass, hass_storage) -> None:
    """The reference data, the lucky number and the kindergarten child's LID
    come back from the saved state: a restart asks Librus only for what is
    due, like any other cycle."""
    first = _coordinator(hass, build_mock_client())
    await first._async_update_data()
    first._kindergarten_lid, first._kindergarten_source = "LID-AUTH-USER-1", "Me"
    saved = saved_state(first)

    entry = make_config_entry()
    _store(hass_storage, state_store_key(entry.entry_id), saved)
    client = build_mock_client()
    second = _coordinator(hass, client, entry)
    await second.async_restore_state()
    await second._async_update_data()

    client.async_get_subjects.assert_not_called()
    client.async_get_lucky_number.assert_not_called()
    client.async_get_token_info.assert_not_called()
    assert second.is_kindergarten


# ----------------------------------------------------------------------
# Fewer writes and requests
# ----------------------------------------------------------------------


async def test_identical_refresh_keeps_the_data_and_skips_writes(hass) -> None:
    client = build_mock_client()
    entry = await setup_integration(hass, client)
    coordinator = entry.runtime_data
    entity_id = _entity_id(hass, entry, "sensor", "attendance")
    before = hass.states.get(entity_id)
    data = coordinator.data

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data is data
    assert hass.states.get(entity_id).last_reported == before.last_reported


async def test_status_last_attempt_is_only_the_failed_one(hass) -> None:
    client = build_mock_client()
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()
    assert coordinator.last_attempt_at is None

    # Librus down: the saved responses stand in - and the failed attempt is
    # what `last_attempt` shows.
    client.async_get_me.side_effect = LibrusConnectionError("down")
    await coordinator._async_update_data()
    assert coordinator.last_attempt_at is not None
    assert coordinator.failures == 1


async def test_sparse_endpoints_are_asked_hourly(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    client = build_mock_client()
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()
    freezer.tick(timedelta(minutes=20))
    await coordinator._async_update_data()

    assert client.async_get_base_text_grades.call_count == 1
    assert client.async_get_behaviour_grade_points.call_count == 1
    assert client.async_get_descriptive_grades.call_count == 1

    freezer.tick(timedelta(hours=1))
    await coordinator._async_update_data()
    assert client.async_get_descriptive_grades.call_count == 2


async def test_event_entity_ignores_other_students_events(hass) -> None:
    entry = await setup_integration(hass, build_mock_client())
    entity_id = _entity_id(hass, entry, "event", "grade_event")

    hass.bus.async_fire(EVENT_NEW_GRADE, {"entry_id": "someone-else", "value": "6"})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unknown"

    hass.bus.async_fire(EVENT_NEW_GRADE, {"entry_id": entry.entry_id, "value": "6"})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state != "unknown"


async def test_assist_grades_default_to_the_last_month(hass, freezer) -> None:
    from custom_components.librus_synergia.llm_api import GradesTool

    freezer.move_to(_FROZEN)
    today = dt_util.now().date()
    client = build_mock_client(
        async_get_subjects={"Subjects": [{"Id": 100, "Name": "Matematyka"}]},
        async_get_grades={
            "Grades": [
                {"Id": 1, "Grade": "3", "Subject": {"Id": 100}, "AddDate": (today - timedelta(days=40)).isoformat()},
                {"Id": 2, "Grade": "5", "Subject": {"Id": 100}, "AddDate": (today - timedelta(days=5)).isoformat()},
            ]
        },
    )
    entry = await setup_integration(hass, client)
    coordinator = entry.runtime_data

    recent = await GradesTool()._async_for_student(coordinator, coordinator.data, today, {})
    by_subject = await GradesTool()._async_for_student(
        coordinator, coordinator.data, today, {"subject": "matem"}
    )

    assert [g["value"] for g in recent["grades"]] == ["5"]
    assert [g["value"] for g in by_subject["grades"]] == ["5", "3"]


async def test_restart_does_not_rewrite_unchanged_statistics(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    client = build_mock_client(
        async_get_grades={
            "Grades": [{"Id": 1, "Grade": "5", "Subject": {"Id": 100}, "AddDate": "2026-09-02 10:00:00"}]
        }
    )
    entry = await setup_integration(hass, client)
    coordinator = entry.runtime_data
    with patch(
        "homeassistant.components.recorder.statistics.async_add_external_statistics"
    ) as add:
        LibrusAverageHistory(hass, coordinator)._async_update()
        assert add.call_count == 2
        add.reset_mock()
        # A new instance - what a restart starts with - with the saved digests.
        LibrusAverageHistory(hass, coordinator)._async_update()
    assert add.call_count == 0


# ----------------------------------------------------------------------
# Clocks
# ----------------------------------------------------------------------


async def test_lesson_sensors_change_when_a_lesson_starts(hass, freezer) -> None:
    """Not at the next poll: at the lesson's start."""
    freezer.move_to(_FROZEN)
    today = dt_util.now().date().isoformat()
    client = build_mock_client(
        async_get_timetable={
            "Timetable": {
                today: [
                    [_lesson(1, _hhmm(10), _hhmm(55), 100)],
                    [_lesson(2, _hhmm(60), _hhmm(105), 200)],
                ]
            }
        },
        async_get_subjects={"Subjects": [{"Id": 100, "Name": "Matematyka"}, {"Id": 200, "Name": "Polski"}]},
    )
    entry = await setup_integration(hass, client)
    next_lesson = _entity_id(hass, entry, "sensor", "next_lesson")
    current = _entity_id(hass, entry, "sensor", "current_lesson")
    assert hass.states.get(next_lesson).state == "Matematyka"
    assert hass.states.get(current).state == "unknown"

    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(next_lesson).state == "Polski"
    assert hass.states.get(current).state == "Matematyka"
    assert client.async_get_grades.call_count == 1  # no poll in between


async def test_midnight_tick_updates_the_entities(hass, freezer) -> None:
    freezer.move_to("2026-10-07T06:59:00+00:00")  # 23:59 local
    entry = await setup_integration(hass, build_mock_client())
    coordinator = entry.runtime_data

    with patch.object(coordinator, "async_update_listeners") as update:
        freezer.move_to("2026-10-07T07:00:06+00:00")
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    update.assert_called()


# ----------------------------------------------------------------------
# Grades
# ----------------------------------------------------------------------


def test_good_grade_streak_uses_the_schools_grade_scale() -> None:
    grades = parse_grades(
        {"Grades": [{"Id": 1, "Grade": "3+", "Subject": {"Id": 100}, "AddDate": "2026-09-01"}]}
    )
    assert good_grade_streak(grades) == 0
    assert good_grade_streak(grades, GradingSystemData(plus_value=1.0)) == 1


def test_data_memo_drops_an_unloaded_entrys_slot() -> None:
    memo = DataMemo()
    data = object()
    memo.get(data, "k", lambda: 1, owner="entry")
    DataMemo.discard_all("entry")
    assert memo.get(data, "k", lambda: 2, owner="entry") == 2


async def test_subject_sensor_of_a_gone_subject_is_retired(hass) -> None:
    client = build_mock_client(
        async_get_subjects={"Subjects": [{"Id": 100, "Name": "Matematyka"}, {"Id": 200, "Name": "Plastyka"}]}
    )
    entry = await setup_integration(hass, client)
    assert _entity_id(hass, entry, "sensor", "subject_200_average") is not None

    client.async_get_subjects.return_value = {"Subjects": [{"Id": 100, "Name": "Matematyka"}]}
    entry.runtime_data._reference_data_fetched_at = None
    await entry.runtime_data.async_refresh()
    with patch("custom_components.librus_synergia.LibrusApiClient", return_value=client):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert _entity_id(hass, entry, "sensor", "subject_200_average") is None
    assert _entity_id(hass, entry, "sensor", "subject_100_average") is not None
