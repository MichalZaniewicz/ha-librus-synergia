"""Tests for the Librus Synergia integration's HA services (`get_message`)."""

from __future__ import annotations

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from custom_components.librus_synergia.const import DOMAIN
from librus_synergia import LibrusInvalidCredentialsError

from .conftest import build_mock_client, setup_integration

GOOD_MESSAGE_PAYLOAD = {
    "data": {
        "senderName": "Nowak Anna",
        "topic": "Zebranie z rodzicami",
        # base64 for "Dzien dobry, zapraszam na zebranie."
        "Message": "RHppZW4gZG9icnksIHphcHJhc3phbSBuYSB6ZWJyYW5pZS4=",
        "sendDate": "2026-09-04T17:47:10",
        "readDate": "2026-09-06T18:22:51",
        "attachments": [],
    }
}

GOOD_GRADES_PAYLOAD = {
    "Grades": [
        {
            "Id": 1,
            "Grade": "4",
            "Category": {"Id": 10},
            "Subject": {"Id": 100},
            "Semester": 1,
            "AddDate": "2026-09-16 10:58:09",
            "Comments": [],
        },
        {
            "Id": 2,
            "Grade": "6",
            "Category": {"Id": 11},
            "Subject": {"Id": 200},
            "Semester": 1,
            "AddDate": "2026-09-15 14:02:06",
            "Comments": [],
        },
    ]
}
GRADES_SUBJECTS_PAYLOAD = {
    "Subjects": [{"Id": 100, "Name": "Biologia"}, {"Id": 200, "Name": "Geografia"}]
}
GRADES_CATEGORIES_PAYLOAD = {
    "Categories": [
        {"Id": 10, "Name": "praca na lekcji", "CountToTheAverage": True, "Weight": 3},
        {"Id": 11, "Name": "odpowiedź ustna", "CountToTheAverage": True, "Weight": 3},
    ]
}


def _device_id(hass, entry) -> str:
    # async_get_device(identifiers=...) is deprecated (identifiers are no
    # longer guaranteed unique across config entries) - the modern,
    # unambiguous lookup is by identifier scoped to the owning entry.
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert device is not None
    return device.id


async def test_get_message_service_returns_decoded_content(hass) -> None:
    client = build_mock_client()
    client.async_get_message.return_value = GOOD_MESSAGE_PAYLOAD
    entry = await setup_integration(hass, client)

    response = await hass.services.async_call(
        DOMAIN,
        "get_message",
        {"device_id": _device_id(hass, entry), "message_id": "100001"},
        blocking=True,
        return_response=True,
    )

    assert response["sender"] == "Nowak Anna"
    assert response["topic"] == "Zebranie z rodzicami"
    assert response["content"] == "Dzien dobry, zapraszam na zebranie."
    assert response["read_date"] == "2026-09-06T18:22:51"
    assert response["has_attachment"] is False
    assert response["attachments"] == []
    client.async_get_message.assert_awaited_once_with("inbox", "100001")


async def test_get_message_service_returns_attachment_filenames(hass) -> None:
    """CONFIRMED live (2026-09-17): each entry is {"filename", "id"} - the
    file itself still can't be downloaded, but the name is real and useful."""
    client = build_mock_client()
    client.async_get_message.return_value = {
        "data": {
            **GOOD_MESSAGE_PAYLOAD["data"],
            "attachments": [
                {"filename": "informacja-dla-rodzicow.pdf", "id": "500001"}
            ],
        }
    }
    entry = await setup_integration(hass, client)

    response = await hass.services.async_call(
        DOMAIN,
        "get_message",
        {"device_id": _device_id(hass, entry), "message_id": "100002"},
        blocking=True,
        return_response=True,
    )

    assert response["has_attachment"] is True
    assert response["attachments"] == [
        {"id": "500001", "filename": "informacja-dla-rodzicow.pdf"}
    ]


async def test_get_message_service_falls_back_to_first_and_last_name(hass) -> None:
    """`resolve_sender_name` (code review - shared with coordinator.py's
    `_parse_message_list`, see test_coordinator.py's own coverage of the
    same helper) must fall back to senderFirstName+senderLastName combined
    when `senderName` itself is absent from the single full-message
    endpoint's response, not just from a mailbox list response."""
    client = build_mock_client()
    client.async_get_message.return_value = {
        "data": {
            "senderFirstName": "Anna",
            "senderLastName": "Nowak",
            "topic": "Zebranie z rodzicami",
            "Message": GOOD_MESSAGE_PAYLOAD["data"]["Message"],
            "sendDate": "2026-09-04T17:47:10",
            "readDate": None,
            "attachments": [],
        }
    }
    entry = await setup_integration(hass, client)

    response = await hass.services.async_call(
        DOMAIN,
        "get_message",
        {"device_id": _device_id(hass, entry), "message_id": "100001"},
        blocking=True,
        return_response=True,
    )

    assert response["sender"] == "Anna Nowak"


async def test_get_message_service_mailbox_field_passed_through(hass) -> None:
    """New (2026-09-06): the `mailbox` field lets a card fetch full
    content for a substitutions/alerts message, not just inbox."""
    client = build_mock_client()
    client.async_get_message.return_value = GOOD_MESSAGE_PAYLOAD
    entry = await setup_integration(hass, client)

    response = await hass.services.async_call(
        DOMAIN,
        "get_message",
        {"device_id": _device_id(hass, entry), "message_id": "1", "mailbox": "substitutions"},
        blocking=True,
        return_response=True,
    )

    assert response["mailbox"] == "substitutions"
    client.async_get_message.assert_awaited_once_with("substitutions", "1")


async def test_get_message_service_unknown_device_raises(hass) -> None:
    client = build_mock_client()
    await setup_integration(hass, client)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "get_message",
            {"device_id": "not-a-real-device", "message_id": "100001"},
            blocking=True,
            return_response=True,
        )


async def test_get_message_service_client_error_surfaces_as_hass_error(hass) -> None:
    """A genuinely failed fetch (e.g. forced re-login also rejected) must
    surface as a real error to whoever clicked the message, not silently
    swallow it or crash uncaught."""
    client = build_mock_client()
    client.async_get_message.side_effect = LibrusInvalidCredentialsError("bad password")
    entry = await setup_integration(hass, client)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "get_message",
            {"device_id": _device_id(hass, entry), "message_id": "100001"},
            blocking=True,
            return_response=True,
        )


async def test_get_message_service_unregistered_after_last_entry_unloaded(hass) -> None:
    client = build_mock_client()
    entry = await setup_integration(hass, client)
    assert hass.services.has_service(DOMAIN, "get_message")

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, "get_message")


async def test_refresh_service_triggers_a_coordinator_update(hass) -> None:
    client = build_mock_client()
    entry = await setup_integration(hass, client)
    assert hass.services.has_service(DOMAIN, "refresh")
    before = client.async_get_grades.await_count

    await hass.services.async_call(
        DOMAIN, "refresh", {"device_id": _device_id(hass, entry)}, blocking=True
    )
    await hass.async_block_till_done()

    assert client.async_get_grades.await_count > before


async def test_refresh_service_without_device_refreshes_all(hass) -> None:
    client = build_mock_client()
    await setup_integration(hass, client)
    before = client.async_get_grades.await_count

    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    await hass.async_block_till_done()

    assert client.async_get_grades.await_count > before


async def test_get_grades_service_returns_every_subject_sorted_newest_first(hass) -> None:
    client = build_mock_client(
        async_get_grades=GOOD_GRADES_PAYLOAD,
        async_get_subjects=GRADES_SUBJECTS_PAYLOAD,
        async_get_grade_categories=GRADES_CATEGORIES_PAYLOAD,
    )
    entry = await setup_integration(hass, client)

    response = await hass.services.async_call(
        DOMAIN,
        "get_grades",
        {"device_id": _device_id(hass, entry)},
        blocking=True,
        return_response=True,
    )

    assert response["count"] == 2
    assert [g["subject"] for g in response["grades"]] == ["Biologia", "Geografia"]
    assert response["grades"][0]["value"] == "4"
    assert response["grades"][0]["category"] == "praca na lekcji"


async def test_get_grades_service_includes_descriptive_grades(hass) -> None:
    client = build_mock_client(
        async_get_descriptive_grades={
            "Grades": [
                {
                    "Id": 1,
                    "Subject": {"Id": 100},
                    "Skill": {"Id": 55},
                    "AddedBy": {"Id": 7},
                    "Grade": 3,
                    "Map": "6",
                    "Date": "2026-09-30",
                    "Semester": 1,
                    "Comments": [{"Id": 44}],
                }
            ]
        },
        async_get_descriptive_grade_skills={"Skills": [{"Id": 55, "Name": "Rytmika"}]},
        async_get_descriptive_grade_comments={"Comments": [{"Id": 44, "Text": "Brawo"}]},
        async_get_subjects={"Subjects": [{"Id": 100, "Name": "Edukacja muzyczna"}]},
        async_get_teachers={"Users": [{"Id": 7, "FirstName": "Anna", "LastName": "Nowak"}]},
    )
    entry = await setup_integration(hass, client)

    response = await hass.services.async_call(
        DOMAIN,
        "get_grades",
        {"device_id": _device_id(hass, entry)},
        blocking=True,
        return_response=True,
    )

    assert response["descriptive_grades"] == [
        {
            "subject": "Edukacja muzyczna",
            "subject_id": 100,
            "value": "6",
            "skill": "Rytmika",
            "comments": ["Brawo"],
            "requirements": [],
            "date": "2026-09-30",
            "semester": 1,
            "teacher": "Anna Nowak",
        }
    ]


async def test_get_grades_service_filters_by_subject_id(hass) -> None:
    client = build_mock_client(
        async_get_grades=GOOD_GRADES_PAYLOAD,
        async_get_subjects=GRADES_SUBJECTS_PAYLOAD,
        async_get_grade_categories=GRADES_CATEGORIES_PAYLOAD,
    )
    entry = await setup_integration(hass, client)

    response = await hass.services.async_call(
        DOMAIN,
        "get_grades",
        {"device_id": _device_id(hass, entry), "subject_id": 200},
        blocking=True,
        return_response=True,
    )

    assert response["count"] == 1
    assert response["grades"][0]["subject"] == "Geografia"


async def test_get_grades_service_unknown_device_raises(hass) -> None:
    client = build_mock_client()
    await setup_integration(hass, client)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "get_grades",
            {"device_id": "not-a-real-device"},
            blocking=True,
            return_response=True,
        )


_NOTICES = {
    "SchoolNotices": [
        {
            "Id": "LID-NBOARD-NOTICE-1",
            "Subject": "Wycieczka",
            "Content": "Zbiórka o 8:00.",
            "StartDate": "2026-10-01",
            "EndDate": "2026-10-20",
            "CreationDate": "2026-10-01 10:00:00",
            "WasRead": False,
        },
        {
            "Id": "LID-NBOARD-NOTICE-2",
            "Subject": "Zebranie",
            "Content": "We wtorek.",
            "StartDate": "2026-10-02",
            "EndDate": "2026-10-20",
            "CreationDate": "2026-10-02 10:00:00",
            "WasRead": False,
        },
    ]
}


def _notices_entity(hass, entry) -> str:
    from homeassistant.helpers import entity_registry as er

    return next(
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id.endswith("_unread_announcements")
    )


async def test_mark_announcement_read_marks_one_and_shows_it_read(hass) -> None:
    client = build_mock_client(async_get_school_notices=_NOTICES)
    entry = await setup_integration(hass, client)
    entity_id = _notices_entity(hass, entry)
    assert hass.states.get(entity_id).state == "2"

    response = await hass.services.async_call(
        DOMAIN,
        "mark_announcement_read",
        {"device_id": _device_id(hass, entry), "announcement_id": "LID-NBOARD-NOTICE-1"},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()

    assert response == {"marked": ["LID-NBOARD-NOTICE-1"]}
    client.async_mark_school_notice_read.assert_awaited_once_with("LID-NBOARD-NOTICE-1")
    state = hass.states.get(entity_id)
    assert state.state == "1"
    read = {n["id"]: n["read"] for n in state.attributes["notices"]}
    assert read == {"LID-NBOARD-NOTICE-1": True, "LID-NBOARD-NOTICE-2": False}


async def test_mark_announcement_read_without_id_marks_every_unread(hass) -> None:
    client = build_mock_client(async_get_school_notices=_NOTICES)
    entry = await setup_integration(hass, client)

    response = await hass.services.async_call(
        DOMAIN,
        "mark_announcement_read",
        {"device_id": _device_id(hass, entry)},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()

    assert sorted(response["marked"]) == ["LID-NBOARD-NOTICE-1", "LID-NBOARD-NOTICE-2"]
    assert client.async_mark_school_notice_read.await_count == 2
    assert hass.states.get(_notices_entity(hass, entry)).state == "0"


async def test_mark_announcement_read_refused_raises(hass) -> None:
    from librus_synergia import LibrusUnexpectedResponseError

    client = build_mock_client(async_get_school_notices=_NOTICES)
    client.async_mark_school_notice_read.side_effect = LibrusUnexpectedResponseError(
        "HTTP 400", status_code=400
    )
    entry = await setup_integration(hass, client)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "mark_announcement_read",
            {"device_id": _device_id(hass, entry), "announcement_id": "LID-NBOARD-NOTICE-1"},
            blocking=True,
            return_response=True,
        )
    # Nothing shows read when Librus refused.
    assert hass.states.get(_notices_entity(hass, entry)).state == "2"


def _inbox(*items: tuple[str, str | None]) -> dict:
    return {
        "data": [
            {
                "messageId": message_id,
                "senderName": "Nowak Anna",
                "topic": f"Temat {message_id}",
                "content": "RHppZWQgZG9icnk=",
                "sendDate": "2026-10-14T08:00:00",
                "readDate": read_date,
                "isAnyFileAttached": False,
            }
            for message_id, read_date in items
        ]
    }


def _messages_entity(hass, entry) -> str:
    from homeassistant.helpers import entity_registry as er

    return next(
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id.endswith("_unread_messages")
    )


def _client_with_inbox() -> object:
    from .conftest import messages_by_mailbox

    client = build_mock_client(
        async_bootstrap_messages=True,
        async_get_unread_messages_count={"data": {"inbox": 2}},
    )
    client.async_get_messages.side_effect = messages_by_mailbox(
        {"inbox": _inbox(("100001", None), ("100002", None), ("100003", "2026-10-13T10:00:00"))}
    )
    return client


async def test_mark_message_read_without_id_opens_every_unread(hass) -> None:
    client = _client_with_inbox()
    entry = await setup_integration(hass, client)
    entity_id = _messages_entity(hass, entry)
    assert hass.states.get(entity_id).state == "2"

    response = await hass.services.async_call(
        DOMAIN,
        "mark_message_read",
        {"device_id": _device_id(hass, entry)},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()

    assert response == {"marked": ["100001", "100002"]}
    # Opening a message is what marks it read in Wiadomości.
    assert [c.args for c in client.async_get_message.await_args_list] == [
        ("inbox", "100001"),
        ("inbox", "100002"),
    ]
    state = hass.states.get(entity_id)
    assert state.state == "0"
    assert state.attributes["mailbox_breakdown"]["inbox"] == 0
    assert [m["unread"] for m in state.attributes["recent"]] == [False, False, False]


async def test_mark_message_read_one_message(hass) -> None:
    client = _client_with_inbox()
    entry = await setup_integration(hass, client)

    response = await hass.services.async_call(
        DOMAIN,
        "mark_message_read",
        {"device_id": _device_id(hass, entry), "message_id": "100002"},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()

    assert response == {"marked": ["100002"]}
    state = hass.states.get(_messages_entity(hass, entry))
    assert state.state == "1"
    unread = {m["id"]: m["unread"] for m in state.attributes["recent"]}
    assert unread == {"100001": True, "100002": False, "100003": False}


async def test_mark_message_read_refused_raises(hass) -> None:
    from librus_synergia import LibrusUnexpectedResponseError

    client = _client_with_inbox()
    client.async_get_message.side_effect = LibrusUnexpectedResponseError(
        "HTTP 500", status_code=500
    )
    entry = await setup_integration(hass, client)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "mark_message_read",
            {"device_id": _device_id(hass, entry), "message_id": "100001"},
            blocking=True,
            return_response=True,
        )
    # Nothing shows read when Librus refused.
    assert hass.states.get(_messages_entity(hass, entry)).state == "2"
