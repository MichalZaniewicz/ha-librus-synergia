"""Forced relogins, the message cache after opening a message, the grade
scale's repair issue, legacy badges and the last write on unload."""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.librus_synergia.const import (
    DOMAIN,
    EVENT_ACHIEVEMENT_UNLOCKED,
    ISSUE_OPTIONAL_ENDPOINT_DEGRADED,
    STATE_STORE_VERSION,
)
from custom_components.librus_synergia.coordinator import (
    LibrusDataUpdateCoordinator,
    optional_endpoint_issue_id,
    state_store_key,
)
from librus_synergia import (
    LibrusConnectionError,
    LibrusSessionExpiredError,
    LibrusUnexpectedResponseError,
)

from .conftest import build_mock_client, make_config_entry, messages_by_mailbox, setup_integration

# 19:00 UTC is 12:00 in the test time zone (US/Pacific).
NOON = "2026-10-07T19:00:00+00:00"
GRADES = {"Grades": [{"Id": 1, "Grade": "5", "Subject": {"Id": 100}, "AddDate": "2026-09-01"}]}


def _coordinator(hass, client, entry=None) -> LibrusDataUpdateCoordinator:
    entry = entry or make_config_entry()
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


def _store(hass_storage, entry_id: str, data: dict) -> None:
    hass_storage[state_store_key(entry_id)] = {
        "version": STATE_STORE_VERSION,
        "minor_version": 1,
        "key": state_store_key(entry_id),
        "data": data,
    }


def _forced_logins(client) -> int:
    return sum(1 for c in client.async_ensure_session_valid.call_args_list if c.kwargs.get("force"))


# ----------------------------------------------------------------------
# Forced relogin
# ----------------------------------------------------------------------


async def test_401_right_after_a_token_refresh_still_logs_in_again(hass) -> None:
    """The cycle's own check has just refreshed the token, and Librus still
    rejects the next request: that is the new session failing, not an old
    cookie - a fresh login is needed. (A "session younger than 30 s" window
    used to skip it, and the retry failed on the same rejected session.)"""
    client = build_mock_client()
    client.session_age_seconds = 7200.0

    async def ensure(*_args, **_kwargs) -> None:
        client.session_age_seconds = 1.0  # refreshed just now

    client.async_ensure_session_valid.side_effect = ensure
    client.async_get_grades.side_effect = [
        LibrusSessionExpiredError("rejected", status_code=401),
        GRADES,
    ]
    coordinator = _coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert len(data.grades) == 1
    assert _forced_logins(client) == 1


async def test_rejection_of_a_request_sent_before_a_login_reuses_that_login(hass) -> None:
    """A request that went out with the old cookie, rejected after another
    caller logged in, retries on the new session without a login of its
    own."""
    client = build_mock_client()
    coordinator = _coordinator(hass, client)
    seen = coordinator._login_count
    coordinator._login_count += 1  # someone logged in meanwhile

    await coordinator._async_relogin(seen)

    assert _forced_logins(client) == 0


# ----------------------------------------------------------------------
# Opening a message
# ----------------------------------------------------------------------


async def test_opening_a_message_drops_that_mailboxs_cached_list(hass, freezer) -> None:
    """Opening a message marks it read; the cached list (reused while the
    unread count stays the same) would keep showing it unread."""
    freezer.move_to(NOON)
    client = build_mock_client()
    client.async_bootstrap_messages.return_value = True
    client.async_get_unread_messages_count.return_value = {"data": {"inbox": 1}}
    message = {
        "messageId": "1",
        "senderName": "Anna Nowak",
        "topic": "Zebranie",
        "content": "",
        "sendDate": "2026-10-07T08:00:00",
        "readDate": None,
    }
    client.async_get_messages.side_effect = messages_by_mailbox({"inbox": {"data": [message]}})
    client.async_get_message.return_value = {"data": {"topic": "Zebranie", "Message": ""}}
    coordinator = _coordinator(hass, client)

    def inbox_lists() -> int:
        return sum(
            1
            for call in client.async_get_messages.call_args_list
            if call.kwargs.get("mailbox", "inbox") == "inbox"
        )

    await coordinator._async_update_data()
    assert inbox_lists() == 1

    await coordinator.async_fetch_message("inbox", "1")
    freezer.tick(timedelta(minutes=20))
    await coordinator._async_update_data()

    # Same unread count (another message arrived meanwhile, say), but the
    # list is asked for again.
    assert inbox_lists() == 2


# ----------------------------------------------------------------------
# Grade scale closed to the account
# ----------------------------------------------------------------------


async def test_grading_system_refusal_clears_an_old_repair_issue(hass, issue_registry) -> None:
    """A 404 on GradingSystem means the default scale - and a degraded-
    endpoint issue an older version raised for it is cleared."""
    client = build_mock_client()
    client.async_get_grading_system.side_effect = LibrusUnexpectedResponseError(
        "HTTP 404", status_code=404
    )
    coordinator = _coordinator(hass, client)
    issue_id = optional_endpoint_issue_id(coordinator.config_entry.entry_id, "GradingSystem")
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_OPTIONAL_ENDPOINT_DEGRADED,
        translation_placeholders={"label": "GradingSystem", "since": "2026-09-01"},
    )

    await coordinator._async_update_data()

    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None
    assert "GradingSystem" not in coordinator.degraded_endpoints


# ----------------------------------------------------------------------
# Badges saved by 0.12.4 or older (keys only)
# ----------------------------------------------------------------------


def _six_today() -> dict:
    today = dt_util.now().date().isoformat()
    return {"Grades": [{"Id": 1, "Grade": "6", "Subject": {"Id": 100}, "Semester": 1, "AddDate": today}]}


async def test_legacy_badges_are_merged_and_a_legacy_honours_dropped(hass, hass_storage) -> None:
    events = async_capture_events(hass, EVENT_ACHIEVEMENT_UNLOCKED)
    entry = make_config_entry()
    _store(hass_storage, entry.entry_id, {"achievements": ["praise", "honours"]})
    coordinator = _coordinator(hass, build_mock_client(async_get_grades=_six_today()), entry)
    await coordinator.async_restore_state()
    # Not merged yet: a save now (an unload while Librus is down) keeps them.
    assert coordinator._tracked_state()["achievements"] == ["praise", "honours"]

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    keys = {a["key"] for a in coordinator.achievements}
    assert "praise" in keys
    # Honours is earned only once the school year is over - dropped in the
    # same cycle the legacy keys are merged.
    assert "honours" not in keys
    assert "first_six" in keys
    assert events == []  # the first pass is still the quiet one
    assert coordinator._tracked_state()["achievements"] is None


async def test_legacy_badges_survive_a_failed_first_cycle_and_a_restart(
    hass, hass_storage
) -> None:
    """A section failing on the first cycle after the upgrade, then a
    restart: the legacy keys are kept, and the first pass that records
    badges is still quiet."""
    events = async_capture_events(hass, EVENT_ACHIEVEMENT_UNLOCKED)
    entry = make_config_entry()
    _store(hass_storage, entry.entry_id, {"achievements": ["praise"]})
    client = build_mock_client(async_get_grades=_six_today())
    client.async_get_attendance_types.side_effect = LibrusConnectionError("timeout")
    first = _coordinator(hass, client, entry)
    await first.async_restore_state()

    await first._async_update_data()  # Attendances/Types failed: badges not recorded
    assert "praise" in {a["key"] for a in first.achievements}
    saved = json.loads(json.dumps(first._state_to_save()))

    second_entry = make_config_entry()
    _store(hass_storage, second_entry.entry_id, saved)
    second = _coordinator(hass, build_mock_client(async_get_grades=_six_today()), second_entry)
    await second.async_restore_state()
    await second._async_update_data()
    await hass.async_block_till_done()

    keys = {a["key"] for a in second.achievements}
    assert {"praise", "first_six"} <= keys
    assert events == []


# ----------------------------------------------------------------------
# Unload
# ----------------------------------------------------------------------


async def test_unload_writes_once_and_a_disk_error_does_not_fail_it(hass) -> None:
    client = build_mock_client()
    entry = await setup_integration(hass, client)
    coordinator = entry.runtime_data

    with (
        patch.object(
            coordinator._state_store, "async_save", side_effect=OSError("disk full")
        ) as save,
        patch.object(coordinator._state_store, "async_delay_save") as delay_save,
    ):
        assert await hass.config_entries.async_unload(entry.entry_id)
        save.assert_awaited_once()

        # Something still finishing after the unload schedules nothing.
        coordinator.record_homework_done("hw-1")
        coordinator._state_dirty = True
        coordinator._maybe_schedule_save()

    delay_save.assert_not_called()
