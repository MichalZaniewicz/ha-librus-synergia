"""Tests for the Librus Synergia sensor platform."""

from __future__ import annotations

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

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


async def test_lucky_number_sensor_flags_whether_it_is_for_today(hass) -> None:
    """BUG FIX (2026-09-06, found live): the state was always presented as
    "today's" number without ever checking Librus's own `LuckyNumberDay`
    against the real current date - confirmed live (user cross-checked
    against the real Librus app on a Sunday) that Librus can publish the
    NEXT school day's number a day ahead, and this integration showed
    that value as if it were for today regardless. `day`/`is_today` let a
    card label it honestly instead of hardcoding "today"."""
    today_iso = dt_util.now().date().isoformat()
    client = build_mock_client(
        async_get_lucky_number={"LuckyNumber": {"LuckyNumber": 4, "LuckyNumberDay": today_iso}}
    )
    entry = await setup_integration(hass, client)

    state = hass.states.get(_entity_id(hass, entry, "lucky_number"))
    assert state.state == "4"
    assert state.attributes["day"] == today_iso
    assert state.attributes["is_today"] is True


async def test_lucky_number_sensor_flags_when_published_for_a_future_day(hass) -> None:
    client = build_mock_client(
        async_get_lucky_number={"LuckyNumber": {"LuckyNumber": 4, "LuckyNumberDay": "2099-01-01"}}
    )
    entry = await setup_integration(hass, client)

    state = hass.states.get(_entity_id(hass, entry, "lucky_number"))
    assert state.state == "4"
    assert state.attributes["day"] == "2099-01-01"
    assert state.attributes["is_today"] is False


async def test_attendance_sensor_counts_only_non_presence_types(hass) -> None:
    """CONFIRMED live: most attendance records are ordinary "present" marks
    (IsPresenceKind=true) - the sensor's primary state must count only the
    real absences, not every record."""
    client = build_mock_client(
        async_get_attendances={
            "Attendances": [
                {"Id": 1, "Date": "2026-09-01", "Semester": 1, "Type": {"Id": 100}},  # present
                {"Id": 2, "Date": "2026-09-02", "Semester": 1, "Type": {"Id": 100}},  # present
                {"Id": 3, "Date": "2026-09-03", "Semester": 2, "Type": {"Id": 1}},  # absence
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
    # BUG FIX (2026-09-06, found live): the companion cards used to guess
    # presence from the name text alone (a regex matching "obecno" as a
    # substring - which also matches inside "Nieobecność") and rendered
    # both with the same color. Expose the authoritative flag instead of
    # making every consumer re-guess it from Polish text.
    assert state.attributes["presence_by_type"] == {"Obecność": True, "Nieobecność": False}
    assert state.attributes["last_absence_date"] == "2026-09-03"
    # Independent % calculation (librusik-inspired, new 2026-09-06) - 2 of
    # 3 records are presence-kind.
    assert state.attributes["percentage"] == round(100 * 2 / 3, 1)
    assert state.attributes["by_semester"]["1"] == {"total": 2, "present": 2, "percentage": 100.0}
    assert state.attributes["by_semester"]["2"] == {"total": 1, "present": 0, "percentage": 0.0}


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
                    "Comments": [701],
                }
            ]
        },
        async_get_grade_categories={
            "Categories": [
                {"Id": 10, "Name": "sprawdzian", "CountToTheAverage": True, "Weight": 2}
            ]
        },
        async_get_grade_comments={"Comments": [{"Id": 701, "Text": "Świetna praca"}]},
    )
    entry = await setup_integration(hass, client)

    entity_id = _entity_id(hass, entry, "subject_42005_average")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state.attributes["latest_grade_comments"] == ["Świetna praca"]
    assert state.attributes["subject"] == "Matematyka"
    assert state.attributes["grades"] == [
        {"value": "4+", "category": "sprawdzian", "date": "2026-09-01", "comments": ["Świetna praca"]}
    ]
    assert float(state.state) == 4.5  # "4+" == 4 + 0.5, per _parse_grade_value


async def test_behaviour_notices_sensor_resolves_category_name(hass) -> None:
    """CONFIRMED live (2026-09-06): Notes/Categories is real and populated -
    the `recent` attribute must resolve category_id to the real name, not
    just show the raw id."""
    client = build_mock_client(
        async_get_notes={
            "Notes": [
                {
                    "Id": 1,
                    "Text": "Wzorowa postawa na lekcji",
                    "Category": {"Id": 855},
                    "Date": "2026-09-05",
                    "Positive": 1,
                }
            ]
        },
        async_get_note_categories={
            "Categories": [{"Id": 855, "CategoryName": "Praca na lekcji"}]
        },
    )
    entry = await setup_integration(hass, client)

    entity_id = _entity_id(hass, entry, "behaviour_notices")
    state = hass.states.get(entity_id)
    assert state.state == "1"
    assert state.attributes["recent"][0]["category"] == "Praca na lekcji"


async def test_unread_announcements_sensor_exposes_recent_details(hass) -> None:
    """The `recent` attribute must carry more than just a bare subject list
    (content preview + dates), matching the Behaviour notices/Unread
    messages sensors' established pattern."""
    client = build_mock_client(
        async_get_school_notices={
            "SchoolNotices": [
                {
                    "Id": "LID-NBOARD-NOTICE-9093-1",
                    "Subject": "Kandydaci do Rady Samorządu Uczniowskiego",
                    "Content": "Treść ogłoszenia...",
                    "StartDate": "2026-09-01",
                    "EndDate": "2026-09-30",
                    "CreationDate": "2026-09-01",
                    "WasRead": False,
                }
            ]
        }
    )
    entry = await setup_integration(hass, client)

    entity_id = _entity_id(hass, entry, "unread_announcements")
    state = hass.states.get(entity_id)
    assert state.state == "1"
    recent = state.attributes["recent"]
    assert len(recent) == 1
    assert recent[0]["subject"] == "Kandydaci do Rady Samorządu Uczniowskiego"
    assert recent[0]["content"] == "Treść ogłoszenia..."
    assert recent[0]["start_date"] == "2026-09-01"
    assert recent[0]["end_date"] == "2026-09-30"


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


async def test_unread_messages_sensor_recent_includes_message_id(hass) -> None:
    """The `get_message` service needs a real message id from somewhere a
    card can reach - this is that somewhere. Without it there'd be no way
    for a dashboard to know WHICH message to ask the service to fetch."""
    client = build_mock_client(
        async_bootstrap_messages=True,
        async_get_unread_messages_count={"data": {"inbox": 1}},
        async_get_messages={
            "data": [
                {
                    "messageId": "186536",
                    "senderName": "Marciszak Amelia",
                    "topic": "Zebranie z rodzicami",
                    "content": "RHppZWQgZG9icnk=",
                    "sendDate": "2026-09-04T17:47:10",
                    "readDate": None,
                    "isAnyFileAttached": False,
                }
            ]
        },
    )
    entry = await setup_integration(hass, client)

    entity_id = _entity_id(hass, entry, "unread_messages")
    state = hass.states.get(entity_id)
    assert state.attributes["recent"][0]["id"] == "186536"
    assert state.attributes["recent"][0]["mailbox"] == "inbox"


async def test_unread_messages_sensor_exposes_secondary_mailbox_content(hass) -> None:
    """New (2026-09-06, librusik-inspired): substitutions/alerts get full
    content, not just a count - each tagged with its own mailbox so a
    card can pass the right value back to the `get_message` service."""
    client = build_mock_client(async_bootstrap_messages=True)
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 0}}
    client.async_get_messages.side_effect = [
        {"data": []},
        {
            "data": [
                {
                    "messageId": "1",
                    "senderName": "Sekretariat",
                    "topic": "Zmiana w planie",
                    "content": "RHppZWQgZG9icnk=",
                    "sendDate": "2026-09-04T10:00:00",
                    "readDate": None,
                    "isAnyFileAttached": False,
                }
            ]
        },
        {
            "data": [
                {
                    "messageId": "2",
                    "senderName": "Dyrekcja",
                    "topic": "Alert",
                    "content": "RHppZWQgZG9icnk=",
                    "sendDate": "2026-09-04T11:00:00",
                    "readDate": None,
                    "isAnyFileAttached": False,
                }
            ]
        },
    ]
    entry = await setup_integration(hass, client)

    entity_id = _entity_id(hass, entry, "unread_messages")
    state = hass.states.get(entity_id)
    assert state.attributes["substitutions_recent"][0]["id"] == "1"
    assert state.attributes["substitutions_recent"][0]["mailbox"] == "substitutions"
    assert state.attributes["substitutions_recent"][0]["topic"] == "Zmiana w planie"
    assert state.attributes["alerts_recent"][0]["id"] == "2"
    assert state.attributes["alerts_recent"][0]["mailbox"] == "alerts"


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


async def test_homework_assignments_sensor(hass) -> None:
    client = build_mock_client(
        async_get_homework_assignments={
            "HomeWorkAssignments": [
                {
                    "Id": 1,
                    "Topic": "Zadanie 5",
                    "Text": "Strona 42",
                    "Teacher": {"Id": 200},
                    "Date": "2026-09-01",
                    "DueDate": "2026-09-08",
                }
            ]
        },
        async_get_teachers={"Users": [{"Id": 200, "FirstName": "Jan", "LastName": "Kowalski"}]},
    )
    entry = await setup_integration(hass, client)

    entity_id = _entity_id(hass, entry, "homework_assignments")
    state = hass.states.get(entity_id)
    assert state.state == "1"
    assert state.attributes["recent"][0]["topic"] == "Zadanie 5"
    assert state.attributes["recent"][0]["teacher"] == "Jan Kowalski"


async def test_behaviour_grade_sensor(hass) -> None:
    client = build_mock_client(
        async_get_behaviour_grade_points={
            "Grades": [
                {
                    "Id": 1,
                    "Value": 5.0,
                    "ShortName": "wz",
                    "Category": {"Id": 21823},
                    "AddDate": "2026-09-05",
                    "Text": "Wzorowe zachowanie",
                }
            ]
        },
        async_get_behaviour_grade_point_categories={
            "Categories": [{"Id": 21823, "Name": "zachowanie"}]
        },
    )
    entry = await setup_integration(hass, client)

    entity_id = _entity_id(hass, entry, "behaviour_grade")
    state = hass.states.get(entity_id)
    assert state.state == "wz"
    assert state.attributes["recent"][0]["category"] == "zachowanie"


async def test_descriptive_grades_sensor(hass) -> None:
    client = build_mock_client(
        async_get_descriptive_grades={
            "Grades": [
                {
                    "Id": 1,
                    "Subject": {"Id": 100},
                    "Grade": "Bardzo dobrze",
                    "AddDate": "2026-09-05",
                }
            ]
        },
        async_get_subjects={"Subjects": [{"Id": 100, "Name": "Matematyka"}]},
    )
    entry = await setup_integration(hass, client)

    entity_id = _entity_id(hass, entry, "descriptive_grades")
    state = hass.states.get(entity_id)
    assert state.state == "1"
    assert state.attributes["recent"][0]["subject"] == "Matematyka"
