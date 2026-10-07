"""Text grades, lesson topics, school trips and documents, homework
categories, the student number from JSON and attachment download."""

from __future__ import annotations

import os
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


def _coordinator(hass, client) -> LibrusDataUpdateCoordinator:
    entry = make_config_entry()
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
    )
    entry = await setup_integration(hass, client)

    topics = _entity(hass, entry, "lesson_topics")
    assert topics.state == "1"
    assert topics.attributes["today"][0]["topic"] == "Ułamki zwykłe"
    assert topics.attributes["today"][0]["subject"] == "Matematyka"
    assert len(topics.attributes["recent"]) == 2

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


async def test_download_attachment_service_saves_to_media(hass, tmp_path) -> None:
    hass.config.media_dirs = {"local": str(tmp_path)}
    client = build_mock_client()
    client.async_download_message_attachment.return_value = AttachmentFileData(
        filename="Plan/lekcji.pdf", content_type="application/pdf", content=b"%PDF-1.4"
    )
    entry = await setup_integration(hass, client)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)

    response = await hass.services.async_call(
        DOMAIN,
        "download_attachment",
        {"device_id": device.id, "message_id": "99", "attachment_id": "55"},
        blocking=True,
        return_response=True,
    )

    assert response["filename"] == "lekcji.pdf"
    assert response["media_content_id"] == "media-source://media_source/local/librus_synergia/99/lekcji.pdf"
    with open(os.path.join(tmp_path, "librus_synergia", "99", "lekcji.pdf"), "rb") as handle:
        assert handle.read() == b"%PDF-1.4"
    client.async_download_message_attachment.assert_awaited_once_with("55", "99")
