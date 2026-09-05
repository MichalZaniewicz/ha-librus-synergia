"""Tests for the Librus Synergia sensor platform."""

from __future__ import annotations

from homeassistant.helpers import entity_registry as er

from custom_components.librus_synergia.const import DOMAIN

from .conftest import build_mock_client, setup_integration


def _entity_id(hass, entry, key: str) -> str | None:
    registry = er.async_get(hass)
    return registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")


async def test_lucky_number_sensor(hass) -> None:
    client = build_mock_client()
    entry = await setup_integration(hass, client)

    entity_id = _entity_id(hass, entry, "lucky_number")
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "7"


async def test_attendance_sensor_counts_only_non_presence_types(hass) -> None:
    """CONFIRMED live: most attendance records are ordinary "present" marks
    (IsPresenceKind=true) - the sensor's primary state must count only the
    real absences, not every record."""
    client = build_mock_client(
        async_get_attendances={
            "Attendances": [
                {"Id": 1, "Date": "2026-09-01", "Type": {"Id": 100}},  # Obecność (present)
                {"Id": 2, "Date": "2026-09-02", "Type": {"Id": 100}},  # Obecność (present)
                {"Id": 3, "Date": "2026-09-03", "Type": {"Id": 1}},  # Nieobecność (absence)
            ]
        },
        async_get_attendance_types={
            "Types": [
                {"Id": 100, "Name": "Obecność", "IsPresenceKind": True},
                {"Id": 1, "Name": "Nieobecność", "IsPresenceKind": False},
            ]
        },
    )
    entry = await setup_integration(hass, client)

    entity_id = _entity_id(hass, entry, "attendance")
    state = hass.states.get(entity_id)
    assert state.state == "1"
    assert state.attributes["total_records"] == 3
    assert state.attributes["breakdown"] == {"Obecność": 2, "Nieobecność": 1}


async def test_dynamic_subject_average_sensor_is_discovered(hass) -> None:
    client = build_mock_client(
        async_get_subjects={"Subjects": [{"Id": 42005, "Name": "Matematyka"}]},
        async_get_grades={
            "Grades": [
                {
                    "Id": 1,
                    "Grade": "4+",
                    "Category": {"Id": 10},
                    "Subject": {"Id": 42005},
                    "Semester": 1,
                    "AddDate": "2026-09-01",
                }
            ]
        },
        async_get_grade_categories={
            "Categories": [
                {"Id": 10, "Name": "sprawdzian", "CountToTheAverage": True, "Weight": 2}
            ]
        },
    )
    entry = await setup_integration(hass, client)

    entity_id = _entity_id(hass, entry, "subject_42005_average")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert float(state.state) == 4.5  # "4+" == 4 + 0.5, per _parse_grade_value


async def test_unread_messages_sensor_unavailable_when_school_has_no_module(hass) -> None:
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = False
    entry = await setup_integration(hass, client)

    entity_id = _entity_id(hass, entry, "unread_messages")
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "unavailable"


async def test_unread_messages_sensor_exposes_mailbox_breakdown(hass) -> None:
    client = build_mock_client(
        async_bootstrap_messages=True,
        async_get_unread_messages_count={
            "data": {"inbox": 1, "notes": 2, "alerts": 0}
        },
        async_get_messages={"data": []},
    )
    entry = await setup_integration(hass, client)

    entity_id = _entity_id(hass, entry, "unread_messages")
    state = hass.states.get(entity_id)
    assert state.state == "1"
    assert state.attributes["mailbox_breakdown"]["inbox"] == 1
    assert state.attributes["mailbox_breakdown"]["notes"] == 2
    assert state.attributes["mailbox_breakdown"]["trash"] == 0


async def test_school_and_class_sensors(hass) -> None:
    client = build_mock_client(
        async_get_schools={
            "School": {
                "Name": "Zespół Szkolno-Przedszkolny nr 21",
                "Town": "Wrocław",
                "Street": "ul. Kłodzka",
                "NameHeadTeacher": "Edyta",
                "SurnameHeadTeacher": "Krajewska",
            }
        },
        async_get_classes={
            "Class": {
                "Number": 7,
                "Symbol": "d",
                "ClassTutor": {"Id": 1823984},
                "EndFirstSemester": "2027-01-31",
            }
        },
        async_get_teachers={
            "Users": [{"Id": 1823984, "FirstName": "Amelia", "LastName": "Marciszak"}]
        },
    )
    entry = await setup_integration(hass, client)

    school_entity_id = _entity_id(hass, entry, "school")
    school_state = hass.states.get(school_entity_id)
    assert school_state.state == "Zespół Szkolno-Przedszkolny nr 21"
    assert school_state.attributes["head_teacher"] == "Edyta Krajewska"

    class_entity_id = _entity_id(hass, entry, "school_class")
    class_state = hass.states.get(class_entity_id)
    assert class_state.state == "7d"
    assert class_state.attributes["homeroom_teacher"] == "Amelia Marciszak"
    assert class_state.attributes["first_semester_end"] == "2027-01-31"
