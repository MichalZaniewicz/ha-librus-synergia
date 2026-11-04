"""What the companion Recent activity card lists besides grades and
messages (issue #28): when Agenda entries and plan changes first showed up,
and the latest absences and late arrivals."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.util import dt as dt_util

from custom_components.librus_synergia.const import STATE_STORE_VERSION
from custom_components.librus_synergia.coordinator import (
    LibrusDataUpdateCoordinator,
    state_store_key,
)

from .conftest import build_mock_client, make_config_entry, saved_state, setup_integration


def _agenda(*ids: int) -> dict:
    day = (dt_util.now().date() + timedelta(days=5)).isoformat()
    return {
        "HomeWorks": [
            {"Id": i, "Date": day, "Content": f"Wpis {i}", "Category": {"Id": 3}, "Subject": {"Id": 100}}
            for i in ids
        ]
    }


def _changes(*ids: int) -> dict:
    day = (dt_util.now().date() + timedelta(days=2)).isoformat()
    return {
        "Substitutions": [
            {"Id": i, "OrgDate": day, "OrgLessonNo": 3, "OrgSubject": {"Id": 100}, "IsCancelled": True}
            for i in ids
        ]
    }


def _coordinator(hass, client, entry=None) -> LibrusDataUpdateCoordinator:
    entry = entry or make_config_entry()
    if entry.state is not ConfigEntryState.SETUP_IN_PROGRESS:
        entry.add_to_hass(hass)
        entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


async def _sync(hass, coordinator) -> None:
    coordinator._fetched_at.clear()
    await coordinator._async_update_data()
    await hass.async_block_till_done()


async def test_first_seen_starts_after_the_first_look(hass, hass_storage) -> None:
    client = build_mock_client(
        async_get_homeworks=_agenda(1),
        async_get_plan_changes=_changes(10),
    )
    coordinator = _coordinator(hass, client)
    await _sync(hass, coordinator)
    # Already there on the first look: no date.
    assert coordinator.first_seen("agenda", 1) is None
    assert coordinator.first_seen("plan_changes", 10) is None

    client.async_get_homeworks.return_value = _agenda(1, 2)
    client.async_get_plan_changes.return_value = _changes(10, 11)
    await _sync(hass, coordinator)
    assert coordinator.first_seen("agenda", 2)
    assert coordinator.first_seen("plan_changes", 11)
    assert coordinator.first_seen("agenda", 1) is None

    # An emptied fetch doesn't forget them; the dates survive a restart.
    client.async_get_homeworks.return_value = {"HomeWorks": []}
    await _sync(hass, coordinator)
    assert coordinator.first_seen("agenda", 2)

    entry = make_config_entry()
    hass_storage[state_store_key(entry.entry_id)] = {
        "version": STATE_STORE_VERSION,
        "minor_version": 1,
        "key": state_store_key(entry.entry_id),
        "data": saved_state(coordinator),
    }
    second = _coordinator(hass, build_mock_client(), entry)
    await second.async_restore_state()
    assert second.first_seen("agenda", 2) == coordinator.first_seen("agenda", 2)


async def test_recently_added_attributes(hass) -> None:
    client = build_mock_client(
        async_get_homeworks=_agenda(1),
        async_get_plan_changes=_changes(10),
        async_get_subjects={"Subjects": [{"Id": 100, "Name": "Matematyka"}]},
        async_get_homework_categories={"Categories": [{"Id": 3, "Name": "Sprawdzian"}]},
    )
    entry = await setup_integration(hass, client)
    coordinator = entry.runtime_data
    client.async_get_homeworks.return_value = _agenda(1, 2)
    client.async_get_plan_changes.return_value = _changes(10, 11)
    coordinator._fetched_at.clear()
    await coordinator.async_force_refresh()
    await hass.async_block_till_done()

    exam = next(
        s for s in hass.states.async_all("sensor") if "recently_added" in s.attributes and "this_week" in s.attributes
    )
    [added] = exam.attributes["recently_added"]
    assert (added["content"], added["subject"], added["category"]) == ("Wpis 2", "Matematyka", "Sprawdzian")
    assert added["seen"]

    cancelled = next(
        s for s in hass.states.async_all("sensor") if "recently_added" in s.attributes and "by_month" in s.attributes
    )
    [change] = cancelled.attributes["recently_added"]
    assert (change["kind"], change["subject"], change["lesson_no"]) == ("cancelled", "Matematyka", 3)


async def test_attendance_recent_lists_absences_and_late(hass) -> None:
    client = build_mock_client(
        async_get_attendances={
            "Attendances": [
                {"Id": 1, "Lesson": {"Id": 5}, "LessonNo": 1, "Date": "2026-11-02", "Semester": 1, "Type": {"Id": 100}},
                {"Id": 2, "Lesson": {"Id": 5}, "LessonNo": 2, "Date": "2026-11-03", "Semester": 1, "Type": {"Id": 1}},
                {"Id": 3, "Lesson": {"Id": 5}, "LessonNo": 3, "Date": "2026-11-04", "Semester": 1, "Type": {"Id": 2}},
                {"Id": 4, "Lesson": {"Id": 5}, "LessonNo": 4, "Date": "2026-11-04", "Semester": 1, "Type": {"Id": 3}},
            ]
        },
        async_get_attendance_types={
            "Types": [
                {"Id": 1, "Name": "Nieobecność", "Short": "nb", "IsPresenceKind": False},
                {"Id": 2, "Name": "Spóźnienie", "Short": "sp", "IsPresenceKind": True},
                {"Id": 3, "Name": "Nieobecność uspr.", "Short": "u", "IsPresenceKind": False},
                {"Id": 100, "Name": "Obecność", "Short": "ob", "IsPresenceKind": True},
            ]
        },
        async_get_lessons={"Lessons": [{"Id": 5, "Subject": {"Id": 100}}]},
        async_get_subjects={"Subjects": [{"Id": 100, "Name": "Matematyka"}]},
    )
    await setup_integration(hass, client)
    state = next(
        s for s in hass.states.async_all("sensor") if "presence_by_type" in s.attributes
    )
    assert [(m["date"], m["lesson_no"], m["kind"], m["subject"]) for m in state.attributes["recent"]] == [
        ("2026-11-04", 4, "excused", "Matematyka"),
        ("2026-11-04", 3, "late", "Matematyka"),
        ("2026-11-03", 2, "unexcused", "Matematyka"),
    ]
