"""Text grades, lesson topics, school trips and documents, homework
categories, the student number from JSON and attachment download."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.librus_synergia.const import (
    DOMAIN,
    EVENT_NEW_GRADE,
    EVENT_NEW_SCHOOL_DOCUMENT,
    EVENT_NEW_SCHOOL_TRIP,
)
from custom_components.librus_synergia.coordinator import LibrusDataUpdateCoordinator
from librus_synergia import LibrusSessionExpiredError
from librus_synergia.models import AttachmentFileData

from .conftest import build_mock_client, make_config_entry, setup_integration

SUBJECTS = {"Subjects": [{"Id": 100, "Name": "Matematyka"}]}
TEXT_GRADES = {
    "Grades": [
        {"Id": 1, "Grade": "Bardzo dobrze opanowany materiał", "Subject": {"Id": 100}, "Category": {"Id": 5},
         "AddedBy": {"Id": 7}, "Date": "2026-09-18", "AddDate": "2026-09-18 11:25:36", "Semester": 1,
         "ShowInGradesView": True}
    ]
}
TEXT_CATEGORIES = {"Categories": [{"Id": 5, "Name": "zadanie", "CountToTheAverage": False}]}
TRIPS = {
    "Data": [
        {"id": 11, "destination": "Muzeum Narodowe", "route": "Szkoła - Muzeum", "locomotion": "autokar",
         "termFrom": "2026-10-08", "termTo": "2026-10-08", "coordinatorName": "Nowak Anna"}
    ]
}
FILES = {"Data": [{"id": "17613", "displayName": "Regulamin wycieczek", "addedOnDate": "2026-09-04 11:26:03",
                   "downloadUrl": "/pliki_szkoly/pobierz/1"}]}


def _coordinator(hass, client, *, options: dict | None = None) -> LibrusDataUpdateCoordinator:
    entry = make_config_entry(options=options)
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


def _entity(hass, entry, key: str):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
    return hass.states.get(entity_id)


async def test_text_grade_on_subject_sensor_and_in_get_grades(hass) -> None:
    client = build_mock_client(
        async_get_subjects=SUBJECTS,
        async_get_base_text_grades=TEXT_GRADES,
        async_get_text_grade_categories=TEXT_CATEGORIES,
    )
    entry = await setup_integration(hass, client)

    subject = _entity(hass, entry, "subject_100_average")
    assert subject.attributes["text_grades"][0]["value"] == "Bardzo dobrze opanowany materiał"
    assert subject.attributes["text_grades"][0]["category"] == "zadanie"

    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    response = await hass.services.async_call(
        DOMAIN, "get_grades", {"device_id": device.id}, blocking=True, return_response=True
    )
    assert response["text_grades"][0]["subject"] == "Matematyka"


async def test_new_text_grade_trip_and_document_fire_events(hass) -> None:
    grades = async_capture_events(hass, EVENT_NEW_GRADE)
    trips = async_capture_events(hass, EVENT_NEW_SCHOOL_TRIP)
    documents = async_capture_events(hass, EVENT_NEW_SCHOOL_DOCUMENT)
    client = build_mock_client(async_get_subjects=SUBJECTS)
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()  # first sync only seeds

    client.async_get_base_text_grades.return_value = TEXT_GRADES
    client.async_get_school_trips.return_value = TRIPS
    client.async_get_school_files.return_value = FILES
    coordinator._fetched_at.clear()  # trips/documents are fetched hourly
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert [e.data["kind"] for e in grades] == ["text"]
    assert grades[0].data["value"] == "Bardzo dobrze opanowany materiał"
    assert trips[0].data["destination"] == "Muzeum Narodowe"
    assert documents[0].data["url"] == "https://synergia.librus.pl/pliki_szkoly/pobierz/1"


DESCRIPTIVE = {
    "Grades": [
        {"Id": 11, "Subject": {"Id": 100}, "Skill": {"Id": 55}, "AddedBy": {"Id": 7}, "Grade": 3, "Map": "6",
         "Date": "2026-09-30", "AddDate": "2026-09-30 13:37:00", "Semester": 1, "Comments": [{"Id": 44}]}
    ]
}


async def test_new_descriptive_grade_fires_new_grade_event(hass) -> None:
    grades = async_capture_events(hass, EVENT_NEW_GRADE)
    client = build_mock_client(
        async_get_subjects=SUBJECTS,
        async_get_descriptive_grade_skills={"Skills": [{"Id": 55, "Name": "Rytmika"}]},
        async_get_descriptive_grade_comments={"Comments": [{"Id": 44, "Text": "Brawo"}]},
    )
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()  # first sync only seeds

    client.async_get_descriptive_grades.return_value = DESCRIPTIVE
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(grades) == 1
    data = grades[0].data
    assert (data["kind"], data["value"], data["subject"]) == ("descriptive", "6", "Matematyka")
    assert (data["skill"], data["category"], data["comments"]) == ("Rytmika", "Rytmika", ["Brawo"])
    assert data["counts_to_average"] is False


async def test_descriptive_grades_turned_on_later_are_seeded_silently(hass) -> None:
    grades = async_capture_events(hass, EVENT_NEW_GRADE)
    client = build_mock_client(async_get_subjects=SUBJECTS, async_get_descriptive_grades=DESCRIPTIVE)
    coordinator = _coordinator(hass, client, options={"descriptive_grades_enabled": False})
    await coordinator._async_update_data()
    await coordinator._async_update_data()

    hass.config_entries.async_update_entry(
        coordinator.config_entry, options={"descriptive_grades_enabled": True}
    )
    await coordinator._async_update_data()  # first sync with them on: seeds
    await hass.async_block_till_done()

    assert grades == []


async def test_lesson_topics_trips_and_documents_sensors(hass, freezer) -> None:
    freezer.move_to("2026-10-07T10:00:00+00:00")
    client = build_mock_client(
        async_get_subjects=SUBJECTS,
        async_get_lessons={"Lessons": [{"Id": 3, "Subject": {"Id": 100}}]},
        async_get_realizations={
            "Realizations": [
                {"Id": "t1", "Lesson": {"Id": 3}, "LessonNo": 2, "Date": "2026-10-07", "Topic": "Ułamki zwykłe"},
                {"Id": "t2", "Lesson": {"Id": 3}, "LessonNo": 1, "Date": "2026-10-06", "Topic": "Powtórzenie"},
            ]
        },
        async_get_school_trips=TRIPS,
        async_get_school_files=FILES,
        async_get_attendances={"Attendances": [{"Id": 1, "Date": "2026-10-06", "LessonNo": "1", "Type": {"Id": 1}}]},
        async_get_attendance_types={"Types": [{"Id": 1, "Name": "Nieobecność", "IsPresenceKind": False}]},
    )
    entry = await setup_integration(hass, client)

    topics = _entity(hass, entry, "lesson_topics")
    assert topics.state == "1"
    assert topics.attributes["today"][0]["topic"] == "Ułamki zwykłe"
    assert topics.attributes["today"][0]["subject"] == "Matematyka"
    assert len(topics.attributes["recent"]) == 2
    assert [r["absent"] for r in topics.attributes["recent"]] == [False, True]

    trip = _entity(hass, entry, "school_trips")
    assert trip.state == "2026-10-08"
    assert trip.attributes["destination"] == "Muzeum Narodowe"
    assert trip.attributes["days_until"] == 1

    documents = _entity(hass, entry, "school_documents")
    assert documents.state == "1"
    assert documents.attributes["recent"][0]["name"] == "Regulamin wycieczek"


async def test_homework_category_name(hass) -> None:
    client = build_mock_client(
        async_get_homework_assignments={
            "HomeWorkAssignments": [
                {"Id": 1, "Topic": "Ćwiczenia", "Text": "str. 12", "Teacher": {"Id": 7},
                 "Date": "2026-10-06", "DueDate": "2026-10-09", "Category": {"Id": 9}}
            ]
        },
        async_get_homework_assignment_categories={"Categories": [{"Id": 9, "CategoryName": "gramatyka"}]},
    )
    entry = await setup_integration(hass, client)

    homework = _entity(hass, entry, "homework_assignments")
    assert homework.attributes["recent"][0]["category"] == "gramatyka"


async def test_student_number_from_the_users_record(hass) -> None:
    client = build_mock_client(
        async_get_me={"Me": {"Account": {"UserId": 77}, "User": {"FirstName": "Ola", "LastName": "Kowalska"}}},
        async_get_user={"User": {"Id": 77, "ClassRegisterNumber": 12}},
    )
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    assert coordinator.student_number_from_librus == 12
    client.async_get_student_info_page.assert_not_called()


async def test_attachment_view_streams_the_file(hass, hass_client) -> None:
    """The file goes straight to the browser - nothing saved in HA."""
    client = build_mock_client()
    client.async_download_message_attachment.return_value = AttachmentFileData(
        filename="Plan lekcji ąę.pdf", content_type="application/pdf", content=b"%PDF-1.4"
    )
    entry = await setup_integration(hass, client)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    http = await hass_client()

    response = await http.get(f"/api/{DOMAIN}/attachment/{device.id}/99/55")

    assert response.status == 200
    assert await response.read() == b"%PDF-1.4"
    assert response.headers["Content-Type"].startswith("application/pdf")
    assert "filename*=UTF-8''Plan%20lekcji%20%C4%85%C4%99.pdf" in response.headers["Content-Disposition"]
    client.async_download_message_attachment.assert_awaited_once_with("55", "99")

    missing = await http.get(f"/api/{DOMAIN}/attachment/unknown-device/99/55")
    assert missing.status == 404


async def test_homework_attachment_view_retries_after_session_expiry(hass, hass_client) -> None:
    client = build_mock_client()
    client.async_download_homework_attachment.side_effect = [
        LibrusSessionExpiredError("gone", status_code=401),
        AttachmentFileData(filename="karta.pdf", content_type="application/pdf", content=b"%PDF"),
    ]
    entry = await setup_integration(hass, client)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    http = await hass_client()

    response = await http.get(f"/api/{DOMAIN}/homework_attachment/{device.id}/31")

    assert response.status == 200
    assert await response.read() == b"%PDF"
    assert 'filename="karta.pdf"' in response.headers["Content-Disposition"]
    assert client.async_download_homework_attachment.await_count == 2
    client.async_download_homework_attachment.assert_awaited_with("31")


async def test_school_file_view_downloads_through_home_assistant(hass, hass_client) -> None:
    """The document's Synergia link needs a logged-in Synergia session the
    browser doesn't have, so it goes through Home Assistant."""
    client = build_mock_client(async_get_school_files=FILES)
    client.async_download_school_file.return_value = AttachmentFileData(
        filename="Regulamin.pdf", content_type="application/pdf", content=b"%PDF"
    )
    entry = await setup_integration(hass, client)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    http = await hass_client()

    response = await http.get(f"/api/{DOMAIN}/school_file/{device.id}/17613")

    assert response.status == 200
    assert await response.read() == b"%PDF"
    client.async_download_school_file.assert_awaited_once_with("/pliki_szkoly/pobierz/1")

    unknown = await http.get(f"/api/{DOMAIN}/school_file/{device.id}/999")
    assert unknown.status == 502
