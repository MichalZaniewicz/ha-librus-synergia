"""Smart polling, hiding subjects without grades, and the event entities."""

from __future__ import annotations

from homeassistant.helpers import entity_registry as er

from custom_components.librus_synergia.const import DOMAIN, EVENT_NEW_ABSENCE, EVENT_NEW_NOTE

from .conftest import build_mock_client, setup_integration

# 23:00 US/Pacific (the harness's time zone) - inside the smart-polling night.
_NIGHT = "2026-09-10T06:00:00+00:00"


async def test_smart_polling_skips_at_night_but_not_on_request(hass, freezer) -> None:
    freezer.move_to(_NIGHT)
    client = build_mock_client()
    entry = await setup_integration(hass, client, options={"smart_polling": True})
    coordinator = entry.runtime_data
    calls = client.async_get_grades.call_count

    await coordinator.async_refresh()
    assert client.async_get_grades.call_count == calls  # skipped: fresh enough for the night

    await coordinator.async_force_refresh()
    await hass.async_block_till_done()
    assert client.async_get_grades.call_count == calls + 1  # the Refresh action always fetches


async def test_without_smart_polling_every_cycle_fetches(hass, freezer) -> None:
    freezer.move_to(_NIGHT)
    client = build_mock_client()
    entry = await setup_integration(hass, client)
    calls = client.async_get_grades.call_count

    await entry.runtime_data.async_refresh()
    assert client.async_get_grades.call_count == calls + 1


async def test_hide_subjects_without_grades(hass) -> None:
    client = build_mock_client(
        async_get_subjects={"Subjects": [{"Id": 1, "Name": "Matematyka"}, {"Id": 2, "Name": "Religia"}]},
        async_get_grades={"Grades": [{"Id": 1, "Grade": "5", "Subject": {"Id": 1}, "AddDate": "2026-09-02"}]},
    )
    entry = await setup_integration(hass, client, options={"hide_empty_subjects": True})
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_subject_1_average")
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_subject_2_average") is None


async def test_event_entities_follow_their_own_student(hass) -> None:
    entry = await setup_integration(hass, build_mock_client())
    registry = er.async_get(hass)
    note_id = registry.async_get_entity_id("event", DOMAIN, f"{entry.entry_id}_note_event")
    absence_id = registry.async_get_entity_id("event", DOMAIN, f"{entry.entry_id}_absence_event")
    assert note_id and absence_id

    # Another student's event is ignored.
    hass.bus.async_fire(EVENT_NEW_NOTE, {"entry_id": "other", "sentiment": "negative"})
    await hass.async_block_till_done()
    assert hass.states.get(note_id).attributes.get("event_type") is None

    hass.bus.async_fire(
        EVENT_NEW_NOTE,
        {"entry_id": entry.entry_id, "sentiment": "negative", "text": "Brak zadania", "student": "Ola"},
    )
    hass.bus.async_fire(EVENT_NEW_ABSENCE, {"entry_id": entry.entry_id, "excused": True})
    await hass.async_block_till_done()

    note = hass.states.get(note_id)
    assert note.attributes["event_type"] == "negative"
    assert note.attributes["text"] == "Brak zadania"
    assert "entry_id" not in note.attributes
    assert hass.states.get(absence_id).attributes["event_type"] == "excused"
