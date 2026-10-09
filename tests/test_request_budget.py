"""How many requests a normal cycle makes: lookups and lists that rarely
change are reused instead of being asked for again every cycle, and the
saved state is written only when something in it changed."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.util import dt as dt_util

from custom_components.librus_synergia.const import STATE_STORE_VERSION
from custom_components.librus_synergia.coordinator import (
    LibrusDataUpdateCoordinator,
    state_store_key,
)
from librus_synergia import LibrusUnexpectedResponseError

from .conftest import build_mock_client, make_config_entry, messages_by_mailbox

# 19:00 UTC is 12:00 in the test time zone (US/Pacific): ticks of a few
# hours stay on the same local day.
NOON = "2026-10-07T19:00:00+00:00"
CYCLE = timedelta(minutes=20)


def _coordinator(hass, client, entry=None) -> LibrusDataUpdateCoordinator:
    entry = entry or make_config_entry()
    if entry.state is not ConfigEntryState.SETUP_IN_PROGRESS:
        entry.add_to_hass(hass)
        entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


def _store(hass_storage, entry_id: str, data: dict) -> None:
    hass_storage[state_store_key(entry_id)] = {
        "version": STATE_STORE_VERSION,
        "minor_version": 1,
        "key": state_store_key(entry_id),
        "data": data,
    }


def _grade(grade_id: int, category_id: int = 10, comment_ids: tuple[int, ...] = ()) -> dict:
    return {
        "Id": grade_id,
        "Grade": "5",
        "Category": {"Id": category_id},
        "Subject": {"Id": 100},
        "Semester": 1,
        "AddDate": "2026-10-01",
        "Comments": [{"Id": comment_id} for comment_id in comment_ids],
    }


def _mailbox_calls(client, mailbox: str) -> int:
    """Message-list requests for one mailbox (the inbox one has no
    `mailbox` keyword)."""
    return sum(
        1
        for call in client.async_get_messages.call_args_list
        if call.kwargs.get("mailbox", "inbox") == mailbox
    )


def _message(message_id: str, **extra) -> dict:
    return {
        "messageId": message_id,
        "senderName": "Anna Nowak",
        "topic": f"Wiadomość {message_id}",
        "content": "",
        "sendDate": "2026-10-01T10:00:00",
        "readDate": None,
        "isAnyFileAttached": False,
        **extra,
    }


# ----------------------------------------------------------------------
# Mailboxes the account doesn't have (L1)
# ----------------------------------------------------------------------


async def test_missing_mailbox_skipped_for_a_day_then_probed_again(hass, freezer) -> None:
    freezer.move_to(NOON)
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 0}}
    client.async_get_messages.side_effect = messages_by_mailbox(
        {"alerts": LibrusUnexpectedResponseError("HTTP 404", status_code=404)}
    )
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    assert _mailbox_calls(client, "alerts") == 1
    assert coordinator.missing_mailboxes == {"alerts"}

    freezer.tick(CYCLE)
    data = await coordinator._async_update_data()
    # Not asked again, still reported missing (the Messages card hides it).
    assert _mailbox_calls(client, "alerts") == 1
    assert coordinator.missing_mailboxes == {"alerts"}
    assert data.alert_messages == []
    assert "Messages/Secondary" not in coordinator.degraded_endpoints

    freezer.tick(timedelta(hours=25))
    await coordinator._async_update_data()
    assert _mailbox_calls(client, "alerts") == 2


async def test_mailbox_that_appears_later_is_found_after_a_day(hass, freezer) -> None:
    freezer.move_to(NOON)
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 0}}
    client.async_get_messages.side_effect = messages_by_mailbox(
        {"alerts": LibrusUnexpectedResponseError("HTTP 404", status_code=404)}
    )
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    client.async_get_messages.side_effect = messages_by_mailbox(
        {"alerts": {"data": [_message("5")]}}
    )
    freezer.tick(timedelta(hours=25))
    data = await coordinator._async_update_data()

    assert coordinator.missing_mailboxes == set()
    assert [m.id for m in data.alert_messages] == ["5"]


# ----------------------------------------------------------------------
# Grade categories and attendance types (L2)
# ----------------------------------------------------------------------


def _lookup_client(**overrides):
    defaults = {
        "async_get_grades": {"Grades": [_grade(1, 10)]},
        "async_get_grade_categories": {
            "Categories": [{"Id": 10, "Name": "Sprawdzian", "Weight": 3}]
        },
        "async_get_attendances": {
            "Attendances": [{"Id": 1, "Date": "2026-10-01", "Type": {"Id": 1}}]
        },
        "async_get_attendance_types": {
            "Types": [{"Id": 1, "Name": "Nieobecność", "IsPresenceKind": False}]
        },
    }
    return build_mock_client(
        **{**defaults, **overrides},
    )


async def test_categories_and_types_not_fetched_every_cycle(hass, freezer) -> None:
    freezer.move_to(NOON)
    client = _lookup_client()
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    freezer.tick(CYCLE)
    data = await coordinator._async_update_data()

    assert client.async_get_grade_categories.call_count == 1
    assert client.async_get_attendance_types.call_count == 1
    # Same shapes for the sensors as before.
    assert data.grade_categories[10].name == "Sprawdzian"
    assert data.grade_categories[10].weight == 3
    assert data.attendance_types[1].name == "Nieobecność"


async def test_unknown_category_or_type_is_fetched_the_same_cycle(hass, freezer) -> None:
    freezer.move_to(NOON)
    client = _lookup_client()
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    # A teacher created a new category and graded with it.
    client.async_get_grades.return_value = {"Grades": [_grade(1, 10), _grade(2, 11)]}
    client.async_get_grade_categories.return_value = {
        "Categories": [
            {"Id": 10, "Name": "Sprawdzian", "Weight": 3},
            {"Id": 11, "Name": "Kartkówka", "Weight": 2},
        ]
    }
    freezer.tick(CYCLE)
    data = await coordinator._async_update_data()

    assert client.async_get_grade_categories.call_count == 2
    assert client.async_get_attendance_types.call_count == 1
    assert data.grade_categories[11].name == "Kartkówka"

    # Same for an attendance type the cache doesn't have.
    client.async_get_attendances.return_value = {
        "Attendances": [
            {"Id": 1, "Date": "2026-10-01", "Type": {"Id": 1}},
            {"Id": 2, "Date": "2026-10-02", "Type": {"Id": 1685}},
        ]
    }
    client.async_get_attendance_types.return_value = {
        "Types": [
            {"Id": 1, "Name": "Nieobecność", "IsPresenceKind": False},
            {"Id": 1685, "Name": "Pobyt w sanatorium", "IsPresenceKind": True},
        ]
    }
    freezer.tick(CYCLE)
    data = await coordinator._async_update_data()

    assert client.async_get_attendance_types.call_count == 2
    assert client.async_get_grade_categories.call_count == 2
    assert data.attendance_types[1685].name == "Pobyt w sanatorium"


async def test_id_librus_itself_does_not_know_waits_for_the_daily_refresh(hass, freezer) -> None:
    """A category id the categories endpoint doesn't list either must not
    be asked for on every cycle, nor every hour - once is enough."""
    freezer.move_to(NOON)
    client = _lookup_client(async_get_grades={"Grades": [_grade(1, 99)]})
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()  # the daily batch already asked
    freezer.tick(CYCLE)
    await coordinator._async_update_data()
    assert client.async_get_grade_categories.call_count == 1

    freezer.tick(timedelta(hours=2))
    await coordinator._async_update_data()
    assert client.async_get_grade_categories.call_count == 1

    # A different unknown id is asked for at once.
    client.async_get_grades.return_value = {"Grades": [_grade(1, 99), _grade(2, 98)]}
    freezer.tick(CYCLE)
    await coordinator._async_update_data()
    assert client.async_get_grade_categories.call_count == 2

    # Then nothing until the daily refresh.
    freezer.tick(timedelta(hours=3))
    await coordinator._async_update_data()
    assert client.async_get_grade_categories.call_count == 2
    freezer.tick(timedelta(hours=22))
    await coordinator._async_update_data()
    assert client.async_get_grade_categories.call_count == 3


async def test_failed_type_refetch_is_retried_and_holds_back_absences(hass, freezer) -> None:
    freezer.move_to(NOON)
    client = _lookup_client()
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    client.async_get_attendances.return_value = {
        "Attendances": [{"Id": 2, "Date": "2026-10-02", "Type": {"Id": 3}}]
    }
    client.async_get_attendance_types.side_effect = LibrusUnexpectedResponseError("HTTP 500")
    freezer.tick(CYCLE)
    await coordinator._async_update_data()
    assert client.async_get_attendance_types.call_count == 2
    # The absences tracker waits for the type (not recorded as seen yet).
    assert "Attendances/Types" in coordinator._failed_this_cycle

    freezer.tick(CYCLE)
    await coordinator._async_update_data()
    assert client.async_get_attendance_types.call_count == 3


# ----------------------------------------------------------------------
# Comment lookups (L3)
# ----------------------------------------------------------------------


async def test_comments_fetched_only_for_new_comment_ids_or_daily(hass, freezer) -> None:
    freezer.move_to(NOON)
    client = build_mock_client(
        async_get_grades={"Grades": [_grade(1, comment_ids=(501,))]},
        async_get_grade_comments={"Comments": [{"Id": 501, "Text": "Brawo"}]},
    )
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    freezer.tick(CYCLE)
    data = await coordinator._async_update_data()
    assert client.async_get_grade_comments.call_count == 1
    assert client.async_get_behaviour_grade_point_comments.call_count == 1
    assert data.grades[0].comments == ["Brawo"]

    # A new grade with a comment the saved lookup doesn't have.
    client.async_get_grades.return_value = {
        "Grades": [_grade(1, comment_ids=(501,)), _grade(2, comment_ids=(502,))]
    }
    client.async_get_grade_comments.return_value = {
        "Comments": [{"Id": 501, "Text": "Brawo"}, {"Id": 502, "Text": "Popraw"}]
    }
    freezer.tick(CYCLE)
    data = await coordinator._async_update_data()
    assert client.async_get_grade_comments.call_count == 2
    assert {g.id: g.comments for g in data.grades} == {1: ["Brawo"], 2: ["Popraw"]}

    # Once a day anyway (a comment's text can be edited).
    freezer.tick(timedelta(hours=25))
    await coordinator._async_update_data()
    assert client.async_get_grade_comments.call_count == 3
    assert client.async_get_behaviour_grade_point_comments.call_count == 2


# ----------------------------------------------------------------------
# Lucky number (L5)
# ----------------------------------------------------------------------


async def test_lucky_number_not_asked_while_cached_day_is_later(hass, freezer) -> None:
    freezer.move_to(NOON)
    tomorrow = (dt_util.now().date() + timedelta(days=1)).isoformat()
    client = build_mock_client(
        async_get_lucky_number={"LuckyNumber": {"LuckyNumber": 7, "LuckyNumberDay": tomorrow}}
    )
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    freezer.tick(timedelta(hours=2))
    data = await coordinator._async_update_data()

    assert client.async_get_lucky_number.call_count == 1
    assert data.lucky_number.number == 7


async def test_no_lucky_number_is_asked_again_hourly(hass, freezer) -> None:
    freezer.move_to(NOON)
    client = build_mock_client(async_get_lucky_number={})
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    freezer.tick(CYCLE)
    await coordinator._async_update_data()
    assert client.async_get_lucky_number.call_count == 1

    freezer.tick(timedelta(minutes=41))
    await coordinator._async_update_data()
    assert client.async_get_lucky_number.call_count == 2


# ----------------------------------------------------------------------
# Message lists (L6)
# ----------------------------------------------------------------------


async def test_inbox_list_reused_while_unread_count_unchanged(hass, freezer) -> None:
    freezer.move_to(NOON)
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 1}}
    client.async_get_messages.side_effect = messages_by_mailbox(
        {"inbox": {"data": [_message("1")]}}
    )
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    freezer.tick(CYCLE)
    data = await coordinator._async_update_data()

    assert _mailbox_calls(client, "inbox") == 1
    assert client.async_get_unread_messages_count.call_count == 2
    assert [m.id for m in data.messages] == ["1"]

    # A new message: the count changes, the list is fetched.
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 2}}
    client.async_get_messages.side_effect = messages_by_mailbox(
        {"inbox": {"data": [_message("2"), _message("1")]}}
    )
    freezer.tick(CYCLE)
    data = await coordinator._async_update_data()
    assert _mailbox_calls(client, "inbox") == 2
    assert [m.id for m in data.messages] == ["2", "1"]

    # Same count, but the copy is over an hour old.
    freezer.tick(timedelta(minutes=61))
    await coordinator._async_update_data()
    assert _mailbox_calls(client, "inbox") == 3


async def test_reused_outbox_list_still_feeds_read_receipts(hass, freezer) -> None:
    freezer.move_to(NOON)
    today = dt_util.now().date().isoformat()
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 0}}
    client.async_get_messages.side_effect = messages_by_mailbox(
        {
            "outbox": {
                "data": [
                    {
                        "messageId": "9",
                        "receiverName": "Anna Nowak",
                        "topic": "Pytanie",
                        "content": "",
                        "sendDate": f"{today} 08:00:00",
                    }
                ]
            }
        }
    )
    client.async_get_message.return_value = {
        "data": {
            "messageId": "9",
            "topic": "Pytanie",
            "Message": "",
            "receivers": [
                {"firstName": "Anna", "lastName": "Nowak", "group": "nauczyciel", "readed": ""}
            ],
        }
    }
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    freezer.tick(CYCLE)
    data = await coordinator._async_update_data()

    assert _mailbox_calls(client, "outbox") == 1
    assert [m.id for m in data.sent_messages] == ["9"]
    assert "9" in coordinator.read_receipts


# ----------------------------------------------------------------------
# Saved state (H4)
# ----------------------------------------------------------------------


async def test_state_save_scheduled_only_when_something_changed(hass, freezer) -> None:
    freezer.move_to("2026-10-07T15:00:00+00:00")  # 08:00 local
    client = build_mock_client(async_get_grades={"Grades": [_grade(1)]})
    coordinator = _coordinator(hass, client)

    with patch.object(coordinator._state_store, "async_delay_save") as save:
        await coordinator._async_update_data()
        assert save.call_count == 1  # first cycle: nothing saved yet

        freezer.tick(CYCLE)
        await coordinator._async_update_data()
        assert save.call_count == 1  # same data: no write

        client.async_get_grades.return_value = {"Grades": [_grade(1), _grade(2)]}
        freezer.tick(CYCLE)
        await coordinator._async_update_data()
        assert save.call_count == 2  # a new response (and a newly seen grade)

        # Nothing changes for hours: written anyway, to keep
        # last_success_at fresh.
        freezer.tick(timedelta(hours=7))
        await coordinator._async_update_data()
        assert save.call_count == 3


async def test_timetable_weeks_are_saved_once(hass) -> None:
    client = build_mock_client()
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    saved = coordinator._state_to_save()

    assert "Timetable (this week)" not in saved["payloads"]
    assert "Timetable (next week)" not in saved["payloads"]
    assert len(saved["timetable"]) == 2


async def test_restart_with_unchanged_data_does_not_write(hass, hass_storage) -> None:
    first = _coordinator(hass, build_mock_client(async_get_grades={"Grades": [_grade(1)]}))
    await first._async_update_data()
    saved = json.loads(json.dumps(first._state_to_save()))

    entry = make_config_entry()
    _store(hass_storage, entry.entry_id, saved)
    second = _coordinator(hass, build_mock_client(async_get_grades={"Grades": [_grade(1)]}), entry)
    await second.async_restore_state()
    with patch.object(second._state_store, "async_delay_save") as save:
        await second._async_update_data()

    save.assert_not_called()


async def test_old_saved_timetable_copies_are_dropped(hass, hass_storage) -> None:
    """Saved by an older version: the weeks were kept twice."""
    entry = make_config_entry()
    _store(
        hass_storage,
        entry.entry_id,
        {
            "payloads": {
                "Grades": {"Grades": []},
                "Timetable (this week)": {"Timetable": {}},
                "Timetable (next week)": {"Timetable": {}},
            },
            "last_success_at": dt_util.utcnow().isoformat(),
        },
    )
    coordinator = _coordinator(hass, build_mock_client(), entry)

    await coordinator.async_restore_state()

    assert "Timetable (this week)" not in coordinator._last_good
    assert "Grades" in coordinator._last_good
    assert coordinator._state_dirty is True


# ----------------------------------------------------------------------
# Point grades
# ----------------------------------------------------------------------


def _point_grade(grade_id: int, category_id: int) -> dict:
    return {
        "Id": grade_id,
        "Grade": "5",
        "GradeValue": 5,
        "Category": {"Id": category_id},
        "Subject": {"Id": 100},
        "Semester": 1,
        "AddDate": "2026-10-01 10:00:00",
    }


async def test_point_grades_hourly_while_unknown_and_categories_on_demand(hass, freezer) -> None:
    """While Units hasn't said whether the school grades in points, point
    grades are asked for at most hourly; their categories once a day, or
    in the same cycle for a category the saved copy doesn't have."""
    freezer.move_to(NOON)
    client = build_mock_client(
        async_get_point_grades={"Grades": [_point_grade(1, 1)]},
        async_get_point_grade_categories={
            "Categories": [{"Id": 1, "Name": "Sprawdzian", "ValueTo": 10}]
        },
    )
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    freezer.tick(CYCLE)
    await coordinator._async_update_data()
    assert coordinator.point_grades_enabled is None
    assert client.async_get_point_grades.call_count == 1
    assert client.async_get_point_grade_categories.call_count == 1

    client.async_get_point_grades.return_value = {
        "Grades": [_point_grade(1, 1), _point_grade(2, 2)]
    }
    client.async_get_point_grade_categories.return_value = {
        "Categories": [
            {"Id": 1, "Name": "Sprawdzian", "ValueTo": 10},
            {"Id": 2, "Name": "Kartkówka", "ValueTo": 5},
        ]
    }
    freezer.tick(timedelta(minutes=45))
    data = await coordinator._async_update_data()

    assert client.async_get_point_grades.call_count == 2
    assert client.async_get_point_grade_categories.call_count == 2
    assert {g.id: g.max_points for g in data.point_grades} == {1: 10.0, 2: 5.0}


# ----------------------------------------------------------------------
# Reference data
# ----------------------------------------------------------------------


_REFERENCE_METHODS = (
    "async_get_subjects",
    "async_get_teachers",
    "async_get_classrooms",
    "async_get_schools",
    "async_get_classes",
    "async_get_homework_categories",
    "async_get_school_free_days",
    "async_get_class_free_days",
    "async_get_note_categories",
    "async_get_behaviour_grade_point_categories",
    "async_get_lessons",
    "async_get_text_grade_categories",
    "async_get_homework_assignment_categories",
    "async_get_units",
    "async_get_grading_system",
    "async_get_grade_categories",
    "async_get_attendance_types",
)


async def test_reference_data_requests_are_capped(hass) -> None:
    """Seventeen reference requests, at most six in flight at once."""
    client = build_mock_client()
    in_flight = {"now": 0, "max": 0}

    for name in _REFERENCE_METHODS:
        method = getattr(client, name)

        async def call(*_args, _result=method.return_value, **_kwargs):
            in_flight["now"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["now"])
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            in_flight["now"] -= 1
            return _result

        method.side_effect = call
    coordinator = _coordinator(hass, client)

    assert await coordinator._async_refresh_reference_data() is True

    assert in_flight["max"] == 6
    for name in _REFERENCE_METHODS:
        assert getattr(client, name).call_count == 1
