"""School library lendings (EbiblioLendings) and the school year's plan
changes (Calendars/Substitutions)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.librus_synergia.const import DOMAIN, EVENT_LIBRARY_CHANGED
from custom_components.librus_synergia.coordinator import LibrusDataUpdateCoordinator
from librus_synergia import LibrusSessionExpiredError, LibrusUnexpectedResponseError

from .conftest import build_mock_client, make_config_entry, setup_integration

SUBJECTS = {"Subjects": [{"Id": 100, "Name": "Matematyka"}, {"Id": 101, "Name": "Historia"}]}
TEACHERS = {"Users": [{"Id": 7, "FirstName": "Anna", "LastName": "Nowak"}]}


def _book(book_id: str, title: str) -> dict:
    return {
        "identifier": book_id,
        "type": "BOOK",
        "title": title,
        "author": "Kowalska Ola",
        "lendedAt": "2026-09-04",
        "dueDate": "2026-09-18",
        "isExtendable": False,
    }


TWO_BOOKS = {"lendings": [_book("a", "Matematyka 7"), _book("b", "Historia 7")]}
PLAN_CHANGES = {
    "Substitutions": [
        {"Id": 1, "OrgDate": "2026-09-15", "OrgLessonNo": "2", "OrgSubject": {"Id": 100},
         "OrgTeacher": {"Id": 7}, "OrgClassroomSymbol": "233", "IsCancelled": True, "IsShifted": False},
        {"Id": 2, "OrgDate": "2026-09-15", "OrgLessonNo": "3", "OrgSubject": {"Id": 100},
         "OrgTeacher": {"Id": 7}, "OrgClassroomSymbol": "233", "IsCancelled": True, "IsShifted": False},
        {"Id": 3, "Date": "2026-10-01", "LessonNo": "4", "Subject": {"Id": 101}, "Teacher": {"Id": 7},
         "ClassroomSymbol": "h106", "OrgDate": "2026-10-01", "OrgLessonNo": "4", "OrgSubject": {"Id": 101},
         "OrgTeacher": {"Id": 8}, "OrgClassroomSymbol": "h106", "IsCancelled": False, "IsShifted": False},
    ]
}


def _coordinator(hass, client) -> LibrusDataUpdateCoordinator:
    entry = make_config_entry()
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


def _entity(hass, entry, key: str):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
    return hass.states.get(entity_id)


async def test_library_and_plan_change_sensors(hass) -> None:
    client = build_mock_client(
        async_get_subjects=SUBJECTS,
        async_get_teachers=TEACHERS,
        async_get_library_lendings=TWO_BOOKS,
        async_get_plan_changes=PLAN_CHANGES,
        async_get_lessons={"Lessons": [{"Id": 3, "Subject": {"Id": 100}}]},
        async_get_realizations={
            "Realizations": [
                {"Id": f"t{i}", "Lesson": {"Id": 3}, "LessonNo": 1, "Date": "2026-10-01", "Topic": "x"}
                for i in range(18)
            ]
        },
    )
    entry = await setup_integration(hass, client)

    books = _entity(hass, entry, "library_books")
    assert books.state == "2"
    assert [b["title"] for b in books.attributes["books"]] == ["Matematyka 7", "Historia 7"]
    assert books.attributes["books"][0]["due_date"] == "2026-09-18"

    cancelled = _entity(hass, entry, "cancelled_lessons")
    assert cancelled.state == "2"
    assert cancelled.attributes["substitutions"] == 1
    assert cancelled.attributes["since"] == "2026-09-15"
    assert cancelled.attributes["lessons_held"] == 18
    assert cancelled.attributes["cancelled_percent"] == 10.0
    assert cancelled.attributes["by_subject"] == {
        "Matematyka": {"cancelled": 2, "substitutions": 0},
        "Historia": {"cancelled": 0, "substitutions": 1},
    }
    assert cancelled.attributes["by_month"] == {
        "2026-09": {"cancelled": 2, "substitutions": 0},
        "2026-10": {"cancelled": 0, "substitutions": 1},
    }
    latest = cancelled.attributes["recent"][0]
    assert (latest["date"], latest["kind"], latest["new_teacher"]) == (
        "2026-10-01",
        "substitution",
        "Anna Nowak",
    )


async def test_library_events_seed_then_lent_and_returned(hass) -> None:
    events = async_capture_events(hass, EVENT_LIBRARY_CHANGED)
    client = build_mock_client(async_get_library_lendings=TWO_BOOKS)
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()  # first sync only seeds
    await hass.async_block_till_done()
    assert events == []

    client.async_get_library_lendings.return_value = {
        "lendings": [_book("b", "Historia 7"), _book("c", "Lektura")]
    }
    coordinator._fetched_at.clear()  # lendings are fetched hourly
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert sorted((e.data["action"], e.data["title"]) for e in events) == [
        ("lent", "Lektura"),
        ("returned", "Matematyka 7"),
    ]


async def test_library_failed_or_emptied_fetch_fires_nothing(hass) -> None:
    events = async_capture_events(hass, EVENT_LIBRARY_CHANGED)
    client = build_mock_client(async_get_library_lendings=TWO_BOOKS)
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    client.async_get_library_lendings.side_effect = LibrusUnexpectedResponseError(
        "boom", status_code=500
    )
    coordinator._fetched_at.clear()
    data = await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert len(data.library_lendings) == 2  # the last good copy
    assert "EbiblioLendings" in coordinator._failed_this_cycle

    client.async_get_library_lendings.side_effect = None
    client.async_get_library_lendings.return_value = {"lendings": []}
    coordinator._fetched_at.clear()
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert events == []


async def test_refused_module_is_an_empty_answer_not_a_failure(hass) -> None:
    client = build_mock_client()
    client.async_get_library_lendings.side_effect = LibrusSessionExpiredError(
        "no module", status_code=403
    )
    client.async_get_plan_changes.side_effect = LibrusUnexpectedResponseError(
        "Insufficient scopes", status_code=401
    )
    coordinator = _coordinator(hass, client)
    data = await coordinator._async_update_data()
    assert data.library_lendings == [] and data.plan_changes == []
    assert "EbiblioLendings" not in coordinator._failed_this_cycle
    assert "Calendars/Substitutions" not in coordinator._failed_this_cycle

    # Asked again on its own hourly schedule, not every cycle.
    await coordinator._async_update_data()
    assert client.async_get_library_lendings.call_count == 1
