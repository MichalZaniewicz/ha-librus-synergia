"""Review round 5: saved fetch times trusted after a quiet spell, the
kindergarten LID kept through a school break, and no account id in the
kindergarten discovery source."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.util import dt as dt_util

from custom_components.librus_synergia.const import STATE_STORE_VERSION
from custom_components.librus_synergia.coordinator import (
    LibrusDataUpdateCoordinator,
    payload_store_key,
    state_store_key,
)
from librus_synergia import LibrusSessionExpiredError

from .conftest import ME_PAYLOAD, build_mock_client, make_config_entry

# ~05:00 on a Wednesday in the test time zone (US/Pacific): clear of both
# midnights, and two days later is still the same week.
_FROZEN = "2026-09-09T12:00:00+00:00"

CHILD_LID = "LID-AUTH-USER-77-CHILD"
PARENT_LID = "LID-AUTH-USER-77-PARENT"


def _coordinator(hass, client, entry=None) -> LibrusDataUpdateCoordinator:
    entry = entry or make_config_entry()
    if entry.state is not ConfigEntryState.SETUP_IN_PROGRESS:
        entry.add_to_hass(hass)
        entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


def _store(hass_storage, key: str, data: dict) -> None:
    hass_storage[key] = {
        "version": STATE_STORE_VERSION,
        "minor_version": 1,
        "key": key,
        "data": data,
    }


def _this_week() -> date:
    today = dt_util.now().date()
    return today - timedelta(days=today.weekday())


def _forbidden(endpoint: str) -> LibrusSessionExpiredError:
    return LibrusSessionExpiredError(
        f"Session rejected on .../{endpoint} (HTTP 403).", status_code=403
    )


def _entries(day: date) -> dict:
    return {
        "timetableEntries": [
            {
                "identifier": "L1",
                "activityTypeIdentifier": "ACT1",
                "type": "planned",
                "date": day.isoformat(),
                "startTime": "07:00",
                "endTime": "12:00",
                "teachers": [],
            }
        ]
    }


# ----------------------------------------------------------------------
# Saved fetch times after a restart (finding 1)
# ----------------------------------------------------------------------


async def test_only_a_real_change_dates_the_payloads(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    coordinator = _coordinator(hass, build_mock_client())

    coordinator._remember_payload("Me", {"Me": {"a": 1}})
    first = coordinator._payloads_changed_at
    assert first == dt_util.utcnow()

    freezer.tick(timedelta(hours=1))
    coordinator._remember_payload("Me", {"Me": {"a": 1}})
    assert coordinator._payloads_changed_at == first

    coordinator._remember_timetable_week(_this_week(), {"Timetable": {}})
    assert coordinator._payloads_changed_at == dt_util.utcnow()
    saved = coordinator._tracked_to_save()
    assert saved["payloads_changed_at"] == dt_util.utcnow().isoformat()


async def test_restart_trusts_fetch_times_after_a_quiet_spell(hass, hass_storage, freezer) -> None:
    """Two quiet days: the daily lookups were asked again but nothing
    changed, so the responses file wasn't rewritten (its `saved_at` is two
    days old) while the tracked state was. Every fetch time is trusted - a
    restart used to ask for all of them again."""
    freezer.move_to(_FROZEN)
    first = _coordinator(hass, build_mock_client())
    await first._async_update_data()
    payloads = json.loads(json.dumps(first._payloads_to_save()))
    changed_at = first._payloads_changed_at
    assert changed_at is not None

    freezer.tick(timedelta(days=2))
    await first._async_update_data()
    # Same responses: the change time didn't move.
    assert first._payloads_changed_at == changed_at
    entry = make_config_entry()
    _store(hass_storage, payload_store_key(entry.entry_id), payloads)
    _store(
        hass_storage,
        state_store_key(entry.entry_id),
        json.loads(json.dumps(first._tracked_to_save())),
    )

    client = build_mock_client()
    second = _coordinator(hass, client, entry)
    await second.async_restore_state()
    assert second.reference_data_fetched_at == dt_util.utcnow()
    assert "Realizations" in second._fetched_at
    await second._async_update_data()

    client.async_get_subjects.assert_not_called()
    client.async_get_realizations.assert_not_called()


async def test_restart_distrusts_fetch_times_when_a_change_missed_the_file(
    hass, hass_storage, freezer
) -> None:
    """A response changed after the responses file was written (HA stopped
    before the next write): fetch times after that file are dropped."""
    freezer.move_to(_FROZEN)
    client = build_mock_client()
    first = _coordinator(hass, client)
    await first._async_update_data()
    payloads = json.loads(json.dumps(first._payloads_to_save()))

    freezer.tick(timedelta(hours=25))
    client.async_get_subjects.return_value = {"Subjects": [{"Id": 100, "Name": "Matematyka"}]}
    await first._async_update_data()
    assert first._payloads_changed_at == dt_util.utcnow()
    entry = make_config_entry()
    _store(hass_storage, payload_store_key(entry.entry_id), payloads)
    _store(
        hass_storage,
        state_store_key(entry.entry_id),
        json.loads(json.dumps(first._tracked_to_save())),
    )

    second = _coordinator(hass, build_mock_client(), entry)
    await second.async_restore_state()

    assert second.reference_data_fetched_at is None
    assert "Realizations" not in second._fetched_at


async def test_tracked_state_without_a_change_time_keeps_the_old_rule(
    hass, hass_storage, freezer
) -> None:
    """Saved by an older version (no `payloads_changed_at`): fetch times
    after the responses file are dropped, as before - one extra round of
    requests on the first restart after the update."""
    freezer.move_to(_FROZEN)
    first = _coordinator(hass, build_mock_client())
    await first._async_update_data()
    payloads = json.loads(json.dumps(first._payloads_to_save()))

    freezer.tick(timedelta(days=2))
    await first._async_update_data()
    tracked = json.loads(json.dumps(first._tracked_to_save()))
    del tracked["payloads_changed_at"]
    entry = make_config_entry()
    _store(hass_storage, payload_store_key(entry.entry_id), payloads)
    _store(hass_storage, state_store_key(entry.entry_id), tracked)

    second = _coordinator(hass, build_mock_client(), entry)
    await second.async_restore_state()

    assert second.reference_data_fetched_at is None


# ----------------------------------------------------------------------
# Kindergarten LID through a school break (finding 2)
# ----------------------------------------------------------------------


async def test_refused_kindergarten_timetable_for_a_day_drops_the_lid(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    client = build_mock_client()
    client.async_get_kindergarten_timetable.side_effect = _forbidden("kindergartens/timetable")
    coordinator = _coordinator(hass, client)
    coordinator._kindergarten_lid = CHILD_LID
    coordinator._kindergarten_found_on = dt_util.now().date()

    await coordinator._async_update_data()
    assert coordinator.is_kindergarten
    assert coordinator._kindergarten_refused_since == dt_util.utcnow()
    # Refused is not empty: the empty clock doesn't start.
    assert coordinator._kindergarten_empty_since is None
    assert coordinator.kindergarten_diagnostics["refused_since"] is not None

    freezer.tick(timedelta(hours=25))
    await coordinator._async_update_data()

    assert not coordinator.is_kindergarten
    assert client.async_get_timetable.call_count == 2


async def test_answered_week_resets_the_refused_clock(hass, freezer) -> None:
    freezer.move_to(_FROZEN)
    client = build_mock_client()
    client.async_get_kindergarten_timetable.side_effect = _forbidden("kindergartens/timetable")
    coordinator = _coordinator(hass, client)
    coordinator._kindergarten_lid = CHILD_LID
    coordinator._kindergarten_found_on = dt_util.now().date()
    await coordinator._async_update_data()

    freezer.tick(timedelta(hours=12))
    client.async_get_kindergarten_timetable.side_effect = None
    await coordinator._async_update_data()
    assert coordinator._kindergarten_refused_since is None

    freezer.tick(timedelta(hours=13))
    await coordinator._async_update_data()
    assert coordinator.is_kindergarten


async def test_same_lid_found_again_is_not_a_new_kindergarten(hass, freezer, caplog) -> None:
    """Three weeks without a lesson drop the LID; the discovery finds the
    same one again (the past 30 days had lessons). No INFO "detected" log,
    no reference refresh, the empty clock keeps running - and the next days
    don't drop it again."""
    freezer.move_to(_FROZEN)
    today = dt_util.now().date()
    client = build_mock_client()
    client.async_get_timetable.side_effect = _forbidden("Timetables")
    client.async_get_token_info.return_value = {"UserIdentifier": PARENT_LID}
    client.async_get_user_info.return_value = {"children": [{"identifier": CHILD_LID}]}

    async def timetable(lid, date_from, date_to):
        # The discovery's long window has lessons; the polled weeks don't.
        if lid == CHILD_LID and (date_to - date_from).days > 7:
            return _entries(today - timedelta(days=25))
        return {"timetableEntries": []}

    client.async_get_kindergarten_timetable.side_effect = timetable
    coordinator = _coordinator(hass, client)
    empty_since = dt_util.utcnow() - timedelta(days=22)
    reference_at = dt_util.utcnow() - timedelta(hours=1)
    coordinator._kindergarten_lid = CHILD_LID
    coordinator._kindergarten_source = "Auth/UserInfo"
    coordinator._kindergarten_found_on = today
    coordinator._kindergarten_empty_since = empty_since
    coordinator._reference_data_fetched_at = reference_at

    with caplog.at_level(logging.INFO):
        await coordinator._async_update_data()

    assert coordinator.is_kindergarten
    assert coordinator._kindergarten_empty_since == empty_since
    assert coordinator._kindergarten_confirmed_at == dt_util.utcnow()
    assert coordinator.reference_data_fetched_at == reference_at
    assert "Kindergarten account detected" not in caplog.text
    assert client.async_get_kindergartener.call_count == 1

    freezer.tick(timedelta(days=1))
    ordinary = client.async_get_timetable.call_count
    await coordinator._async_update_data()

    assert coordinator.is_kindergarten
    assert client.async_get_timetable.call_count == ordinary
    assert client.async_get_kindergartener.call_count == 1


# ----------------------------------------------------------------------
# No account id in the discovery source (finding 3)
# ----------------------------------------------------------------------


def _users_client(account: dict, child_from: int):
    client = build_mock_client()
    client.async_get_timetable.side_effect = _forbidden("Timetables")
    client.async_get_me.return_value = {"Me": {"Account": account, "User": {}}}

    async def user(numeric_id):
        return {"Child": CHILD_LID} if numeric_id == child_from else {}

    async def timetable(lid, date_from, date_to):
        return _entries(dt_util.now().date()) if lid == CHILD_LID else {"timetableEntries": []}

    client.async_get_user.side_effect = user
    client.async_get_kindergarten_timetable.side_effect = timetable
    return client


async def test_users_source_is_named_after_the_field(hass) -> None:
    client = _users_client({"UserId": 7654321, "Id": 1234567}, child_from=7654321)
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    assert coordinator.is_kindergarten
    assert coordinator.kindergarten_diagnostics["source"] == "Users/UserId"
    dumped = json.dumps(coordinator.kindergarten_diagnostics)
    assert "7654321" not in dumped
    assert "1234567" not in dumped

    client = _users_client({"Id": 1234567}, child_from=1234567)
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()

    assert coordinator.kindergarten_diagnostics["source"] == "Users/Id"


async def test_saved_users_source_with_the_id_is_normalized(hass, hass_storage) -> None:
    kindergarten = {"lid": CHILD_LID, "group_id": None, "source": "Users/1234567"}
    cases = (
        ({"Me": {"Account": {"UserId": 1234567, "Id": 5}}}, "Users/UserId"),
        (ME_PAYLOAD, "Users/Id"),  # Account.Id is 1234567
        (None, "Users/Id"),  # /Me not saved
    )
    for me, expected in cases:
        entry = make_config_entry()
        _store(hass_storage, state_store_key(entry.entry_id), {"kindergarten": kindergarten})
        _store(
            hass_storage,
            payload_store_key(entry.entry_id),
            {"payloads": {"Me": me} if me else {}, "saved_at": dt_util.utcnow().isoformat()},
        )
        coordinator = _coordinator(hass, build_mock_client(), entry)

        await coordinator.async_restore_state()

        assert coordinator.kindergarten_diagnostics["source"] == expected
        assert coordinator._tracked_state()["kindergarten"]["source"] == expected
