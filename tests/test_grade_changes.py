"""A grade the teacher edits or deletes after it was announced fires
librus_synergia_grade_changed (`change: changed` / `removed`)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_mock_service,
)

from custom_components.librus_synergia import logbook as librus_logbook
from custom_components.librus_synergia.const import (
    EVENT_GRADE_CHANGED,
    EVENT_NEW_GRADE,
    STATE_STORE_VERSION,
)
from custom_components.librus_synergia.coordinator import (
    LibrusDataUpdateCoordinator,
    state_store_key,
)
from librus_synergia import LibrusConnectionError

from .conftest import build_mock_client, make_config_entry, saved_state, setup_integration

CATEGORIES = {
    "Categories": [
        {"Id": 10, "Name": "Sprawdzian", "Weight": 1, "CountToTheAverage": True},
        {"Id": 11, "Name": "Kartkówka", "Weight": 1, "CountToTheAverage": True},
    ]
}
SUBJECTS = {"Subjects": [{"Id": 100, "Name": "Matematyka"}, {"Id": 200, "Name": "Fizyka"}]}


def _grade(grade_id: int, value: str, *, category: int = 10, subject: int = 100) -> dict:
    return {
        "Id": grade_id,
        "Grade": value,
        "Category": {"Id": category},
        "Subject": {"Id": subject},
        "Semester": 1,
        "AddDate": "2026-11-03 08:29:34",
        "AddedBy": {"Id": 7},
    }


def _grades(*grades: dict) -> dict:
    return {"Grades": list(grades)}


def _coordinator(hass, client, entry=None) -> LibrusDataUpdateCoordinator:
    entry = entry or make_config_entry()
    if entry.state is not ConfigEntryState.SETUP_IN_PROGRESS:
        entry.add_to_hass(hass)
        entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


def _client(*grades: dict, **overrides):
    return build_mock_client(
        async_get_grades=_grades(*grades),
        async_get_grade_categories=CATEGORIES,
        async_get_subjects=SUBJECTS,
        **overrides,
    )


async def _sync(hass, coordinator) -> None:
    coordinator._fetched_at.clear()
    await coordinator._async_update_data()
    await hass.async_block_till_done()


async def test_edited_grade_fires_changed_with_previous_value_and_averages(hass) -> None:
    changes = async_capture_events(hass, EVENT_GRADE_CHANGED)
    new = async_capture_events(hass, EVENT_NEW_GRADE)
    client = _client(_grade(1, "3"), _grade(2, "1"))
    coordinator = _coordinator(hass, client)
    await _sync(hass, coordinator)  # first sync only seeds
    assert changes == []

    # The teacher corrects the 1 to a 5 (same grade id).
    client.async_get_grades.return_value = _grades(_grade(1, "3"), _grade(2, "5"))
    await _sync(hass, coordinator)

    [event] = changes
    data = event.data
    assert (data["id"], data["change"], data["value"]) == (2, "changed", "5")
    assert data["changed_fields"] == ["value"]
    assert data["previous"] == {"value": "1"}
    assert (data["subject"], data["category"], data["kind"]) == ("Matematyka", "Sprawdzian", "normal")
    assert data["average_before"] == 2.0
    assert data["average_after"] == 4.0
    assert data["entry_id"] == coordinator.config_entry.entry_id
    assert new == []  # an edit isn't a new grade

    await _sync(hass, coordinator)  # announced once
    assert len(changes) == 1


async def test_weight_change_is_an_edit_too(hass) -> None:
    changes = async_capture_events(hass, EVENT_GRADE_CHANGED)
    client = _client(_grade(1, "3"), _grade(2, "5", category=11))
    coordinator = _coordinator(hass, client)
    await _sync(hass, coordinator)

    # Moved to a category that counts double.
    client.async_get_grade_categories.return_value = {
        "Categories": [
            *CATEGORIES["Categories"],
            {"Id": 12, "Name": "Praca klasowa", "Weight": 2, "CountToTheAverage": True},
        ]
    }
    client.async_get_grades.return_value = _grades(_grade(1, "3"), _grade(2, "5", category=12))
    await _sync(hass, coordinator)

    [event] = changes
    assert event.data["changed_fields"] == ["weight"]
    assert event.data["previous"] == {"weight": 1}
    assert event.data["category"] == "Praca klasowa"
    assert event.data["average_before"] == 4.0
    assert event.data["average_after"] == 4.33


async def test_deleted_grade_fires_removed_after_two_syncs(hass) -> None:
    changes = async_capture_events(hass, EVENT_GRADE_CHANGED)
    client = _client(_grade(1, "3"), _grade(2, "1"))
    coordinator = _coordinator(hass, client)
    await _sync(hass, coordinator)

    client.async_get_grades.return_value = _grades(_grade(1, "3"))
    await _sync(hass, coordinator)
    assert changes == []  # missing once: not yet

    await _sync(hass, coordinator)
    [event] = changes
    data = event.data
    assert (data["id"], data["change"], data["value"]) == (2, "removed", "1")
    assert (data["subject"], data["category"]) == ("Matematyka", "Sprawdzian")
    assert data["average_before"] == 2.0
    assert data["average_after"] == 3.0
    assert "previous" not in data

    await _sync(hass, coordinator)
    assert len(changes) == 1


async def test_grade_missing_from_one_sync_is_not_removed(hass) -> None:
    changes = async_capture_events(hass, EVENT_GRADE_CHANGED)
    client = _client(_grade(1, "3"), _grade(2, "1"))
    coordinator = _coordinator(hass, client)
    await _sync(hass, coordinator)

    client.async_get_grades.return_value = _grades(_grade(1, "3"))
    await _sync(hass, coordinator)
    client.async_get_grades.return_value = _grades(_grade(1, "3"), _grade(2, "1"))
    await _sync(hass, coordinator)
    client.async_get_grades.return_value = _grades(_grade(1, "3"))
    await _sync(hass, coordinator)

    assert changes == []


async def test_failed_or_emptied_fetch_is_not_announced(hass) -> None:
    changes = async_capture_events(hass, EVENT_GRADE_CHANGED)
    client = _client(_grade(1, "3"), _grade(2, "1"))
    coordinator = _coordinator(hass, client)
    await _sync(hass, coordinator)

    client.async_get_grades.side_effect = LibrusConnectionError("down")
    await _sync(hass, coordinator)
    await _sync(hass, coordinator)
    client.async_get_grades.side_effect = None
    client.async_get_grades.return_value = {"Grades": []}
    await _sync(hass, coordinator)
    await _sync(hass, coordinator)
    assert changes == []

    # Back to normal: an edit made meanwhile is still found.
    client.async_get_grades.return_value = _grades(_grade(1, "3"), _grade(2, "2"))
    await _sync(hass, coordinator)
    assert [(e.data["id"], e.data["previous"]) for e in changes] == [(2, {"value": "1"})]


async def test_many_changes_at_once_are_recorded_quietly(hass) -> None:
    """The school reweighting a whole category (or a new school year)
    touches many grades at once - that isn't a teacher correcting one."""
    changes = async_capture_events(hass, EVENT_GRADE_CHANGED)
    client = _client(*(_grade(i, "4") for i in range(1, 8)))
    coordinator = _coordinator(hass, client)
    await _sync(hass, coordinator)

    # Every grade moved to a category that counts triple.
    client.async_get_grade_categories.return_value = {
        "Categories": [
            *CATEGORIES["Categories"],
            {"Id": 12, "Name": "Sprawdzian", "Weight": 3, "CountToTheAverage": True},
        ]
    }
    reweighted = [_grade(i, "4", category=12) for i in range(1, 8)]
    client.async_get_grades.return_value = _grades(*reweighted)
    await _sync(hass, coordinator)
    await _sync(hass, coordinator)
    assert changes == []
    assert coordinator._known_grades["grades"]["1"]["weight"] == 3

    # The new weights are the baseline now: the next edit is announced.
    client.async_get_grades.return_value = _grades(*reweighted[:-1], _grade(7, "5", category=12))
    await _sync(hass, coordinator)
    assert [e.data["previous"] for e in changes] == [{"value": "4"}]


async def test_point_grade_edit(hass) -> None:
    changes = async_capture_events(hass, EVENT_GRADE_CHANGED)
    categories = {
        "Categories": [
            {"Id": 1, "Name": "Lutowanie", "Weight": 1, "CountToTheAverage": True, "ValueTo": 10}
        ]
    }

    def point(points: int) -> dict:
        return {
            "Id": 5, "Grade": str(points), "GradeValue": points, "Category": {"Id": 1},
            "Subject": {"Id": 200}, "Semester": 1, "AddDate": "2026-11-03 08:35:10",
        }

    client = _client(
        async_get_point_grades={"Grades": [point(8)]},
        async_get_point_grade_categories=categories,
    )
    coordinator = _coordinator(hass, client)
    await _sync(hass, coordinator)

    client.async_get_point_grades.return_value = {"Grades": [point(10)]}
    await _sync(hass, coordinator)

    [event] = changes
    data = event.data
    assert (data["kind"], data["change"], data["subject"]) == ("point", "changed", "Fizyka")
    assert "value" in data["changed_fields"] and "points" in data["changed_fields"]
    assert data["previous"]["points"] == 8.0
    assert data["points"] == 10.0
    assert "average_before" not in data  # like EVENT_NEW_GRADE for point grades


async def test_grade_change_survives_restart(hass, hass_storage) -> None:
    first = _coordinator(hass, _client(_grade(1, "3"), _grade(2, "1")))
    await _sync(hass, first)
    saved = saved_state(first)
    assert saved["grades"]["grades"]["2"]["value"] == "1"

    changes = async_capture_events(hass, EVENT_GRADE_CHANGED)
    entry = make_config_entry()
    hass_storage[state_store_key(entry.entry_id)] = {
        "version": STATE_STORE_VERSION,
        "minor_version": 1,
        "key": state_store_key(entry.entry_id),
        "data": saved,
    }
    second = _coordinator(hass, _client(_grade(1, "3"), _grade(2, "5")), entry)
    await second.async_restore_state()
    await _sync(hass, second)

    assert [(e.data["id"], e.data["previous"]) for e in changes] == [(2, {"value": "1"})]


async def test_state_without_saved_grades_seeds_silently(hass, hass_storage) -> None:
    """Upgrading from a version without the saved grades: the first sync
    seeds them instead of announcing anything."""
    first = _coordinator(hass, _client(_grade(1, "3")))
    await _sync(hass, first)
    saved = saved_state(first)
    del saved["grades"]

    changes = async_capture_events(hass, EVENT_GRADE_CHANGED)
    entry = make_config_entry()
    hass_storage[state_store_key(entry.entry_id)] = {
        "version": STATE_STORE_VERSION,
        "minor_version": 1,
        "key": state_store_key(entry.entry_id),
        "data": saved,
    }
    second = _coordinator(hass, _client(_grade(1, "5")), entry)
    await second.async_restore_state()
    await _sync(hass, second)
    assert changes == []
    assert second._known_grades["grades"]["1"]["value"] == "5"


async def test_grade_changed_trigger_and_event_entity(hass) -> None:
    entry = await setup_integration(hass, build_mock_client())
    coordinator = entry.runtime_data
    device = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)[0]
    calls = async_mock_service(hass, "test", "automation")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "triggers": [
                    {
                        "trigger": "librus_synergia.grade_changed",
                        "target": {"device_id": device.id},
                        "options": {"change": ["removed"], "subject": "fizyka"},
                    }
                ],
                "actions": [{"action": "test.automation", "data": {"value": "{{ trigger.value }}"}}],
            }
        },
    )
    base = {"entry_id": entry.entry_id, "kind": "normal", "category": "Sprawdzian"}
    coordinator.async_fire_event(
        EVENT_GRADE_CHANGED, {**base, "id": 1, "change": "changed", "subject": "Fizyka", "value": "5"}
    )
    coordinator.async_fire_event(
        EVENT_GRADE_CHANGED, {**base, "id": 2, "change": "removed", "subject": "Matematyka", "value": "1"}
    )
    coordinator.async_fire_event(
        EVENT_GRADE_CHANGED, {**base, "id": 3, "change": "removed", "subject": "Fizyka", "value": "2"}
    )
    await hass.async_block_till_done()
    assert [call.data for call in calls] == [{"value": 2}]  # rendered as a number

    states = [
        s for s in hass.states.async_all("event") if s.attributes.get("event_type") in ("changed", "removed")
        and "event_types" in s.attributes and set(s.attributes["event_types"]) == {"changed", "removed"}
        and s.attributes.get("id") == 3
    ]
    assert len(states) == 1
    assert states[0].attributes["event_type"] == "removed"


def test_logbook_lines() -> None:
    changed = {
        "student": "Ola", "change": "changed", "subject": "Język niemiecki", "category": "prezentacje",
        "value": "5", "kind": "normal", "previous": {"value": "1"},
    }
    assert librus_logbook.describe(EVENT_GRADE_CHANGED, changed, "pl")["message"] == (
        "zmieniona ocena 1 → 5 - Język niemiecki (prezentacje)"
    )
    removed = {**changed, "change": "removed", "value": "1"}
    del removed["previous"]
    assert librus_logbook.describe(EVENT_GRADE_CHANGED, removed, "en")["message"] == (
        "grade removed 1 - Język niemiecki (prezentacje)"
    )
    weight = {**changed, "previous": {"weight": 1}}
    assert librus_logbook.describe(EVENT_GRADE_CHANGED, weight, "pl")["message"] == (
        "zmieniona ocena 5 - Język niemiecki (prezentacje)"
    )
