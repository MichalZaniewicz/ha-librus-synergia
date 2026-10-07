"""Grade forecast entities: the sensor, the at-risk binary sensor, the
subject-average attributes and the forecast-changed event."""

from __future__ import annotations

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.librus_synergia.const import DOMAIN, EVENT_FORECAST_CHANGED

from .conftest import build_mock_client, setup_integration

_NOW = "2026-10-20T12:00:00+00:00"


def _entity_id(hass, entry, domain: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, f"{entry.entry_id}_{key}"
    )


def _grade(gid: int, value: str, subject: int, day: str = "2026-09-15") -> dict:
    return {
        "Id": gid,
        "Grade": value,
        "Category": {"Id": 10},
        "Subject": {"Id": subject},
        "Semester": 1,
        "AddDate": f"{day} 10:00:00",
    }


def _client(grades: list[dict]):
    return build_mock_client(
        async_get_subjects={
            "Subjects": [
                {"Id": 100, "Name": "Matematyka"},
                {"Id": 200, "Name": "Historia"},
            ]
        },
        async_get_grades={"Grades": grades},
        async_get_grade_categories={
            "Categories": [
                {"Id": 10, "Name": "Kartkówka", "CountToTheAverage": True, "Weight": 1}
            ]
        },
    )


async def test_forecast_sensor_and_risk(hass, freezer) -> None:
    freezer.move_to(_NOW)
    client = _client(
        [
            _grade(1, "5", 100),
            _grade(2, "5", 100),
            # Historia: a 3 earlier, two 1s this week -> 1.67, at risk and declining.
            _grade(3, "3", 200, day="2026-09-20"),
            _grade(4, "1", 200, day="2026-10-18"),
            _grade(5, "1", 200, day="2026-10-19"),
        ]
    )
    entry = await setup_integration(hass, client)

    forecast = hass.states.get(_entity_id(hass, entry, "sensor", "grade_forecast"))
    assert float(forecast.state) == 3.0  # (1 + 5) / 2
    assert forecast.attributes["basis"] == "semester_1"
    assert forecast.attributes["at_risk"] == ["Historia"]
    assert forecast.attributes["declining"] == ["Historia"]
    assert forecast.attributes["honours_average"] is False
    assert [s["subject"] for s in forecast.attributes["subjects"]] == [
        "Historia",
        "Matematyka",
    ]

    risk = hass.states.get(_entity_id(hass, entry, "binary_sensor", "grade_risk"))
    assert risk.state == "on"
    assert risk.attributes["subjects"][0]["subject"] == "Historia"

    math = hass.states.get(_entity_id(hass, entry, "sensor", "subject_100_average"))
    assert math.attributes["predicted_grade"] == 5
    assert math.attributes["forecast_average"] == 5.0
    assert math.attributes["forecast_weight"] == 2
    assert math.attributes["next_grade_at"] == 5.5
    # (10 + 6n) / (2 + n) >= 5.5 -> n >= 2.
    assert math.attributes["sixes_to_next_grade"] == 2
    assert math.attributes["forecast_declining"] is False


async def test_custom_thresholds(hass, freezer) -> None:
    freezer.move_to(_NOW)
    client = _client([_grade(1, "5", 100), _grade(2, "4", 100)])
    entry = await setup_integration(
        hass, client, options={"grade_thresholds": "1.6, 2.6, 3.6, 4.5, 5.3"}
    )
    math = hass.states.get(_entity_id(hass, entry, "sensor", "subject_100_average"))
    assert math.attributes["predicted_grade"] == 5  # 4.5 reaches the 4.5 threshold


async def test_forecast_changed_event(hass, freezer) -> None:
    freezer.move_to(_NOW)
    client = _client([_grade(1, "4", 100)])
    entry = await setup_integration(hass, client)
    events = async_capture_events(hass, EVENT_FORECAST_CHANGED)

    # Same data again: nothing.
    await entry.runtime_data.async_refresh()
    assert events == []

    client.async_get_grades.return_value = {
        "Grades": [_grade(1, "4", 100), _grade(2, "1", 100, day="2026-10-19")]
    }
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 1
    data = events[0].data
    assert data["entry_id"] == entry.entry_id
    assert data["subject"] == "Matematyka"
    assert (data["old"], data["new"], data["direction"]) == (4, 2, "down")
    assert data["average"] == 2.5

    event_entity = hass.states.get(_entity_id(hass, entry, "event", "forecast_event"))
    assert event_entity.attributes["event_type"] == "down"
