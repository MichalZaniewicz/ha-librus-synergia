"""Tests for the Librus Synergia data update coordinator."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.librus_synergia.const import (
    EVENT_NEW_ABSENCE,
    EVENT_NEW_GRADE,
    EVENT_NEW_HOMEWORK,
    EVENT_TIMETABLE_CHANGED,
)
from custom_components.librus_synergia.coordinator import LibrusDataUpdateCoordinator
from custom_components.librus_synergia.librus_api import (
    LibrusConnectionError,
    LibrusInvalidCredentialsError,
    LibrusSessionExpiredError,
    LibrusUnexpectedResponseError,
)

from .conftest import build_mock_client, make_config_entry

GRADE_PAYLOAD = {
    "Grades": [
        {
            "Id": 1,
            "Grade": "5",
            "Category": {"Id": 10},
            "Subject": {"Id": 100},
            "Semester": 1,
            "AddDate": "2026-09-01",
        }
    ]
}


def _make_coordinator(hass, client, *, options: dict | None = None) -> LibrusDataUpdateCoordinator:
    entry = make_config_entry(options=options)
    entry.add_to_hass(hass)
    # async_config_entry_first_refresh() asserts the entry is mid-setup -
    # true when HA drives it via async_setup_entry, but this suite calls it
    # directly on a bare MockConfigEntry, so fake that state ourselves.
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


async def test_first_refresh_builds_data_and_seeds_silently(hass) -> None:
    """A brand-new entry's first refresh must populate data without firing
    any "new item" events - those events exist to flag things that appeared
    SINCE the last check, and there is no "last check" yet."""
    events = async_capture_events(hass, EVENT_NEW_GRADE)
    client = build_mock_client(async_get_grades=GRADE_PAYLOAD)
    coordinator = _make_coordinator(hass, client)

    await coordinator.async_config_entry_first_refresh()
    await hass.async_block_till_done()

    assert coordinator.data is not None
    assert len(coordinator.data.grades) == 1
    assert events == []


async def test_second_refresh_fires_new_grade_event(hass) -> None:
    events = async_capture_events(hass, EVENT_NEW_GRADE)
    client = build_mock_client()
    coordinator = _make_coordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    client.async_get_grades.return_value = GRADE_PAYLOAD
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["id"] == 1
    assert events[0].data["value"] == "5"


async def test_grade_seen_twice_is_not_re_announced(hass) -> None:
    """Union, not replace: a grade dropping out of a later fetch and then
    reappearing must not fire a second event for the same id."""
    events = async_capture_events(hass, EVENT_NEW_GRADE)
    client = build_mock_client()
    coordinator = _make_coordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    client.async_get_grades.return_value = GRADE_PAYLOAD
    await coordinator.async_refresh()
    client.async_get_grades.return_value = {"Grades": []}
    await coordinator.async_refresh()
    client.async_get_grades.return_value = GRADE_PAYLOAD
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 1


async def test_auth_error_raises_config_entry_auth_failed(hass) -> None:
    client = build_mock_client()
    client.async_ensure_session_valid.side_effect = LibrusInvalidCredentialsError("bad")
    coordinator = _make_coordinator(hass, client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_connection_error_raises_update_failed(hass) -> None:
    client = build_mock_client()
    client.async_ensure_session_valid.side_effect = LibrusConnectionError("net")
    coordinator = _make_coordinator(hass, client)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_session_expired_mid_cycle_recovers_via_forced_relogin(hass) -> None:
    """CONFIRMED live (2026-09-05): Librus's real session lifetime can run
    shorter than our own assumed-lifetime clock - a data endpoint rejects
    the session mid-cycle even though `async_ensure_session_valid` thought
    it was still fresh. The coordinator must force one re-login and retry
    silently, WITHOUT ever asking the user to reauth, as long as the
    forced re-login itself succeeds."""
    client = build_mock_client()
    client.async_get_grades.side_effect = [
        LibrusSessionExpiredError("session dead"),
        GRADE_PAYLOAD,
    ]
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert len(data.grades) == 1
    # First call is the normal not-yet-expired check; second is the forced
    # re-login triggered by the session-expired error.
    assert client.async_ensure_session_valid.call_count == 2
    _, kwargs = client.async_ensure_session_valid.call_args
    assert kwargs.get("force") is True


async def test_session_expired_and_relogin_also_fails_raises_auth_failed(hass) -> None:
    """If the forced re-login itself fails (genuinely wrong password,
    captcha, account action required), THAT must surface as a real reauth
    prompt - there's nothing more to retry automatically."""
    client = build_mock_client()
    client.async_get_grades.side_effect = LibrusSessionExpiredError("session dead")

    async def ensure_session_valid(password, *, force: bool = False):
        if force:
            raise LibrusInvalidCredentialsError("bad password")

    client.async_ensure_session_valid.side_effect = ensure_session_valid
    coordinator = _make_coordinator(hass, client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_messages_unavailable_school_is_non_fatal(hass) -> None:
    """Bootstrap returning False (module not enabled for this school) must
    not fail the whole update cycle - see _async_get_messages."""
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = False
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert data.messages_available is False
    assert data.unread_message_count == 0
    assert data.unread_messages_by_mailbox == {}
    assert data.messages == []
    client.async_get_unread_messages_count.assert_not_called()


async def test_messages_disabled_via_options_skips_all_message_calls(hass) -> None:
    """`messages_enabled: False` in the options must skip the Wiadomości
    bootstrap and every messages call entirely, leaving the sensor
    unavailable - same end state as a school without the module."""
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True  # would work if asked
    coordinator = _make_coordinator(hass, client, options={"messages_enabled": False})

    data = await coordinator._async_update_data()

    assert data.messages_available is False
    assert data.unread_message_count == 0
    client.async_bootstrap_messages.assert_not_called()
    client.async_get_unread_messages_count.assert_not_called()
    client.async_get_messages.assert_not_called()


async def test_messages_parsed_when_available(hass) -> None:
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {
        "data": {"inbox": 1, "notes": 2, "alerts": 0}
    }
    client.async_get_messages.return_value = {
        "data": [
            {
                "messageId": "42",
                "senderName": "Amelia Marciszak",
                "topic": "Zebranie",
                "content": "RHppZcWEIGRvYnJ5",
                "sendDate": "2026-09-04T17:47:10",
                "readDate": None,
                "isAnyFileAttached": False,
            }
        ]
    }
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert data.messages_available is True
    assert data.unread_message_count == 1
    assert data.unread_messages_by_mailbox["inbox"] == 1
    assert data.unread_messages_by_mailbox["notes"] == 2
    assert data.unread_messages_by_mailbox["alerts"] == 0
    assert data.unread_messages_by_mailbox["trash"] == 0
    assert len(data.messages) == 1
    assert data.messages[0].id == "42"
    assert data.messages[0].content == "Dzień dobry"
    assert data.messages[0].mailbox == "inbox"


async def test_secondary_mailbox_messages_parsed_with_mailbox_tag(hass) -> None:
    """New (2026-09-06, librusik-inspired): "substitutions" and "alerts"
    now get their own full message list (not just an unread count, unlike
    every other secondary mailbox), each message tagged with which
    mailbox it came from so a card can pass the right value to the
    `get_message` service. "justifications" (usprawiedliwienia) joined
    them the same day, on user request, via the identical mechanism."""
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 0}}
    # asyncio.gather(inbox, substitutions, alerts, justifications) -
    # side_effect consumed in call order, which matches that argument order.
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
        {
            "data": [
                {
                    "messageId": "3",
                    "senderName": "Wychowawca",
                    "topic": "Usprawiedliwienie",
                    "content": "RHppZWQgZG9icnk=",
                    "sendDate": "2026-09-06T09:00:00",
                    "readDate": None,
                    "isAnyFileAttached": False,
                }
            ]
        },
    ]
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert data.messages == []
    assert len(data.substitution_messages) == 1
    assert data.substitution_messages[0].mailbox == "substitutions"
    assert data.substitution_messages[0].topic == "Zmiana w planie"
    assert len(data.alert_messages) == 1
    assert data.alert_messages[0].mailbox == "alerts"
    assert data.alert_messages[0].topic == "Alert"
    assert len(data.justification_messages) == 1
    assert data.justification_messages[0].mailbox == "justifications"
    assert data.justification_messages[0].topic == "Usprawiedliwienie"


async def test_secondary_mailbox_failure_does_not_wipe_inbox_data(hass) -> None:
    """BUG FIX (2026-09-06, found live): substitutions/alerts used to be
    fetched in the SAME asyncio.gather() as the core inbox/unread-count
    calls - asyncio.gather() fails as a whole the moment any ONE of its
    awaitables raises, so a real live failure fetching these two bonus
    mailboxes (a genuine shape mismatch - see test_api_client.py's
    test_messages_list_bare_array_response_is_normalized) silently wiped
    out the otherwise-working inbox data too: mailbox_breakdown went from
    real per-mailbox counts to an empty {} on the user's actual account.
    The two fetches must be isolated so one failing can only ever degrade
    to "no substitutions/alerts shown", never take inbox down with it."""
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 1}}
    client.async_get_messages.side_effect = [
        {
            "data": [
                {
                    "messageId": "42",
                    "senderName": "Amelia Marciszak",
                    "topic": "Zebranie",
                    "content": "",
                    "sendDate": None,
                    "readDate": None,
                    "isAnyFileAttached": False,
                }
            ]
        },
        LibrusUnexpectedResponseError("Expected a JSON object, got list"),
        LibrusUnexpectedResponseError("Expected a JSON object, got list"),
        LibrusUnexpectedResponseError("Expected a JSON object, got list"),
    ]
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert data.unread_message_count == 1
    assert data.unread_messages_by_mailbox["inbox"] == 1
    assert len(data.messages) == 1
    assert data.messages[0].id == "42"
    assert data.substitution_messages == []
    assert data.alert_messages == []
    assert data.justification_messages == []


async def test_optional_endpoint_failure_does_not_wipe_core_data(hass) -> None:
    """BUG FIX (2026-09-06, code review): _async_fetch_core_payloads used to
    put all 16 core+supplementary endpoints in ONE asyncio.gather() -
    exactly the same all-or-nothing failure class already fixed once for
    messages (see test_secondary_mailbox_failure_does_not_wipe_inbox_data).
    A single newer/less-exercised endpoint (DescriptiveGrades here) raising
    must degrade only ITS OWN entity to empty, never wipe out
    grades/attendance/timetable/notices, which were fetched successfully."""
    client = build_mock_client(async_get_grades=GRADE_PAYLOAD)
    client.async_get_descriptive_grades.side_effect = LibrusUnexpectedResponseError(
        "HTTP 500 from DescriptiveGrades"
    )
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert len(data.grades) == 1
    assert data.descriptive_grades == []


def test_decode_message_content_strips_xml_cdata_wrapper() -> None:
    """BUG FIX (2026-09-06, found live - "co to za bug z treścią
    wiadomości?"): the single-message endpoint's `Message` field, once
    base64-decoded, isn't plain text - it's a tiny XML wrapper
    (`<Message><Content><![CDATA[...]]></Content></Message>`). A card's
    expanded message view showed the literal
    "<Message><Content><![CDATA[" prefix leaking into the display."""
    import base64

    from custom_components.librus_synergia.coordinator import decode_message_content

    xml_wrapped = (
        "<Message><Content><![CDATA[Szanowni Państwo! Drodzy Uczniowie!"
        "\n\nPrzypominam wszystkim.]]></Content></Message>"
    )
    encoded = base64.b64encode(xml_wrapped.encode("utf-8")).decode("ascii")

    result = decode_message_content(encoded)

    assert result == "Szanowni Państwo! Drodzy Uczniowie!\n\nPrzypominam wszystkim."
    assert "<Message>" not in result
    assert "CDATA" not in result


def test_decode_message_content_handles_missing_cdata_close_tag() -> None:
    """Defensive fallback if the CDATA-wrapped text is itself truncated
    (mirrors the plain-text truncation the list endpoint is confirmed to
    do) - everything after the opening marker, not raw XML markup."""
    import base64

    from custom_components.librus_synergia.coordinator import decode_message_content

    truncated = "<Message><Content><![CDATA[Szanowni Państwo! cut off mid"
    encoded = base64.b64encode(truncated.encode("utf-8")).decode("ascii")

    result = decode_message_content(encoded)

    assert result == "Szanowni Państwo! cut off mid"
    assert "<Message>" not in result


def test_decode_message_content_leaves_plain_text_untouched() -> None:
    """The list endpoint's `content` field is confirmed plain text (no XML
    wrapper) - the CDATA-stripping regex must be a no-op there."""
    import base64

    from custom_components.librus_synergia.coordinator import decode_message_content

    plain = "Szanowni Państwo, zapraszam na zebranie."
    encoded = base64.b64encode(plain.encode("utf-8")).decode("ascii")

    assert decode_message_content(encoded) == plain


def test_decode_message_content_flattens_librus_link_converter_html() -> None:
    """BUG FIX (2026-09-10, found live): Librus rewrites every link in a
    message into an <a href="https://liblink.pl/..." title="Link został
    skonwertowany...">...</a> tag. Rendered as plain text in the Wiadomości
    card it read as raw tag soup - flatten it to just the URL, and turn
    <br> into newlines."""
    import base64

    from custom_components.librus_synergia.coordinator import decode_message_content

    body = (
        "Proszę przynieść flet.<br>"
        'Kup tu: <a href="https://liblink.pl/VAQE883If2" target="_blank" '
        'title="Link został skonwertowany ze względów bezpieczeństwa systemu.">'
        "https://liblink.pl/VAQE883If2</a><br>"
        'sklep &amp; serwis: <a href="https://liblink.pl/lJGRzdksvR">tutaj</a>'
    )
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")

    result = decode_message_content(encoded)

    assert "<a " not in result and "href=" not in result
    assert "https://liblink.pl/VAQE883If2" in result
    assert "tutaj (https://liblink.pl/lJGRzdksvR)" in result
    assert "&amp;" not in result and "sklep & serwis" in result
    assert result.startswith("Proszę przynieść flet.\n")


async def test_message_content_truncated_mid_char_decodes_readable_prefix(hass) -> None:
    """CONFIRMED live (2026-09-06): Librus truncates the list endpoint's
    base64 `content` field to a fixed byte length, which can land mid a
    multi-byte UTF-8 character (a Polish "a" here). Previously this raised
    UnicodeDecodeError and fell all the way back to the raw, still-encoded
    base64 string - rendered as an unbroken hash-like blob in the
    Wiadomości card, causing horizontal scroll. Must decode the readable
    prefix instead of the raw base64."""
    # base64.b64decode("U3phbm93bmkgUGHFhHN0d28sCgp6YXByYXN6YW0gY2jEmXRu"
    # eyChIHVjem5pw7N3IHoga2xhcyBzacOzZG15Y2ggZG8gdWR6aWHFgnUgdyB6YWrEmWNp"
    # YWNoIHJvendpamFqxA==") truncates "rozwijając" mid-"ą".
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 0}}
    client.async_get_messages.return_value = {
        "data": [
            {
                "messageId": "99",
                "senderName": "Hanke Kamila",
                "topic": "Zajecia",
                "content": (
                    "U3phbm93bmkgUGHFhHN0d28sCgp6YXByYXN6YW0gY2jEmXRueWNoIHVj"
                    "em5pw7N3IHoga2xhcyBzacOzZG15Y2ggZG8gdWR6aWHFgnUgdyB6YWrE"
                    "mWNpYWNoIHJvendpamFqxA=="
                ),
                "sendDate": "2026-09-04T15:06:46",
                "readDate": "2026-09-05T10:00:00",
                "isAnyFileAttached": False,
            }
        ]
    }
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert len(data.messages) == 1
    content = data.messages[0].content
    # Readable prefix decoded, no raw base64 leaked through, no U+FFFD/mojibake.
    assert content.startswith("Szanowni Państwo,\n\nzapraszam chętnych uczniów")
    assert "U3phbm93" not in content
    assert "�" not in content


async def test_fetch_message_recovers_from_mid_cycle_session_expiry(hass) -> None:
    """`async_fetch_message` (services.py's `get_message` on-demand path,
    deliberately outside the coordinator's routine polling) gets the same
    forced-relogin-and-retry-once recovery as `_async_update_data` and
    `async_fetch_timetable_week` for a session that died since the last
    successful poll."""
    good_payload = {
        "data": {
            "senderName": "Marciszak Amelia",
            "topic": "Zebranie z rodzicami",
            "Message": "RHppZWQgZG9icnk=",  # "Dzied dobry" (base64)
            "sendDate": "2026-09-04T17:47:10",
            "readDate": "2026-09-06T18:22:51",
        }
    }
    client = build_mock_client()
    client.async_get_message.side_effect = [
        LibrusSessionExpiredError("session dead"),
        good_payload,
    ]
    coordinator = _make_coordinator(hass, client)

    result = await coordinator.async_fetch_message("inbox", "186536")

    assert result == good_payload
    force_calls = [
        c for c in client.async_ensure_session_valid.call_args_list if c.kwargs.get("force")
    ]
    assert len(force_calls) == 1


async def test_fetch_message_raises_when_recovery_also_fails(hass) -> None:
    """Unlike the timetable's on-demand fetch (which degrades to "no
    lessons known"), a failed message fetch has no sensible empty fallback
    - it must propagate so the `get_message` service can surface a real
    error to whoever clicked the message."""
    client = build_mock_client()
    client.async_get_message.side_effect = [
        LibrusSessionExpiredError("session dead"),
        LibrusInvalidCredentialsError("bad password"),
    ]
    coordinator = _make_coordinator(hass, client)

    with pytest.raises(LibrusInvalidCredentialsError):
        await coordinator.async_fetch_message("inbox", "186536")


async def test_notes_positive_resolves_to_sentiment_label(hass) -> None:
    """CONFIRMED (2026-09-06) via szkolny-android's reference parser:
    0=negative, 1=positive, else(2)=neutral."""
    client = build_mock_client(
        async_get_notes={
            "Notes": [
                {"Id": 1, "Text": "a", "Positive": 0, "Date": "2026-09-01"},
                {"Id": 2, "Text": "b", "Positive": 1, "Date": "2026-09-02"},
                {"Id": 3, "Text": "c", "Positive": 2, "Date": "2026-09-03"},
            ]
        }
    )
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    by_id = {n.id: n for n in data.notes}
    assert by_id[1].sentiment == "negative"
    assert by_id[2].sentiment == "positive"
    assert by_id[3].sentiment == "neutral"


async def test_grade_comments_resolved_from_separate_endpoint(hass) -> None:
    """CONFIRMED (2026-09-06): a grade's `Comments` field is a list of ids
    into the separate Grades/Comments endpoint, not embedded objects."""
    client = build_mock_client(
        async_get_grades={
            "Grades": [
                {
                    "Id": 1,
                    "Grade": "5",
                    "Category": {"Id": 10},
                    "Subject": {"Id": 100},
                    "Comments": [501, {"Id": 502}],
                }
            ]
        },
        async_get_grade_comments={
            "Comments": [
                {"Id": 501, "Text": "Świetna praca"},
                {"Id": 502, "Text": "Popraw pismo"},
            ]
        },
    )
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert data.grades[0].comments == ["Świetna praca", "Popraw pismo"]


async def test_homework_assignments_parsed(hass) -> None:
    client = build_mock_client(
        async_get_homework_assignments={
            "HomeWorkAssignments": [
                {
                    "Id": 1,
                    "Topic": "Zadanie 5",
                    "Text": "Strona 42, zadania 1-5",
                    "Teacher": {"Id": 200},
                    "Date": "2026-09-01",
                    "DueDate": "2026-09-08",
                }
            ]
        },
        async_get_teachers={"Users": [{"Id": 200, "FirstName": "Jan", "LastName": "Kowalski"}]},
    )
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert len(data.homework_assignments) == 1
    assignment = data.homework_assignments[0]
    assert assignment.topic == "Zadanie 5"
    assert assignment.due_date == "2026-09-08"
    assert assignment.teacher_id == 200


async def test_behaviour_grades_parsed_with_resolved_category_and_comments(hass) -> None:
    client = build_mock_client(
        async_get_behaviour_grade_points={
            "Grades": [
                {
                    "Id": 1,
                    "Value": 5.0,
                    "ShortName": "wz",
                    "Semester": 1,
                    "Category": {"Id": 21823},
                    "AddedBy": {"Id": 300},
                    "AddDate": "2026-09-05",
                    "Text": "Wzorowe zachowanie",
                    "Comments": [601],
                }
            ]
        },
        async_get_behaviour_grade_point_categories={
            "Categories": [{"Id": 21823, "Name": "zachowanie"}]
        },
        async_get_behaviour_grade_point_comments={
            "Comments": [{"Id": 601, "Text": "Brawo"}]
        },
    )
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert len(data.behaviour_grades) == 1
    grade = data.behaviour_grades[0]
    assert grade.short_name == "wz"
    assert grade.comments == ["Brawo"]
    assert data.behaviour_grade_categories[21823] == "zachowanie"


async def test_descriptive_grades_parsed(hass) -> None:
    client = build_mock_client(
        async_get_descriptive_grades={
            "Grades": [
                {
                    "Id": 1,
                    "Subject": {"Id": 100},
                    "Grade": "Opanował materiał w stopniu bardzo dobrym",
                    "Skill": {"Id": 55},
                    "AddDate": "2026-09-05",
                }
            ]
        },
        async_get_subjects={"Subjects": [{"Id": 100, "Name": "Matematyka"}]},
    )
    coordinator = _make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert len(data.descriptive_grades) == 1
    assert data.descriptive_grades[0].subject_id == 100
    assert data.descriptive_grades[0].skill_id == 55


def _timetable_with_disruption(day_iso: str, *, canceled: bool = False, substitution: bool = False) -> dict:
    return {
        "Timetable": {
            day_iso: [
                [
                    {
                        "LessonNo": "3",
                        "HourFrom": "10:00",
                        "HourTo": "10:45",
                        "Subject": {"Id": "300"},
                        "IsCanceled": canceled,
                        "IsSubstitutionClass": substitution,
                    }
                ]
            ]
        }
    }


async def test_timetable_change_event_seeds_silently_then_fires(hass, freezer) -> None:
    """First sync only establishes the baseline (no event); a lesson newly
    turning up cancelled on the next sync fires EVENT_TIMETABLE_CHANGED
    once, with the resolved subject and enough detail for a notification."""
    freezer.move_to("2026-09-09T12:00:00+00:00")
    day_iso = dt_util.now().date().isoformat()
    events = async_capture_events(hass, EVENT_TIMETABLE_CHANGED)
    client = build_mock_client(
        async_get_timetable=_timetable_with_disruption(day_iso),
        async_get_subjects={"Subjects": [{"Id": 300, "Name": "Historia"}]},
    )
    coordinator = _make_coordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()
    await hass.async_block_till_done()
    assert events == []

    client.async_get_timetable.return_value = _timetable_with_disruption(day_iso, canceled=True)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["kind"] == "canceled"
    assert events[0].data["lesson_no"] == 3
    assert events[0].data["date"] == day_iso
    assert events[0].data["subject"] == "Historia"


async def test_new_absence_event_fires_with_excused_flag(hass) -> None:
    events = async_capture_events(hass, EVENT_NEW_ABSENCE)
    client = build_mock_client(
        async_get_attendance_types={
            "Types": [
                {"Id": 100, "Name": "Obecność", "IsPresenceKind": True},
                {"Id": 1, "Name": "Nieobecność", "IsPresenceKind": False},
            ]
        },
    )
    coordinator = _make_coordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()  # seeds silently

    client.async_get_attendances.return_value = {
        "Attendances": [
            {"Id": 501, "Date": "2026-09-11", "LessonNo": 3, "Type": {"Id": 1}},
            {"Id": 502, "Date": "2026-09-11", "Type": {"Id": 100}},  # present - no event
        ]
    }
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["id"] == 501
    assert events[0].data["excused"] is False
    assert events[0].data["date"] == "2026-09-11"
    assert events[0].data["lesson_no"] == 3


async def test_new_homework_event_carries_resolved_subject_and_category(hass) -> None:
    events = async_capture_events(hass, EVENT_NEW_HOMEWORK)
    client = build_mock_client(
        async_get_subjects={"Subjects": [{"Id": 100, "Name": "Matematyka"}]},
        async_get_homework_categories={"Categories": [{"Id": 1, "Name": "Sprawdzian"}]},
    )
    coordinator = _make_coordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    client.async_get_homeworks.return_value = {
        "HomeWorks": [
            {
                "Id": 77,
                "Content": "Dział 3",
                "Date": "2026-09-20",
                "Category": {"Id": 1},
                "Subject": {"Id": 100},
            }
        ]
    }
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["id"] == 77
    assert events[0].data["subject"] == "Matematyka"
    assert events[0].data["category"] == "Sprawdzian"
    assert events[0].data["date"] == "2026-09-20"


async def test_timetable_change_event_not_re_fired_for_known_disruption(hass, freezer) -> None:
    freezer.move_to("2026-09-09T12:00:00+00:00")
    day_iso = dt_util.now().date().isoformat()
    events = async_capture_events(hass, EVENT_TIMETABLE_CHANGED)
    client = build_mock_client(
        async_get_timetable=_timetable_with_disruption(day_iso, substitution=True),
    )
    coordinator = _make_coordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert events == []  # substitution present from the very first (seeding) sync
