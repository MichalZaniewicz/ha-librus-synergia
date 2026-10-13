"""Point grades (schools grading in points or percent)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.librus_synergia.coordinator import LibrusDataUpdateCoordinator

from .conftest import build_mock_client, make_config_entry, setup_integration

POINT_CATEGORIES = {
    "Categories": [
        {"Id": 1, "Name": "Sprawdzian", "Weight": 2, "CountToTheAverage": True, "ValueTo": 100},
        {"Id": 2, "Name": "Kartkówka", "Weight": 1, "CountToTheAverage": True, "ValueTo": 20},
    ]
}
POINT_GRADES = {
    "Grades": [
        {"Id": 1, "Grade": "85", "GradeValue": 85, "Category": {"Id": 1},
         "Subject": {"Id": 100}, "Semester": 1, "AddDate": "2026-10-01 10:00:00"},
        {"Id": 2, "Grade": "15", "GradeValue": 15, "Category": {"Id": 2},
         "Subject": {"Id": 100}, "Semester": 1, "AddDate": "2026-10-02 10:00:00"},
    ]
}
SUBJECTS = {"Subjects": [{"Id": 100, "Name": "Matematyka"}]}


def _coordinator(hass, client) -> LibrusDataUpdateCoordinator:
    entry = make_config_entry()
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))


async def test_point_grades_parsed(hass) -> None:
    client = build_mock_client(
        async_get_point_grades=POINT_GRADES,
        async_get_point_grade_categories=POINT_CATEGORIES,
    )
    data = await _coordinator(hass, client)._async_update_data()

    assert [(g.points, g.max_points) for g in data.point_grades] == [(85.0, 100.0), (15.0, 20.0)]


async def test_point_grades_skipped_when_school_has_them_off(hass) -> None:
    client = build_mock_client(
        async_get_units={"Units": [{"GradesSettings": {"PointGradesEnabled": False}}]}
    )
    coordinator = _coordinator(hass, client)
    await coordinator._async_update_data()  # learns the flag from Units
    client.async_get_point_grades.reset_mock()

    await coordinator._async_update_data()

    assert coordinator.point_grades_enabled is False
    client.async_get_point_grades.assert_not_called()


async def test_point_grades_sensor_and_subject_attributes(hass) -> None:
    client = build_mock_client(
        async_get_point_grades=POINT_GRADES,
        async_get_point_grade_categories=POINT_CATEGORIES,
        async_get_subjects=SUBJECTS,
    )
    entry = await setup_integration(hass, client)
    registry = er.async_get(hass)

    entity_id = registry.async_get_entity_id("sensor", "librus_synergia", f"{entry.entry_id}_point_grades")
    state = hass.states.get(entity_id)
    # (85*2 + 15*1) / (100*2 + 20*1)
    assert float(state.state) == round(100 * 185 / 220, 1)
    assert state.attributes["subjects"]["Matematyka"]["count"] == 2
    assert state.attributes["recent"][0]["percentage"] == 75.0

    subject_id = registry.async_get_entity_id(
        "sensor", "librus_synergia", f"{entry.entry_id}_subject_100_average"
    )
    subject = hass.states.get(subject_id)
    assert subject.attributes["points_percentage"] == round(100 * 185 / 220, 1)
    assert len(subject.attributes["point_grades"]) == 2


async def test_no_point_grades_sensor_without_them(hass) -> None:
    client = build_mock_client(
        async_get_units={"Units": [{"GradesSettings": {"PointGradesEnabled": False}}]}
    )
    entry = await setup_integration(hass, client)

    assert (
        er.async_get(hass).async_get_entity_id(
            "sensor", "librus_synergia", f"{entry.entry_id}_point_grades"
        )
        is None
    )


async def test_get_grades_service_includes_point_grades(hass) -> None:
    client = build_mock_client(
        async_get_point_grades=POINT_GRADES,
        async_get_point_grade_categories=POINT_CATEGORIES,
        async_get_subjects=SUBJECTS,
    )
    entry = await setup_integration(hass, client)
    device = dr.async_get(hass).async_get_device_by_identifier(
        ("librus_synergia", entry.entry_id), entry.entry_id
    )

    response = await hass.services.async_call(
        "librus_synergia",
        "get_grades",
        {"device_id": device.id},
        blocking=True,
        return_response=True,
    )

    assert [g["percentage"] for g in response["point_grades"]] == [75.0, 85.0]
    assert response["points_percentage"] == round(100 * 185 / 220, 1)


async def test_assist_grades_tool_lists_point_grades(hass) -> None:
    from .test_llm_api import _call

    client = build_mock_client(
        async_get_point_grades=POINT_GRADES,
        async_get_point_grade_categories=POINT_CATEGORIES,
        async_get_subjects=SUBJECTS,
    )
    await setup_integration(hass, client)

    data = await _call(hass, "librus_get_grades", subject="matem")

    assert [(g["points"], g["max_points"]) for g in data["point_grades"]] == [(15.0, 20.0), (85.0, 100.0)]
    assert data["point_grades"][0]["percent"] == 75.0
    assert data["point_grades"][1]["category"] == "Sprawdzian"
    assert "Matematyka" in data["points_percent_by_subject"]


async def test_point_thresholds_per_subject(hass) -> None:
    """A subject with its own scale (options) uses it; an entry for an
    unknown subject is listed so the user can see the typo."""
    client = build_mock_client(
        async_get_point_grades=POINT_GRADES,
        async_get_point_grade_categories=POINT_CATEGORIES,
        async_get_subjects=SUBJECTS,
    )
    entry = await setup_integration(
        hass,
        client,
        options={
            "point_grade_thresholds": "40, 55, 75, 90, 98",
            "point_grade_thresholds_by_subject": "MATEMATYKA: 30, 50, 70, 80, 95\nmatma: 1, 2, 3, 4, 5",
        },
    )
    registry = er.async_get(hass)
    forecast = hass.states.get(
        registry.async_get_entity_id("sensor", "librus_synergia", f"{entry.entry_id}_grade_forecast")
    )
    assert forecast.attributes["point_thresholds"] == [40, 55, 75, 90, 98]
    assert forecast.attributes["point_thresholds_by_subject"] == {"Matematyka": [30, 50, 70, 80, 95]}
    assert forecast.attributes["point_thresholds_unmatched"] == ["matma: 1, 2, 3, 4, 5"]
    # 84.1 % is a 5 on the subject's own scale (a 4 on the general one).
    (row,) = [s for s in forecast.attributes["subjects"] if s["subject"] == "Matematyka"]
    assert row["predicted"] == 5

    subject = hass.states.get(
        registry.async_get_entity_id("sensor", "librus_synergia", f"{entry.entry_id}_subject_100_average")
    )
    assert subject.attributes["point_thresholds"] == [30, 50, 70, 80, 95]
    assert subject.attributes["predicted_grade"] == 5
