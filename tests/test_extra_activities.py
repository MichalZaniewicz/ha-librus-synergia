"""Activities outside the regular timetable (issue #23): extra activities,
the individualised learning path and one-to-one teaching, shown in the
timetable calendar."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.librus_synergia.const import CONF_EXTRA_ACTIVITIES, DOMAIN
from librus_synergia import LibrusSessionExpiredError

from .conftest import build_mock_client, setup_integration


def _activity(day: str, start: str, end: str, title: str, **extra) -> dict:
    # Shape of a real `OtherActivitiesRegister` item (2026-10-30), names made up.
    return {
        "status": "ACTUAL",
        "identifier": f"LID-OAR-SCHEDULEEVENT-{title}-{start}",
        "date": day,
        "startTime": start,
        "endTime": end,
        "teacherName": "Anna Nowak",
        "teacherAccountId": 1000001,
        "title": title,
        "classroom": {"identifier": "90001", "symbol": "101", "name": "101", "size": "0"},
        **extra,
    }


def _calendar_id(hass, entry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "calendar", DOMAIN, f"{entry.entry_id}_timetable"
    )
    assert entity_id is not None
    return entity_id


async def _events_on(hass, entity_id: str, days_ahead: int) -> list[dict]:
    # One calendar date on both ends, so exactly one week is asked for.
    start = dt_util.start_of_local_day() + timedelta(days=days_ahead)
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": entity_id,
            "start_date_time": start,
            "end_date_time": start.replace(hour=23, minute=59, second=59),
        },
        blocking=True,
        return_response=True,
    )
    return response[entity_id]["events"]


def _client_with(activities_by_kind: dict[str, list[dict]]):
    def extra(kind, date_from, date_to):
        return {"data": activities_by_kind.get(kind, [])}

    client = build_mock_client()
    client.async_get_extra_activities.side_effect = extra
    return client


async def test_extra_activities_show_in_the_timetable_calendar(hass) -> None:
    tomorrow = (dt_util.now().date() + timedelta(days=1)).isoformat()
    client = _client_with(
        {
            "other": [
                _activity(tomorrow, "07:50", "08:50", "Zajęcia dodatkowe A"),
                _activity(tomorrow, "14:40", "15:25", "Koło szachowe", status="CANCELED"),
            ],
            "one_to_one": [_activity(tomorrow, "12:00", "12:45", "Matematyka")],
        }
    )
    entry = await setup_integration(hass, client)
    coordinator = entry.runtime_data

    # One request per kind for this week and next together.
    week_start = dt_util.now().date() - timedelta(days=dt_util.now().date().weekday())
    kinds = sorted(call.args[0] for call in client.async_get_extra_activities.call_args_list)
    assert kinds == ["individual_path", "one_to_one", "other"]
    first = client.async_get_extra_activities.call_args_list[0].args
    assert (first[1], first[2]) == (week_start, week_start + timedelta(days=13))
    assert len(coordinator.data.extra_activities) == 3

    events = await _events_on(hass, _calendar_id(hass, entry), 1)
    by_summary = {event["summary"]: event for event in events}
    assert list(by_summary) == ["Zajęcia dodatkowe A", "Matematyka", "Koło szachowe (odwołane)"]
    first_event = by_summary["Zajęcia dodatkowe A"]
    assert first_event["location"] == "101"
    # The teacher on the first line, like a lesson; the kind only when the
    # title doesn't already say it.
    assert first_event["description"] == "Anna Nowak"
    assert by_summary["Matematyka"]["description"] == "Anna Nowak\nNauczanie indywidualne"
    assert by_summary["Koło szachowe (odwołane)"]["description"] == "Anna Nowak\nZajęcia dodatkowe"

    # Not asked again within the hour.
    client.async_get_extra_activities.reset_mock()
    await coordinator.async_refresh()
    client.async_get_extra_activities.assert_not_called()


async def test_extra_activities_of_another_week_come_with_its_timetable(hass) -> None:
    day = dt_util.now().date() + timedelta(days=21)
    client = _client_with({"other": [_activity(day.isoformat(), "16:00", "17:00", "Chór")]})
    entry = await setup_integration(hass, client)
    client.async_get_extra_activities.reset_mock()

    for _ in range(2):
        events = await _events_on(hass, _calendar_id(hass, entry), 21)
        assert [event["summary"] for event in events] == ["Chór"]
    # Fetched once, with that week's timetable, for that week only.
    calls = client.async_get_extra_activities.call_args_list
    assert len(calls) == 3
    week = day - timedelta(days=day.weekday())
    assert all((c.args[1], c.args[2]) == (week, week + timedelta(days=6)) for c in calls)


async def test_extra_activity_kinds_switched_off_are_not_asked_for(hass) -> None:
    tomorrow = (dt_util.now().date() + timedelta(days=1)).isoformat()
    client = _client_with({"other": [_activity(tomorrow, "07:50", "08:50", "Zajęcia dodatkowe")]})
    entry = await setup_integration(hass, client, options={CONF_EXTRA_ACTIVITIES: ["one_to_one"]})

    kinds = [call.args[0] for call in client.async_get_extra_activities.call_args_list]
    assert kinds == ["one_to_one"]
    assert await _events_on(hass, _calendar_id(hass, entry), 1) == []


async def test_refused_extra_activity_list_is_not_a_degraded_endpoint(hass) -> None:
    client = build_mock_client()
    client.async_get_extra_activities.side_effect = LibrusSessionExpiredError(
        "forbidden", status_code=403
    )
    entry = await setup_integration(hass, client)
    coordinator = entry.runtime_data

    assert coordinator.last_update_success
    assert coordinator.data.extra_activities == []
    assert not coordinator._failed_this_cycle & {
        "Timetables/OtherActivitiesRegister",
        "Timetables/IndividualLearningPath",
        "Timetables/OneToOneLearningPlan",
    }
