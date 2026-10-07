"""What a substitution changes (room, teacher, moved) and absence
justifications (sensor, awaiting days, status-change event)."""

from __future__ import annotations

from datetime import date, timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.librus_synergia.calendar import _lesson_to_event
from custom_components.librus_synergia.const import DOMAIN, EVENT_JUSTIFICATION_STATUS
from custom_components.librus_synergia.coordinator import (
    LibrusDataUpdateCoordinator,
    lesson_change,
)
from librus_synergia.models import LessonData, LibrusData, MeData, OriginalLessonData

from .conftest import build_mock_client, make_config_entry, setup_integration

DAY = date(2026, 10, 8)


def _data() -> LibrusData:
    return LibrusData(
        me=MeData(account_id=1, first_name="Ola", last_name="Kowalska"),
        grades=[],
        grade_categories={},
        notes=[],
        attendances=[],
        attendance_types={},
        timetable={},
        homeworks=[],
        school_notices=[],
        lucky_number=None,
        subjects={1: "Matematyka", 2: "Chemia"},
        teachers={10: "Anna Nowak", 20: "Jan Kowal"},
        classrooms={12: "12", 21: "21"},
    )


def _lesson(**overrides) -> LessonData:
    base = {
        "lesson_no": 3,
        "hour_from": "09:50",
        "hour_to": "10:35",
        "subject_id": 1,
        "teacher_id": 10,
        "classroom_id": 21,
        "is_canceled": False,
        "is_substitution": True,
    }
    base.update(overrides)
    return LessonData(**base)


def _original(**overrides) -> OriginalLessonData:
    base = {
        "date": DAY.isoformat(),
        "lesson_no": 3,
        "hour_from": "09:50",
        "hour_to": "10:35",
        "subject_id": 1,
        "teacher_id": 10,
        "classroom_id": 12,
    }
    base.update(overrides)
    return OriginalLessonData(**base)


def test_room_change_only() -> None:
    change = lesson_change(DAY, _lesson(original=_original()), _data())
    assert change["kind"] == "room_change"
    assert (change["original_classroom"], change["classroom"]) == ("12", "21")


def test_other_teacher_and_subject_is_a_substitution() -> None:
    change = lesson_change(DAY, _lesson(original=_original(subject_id=2, teacher_id=20)), _data())
    assert change["kind"] == "substitution"
    assert change["original_subject"] == "Chemia"
    assert change["original_teacher"] == "Jan Kowal"


def test_moved_lesson() -> None:
    change = lesson_change(DAY, _lesson(original=_original(lesson_no=5, classroom_id=21)), _data())
    assert change["kind"] == "moved"
    assert change["original_lesson_no"] == 5


def test_ordinary_and_cancelled_lessons() -> None:
    assert lesson_change(DAY, _lesson(is_substitution=False), _data())["kind"] is None
    assert lesson_change(DAY, _lesson(is_canceled=True, is_substitution=False), _data())["kind"] == "canceled"
    # A substitution without the original data stays a plain substitution.
    assert lesson_change(DAY, _lesson(), _data())["kind"] == "substitution"


def test_calendar_event_labels_the_change() -> None:
    event = _lesson_to_event(DAY, _lesson(original=_original()), _data())
    assert event.summary == "Matematyka (zmiana sali)"
    assert event.description == "Anna Nowak\nZmiana sali: 12 → 21"

    event = _lesson_to_event(DAY, _lesson(original=_original(subject_id=2, teacher_id=20)), _data())
    assert event.summary == "Matematyka (zastępstwo)"
    assert event.description.splitlines()[1] == "Zastępstwo za: Chemia, Jan Kowal"

    # Same subject, another teacher: only the teacher is named.
    event = _lesson_to_event(DAY, _lesson(original=_original(teacher_id=20, classroom_id=21)), _data())
    assert event.description.splitlines()[1] == "Zastępstwo za: Jan Kowal"

    # An ordinary lesson keeps the teacher-only description.
    event = _lesson_to_event(DAY, _lesson(is_substitution=False), _data())
    assert (event.summary, event.description) == ("Matematyka", "Anna Nowak")


# ----------------------------------------------------------------------
# Justifications
# ----------------------------------------------------------------------


def _justifications(status: str = "accept") -> dict:
    return {
        "status": "OK",
        "data": [
            {
                "id": 1,
                "messageFromParent": "Proszę o usprawiedliwienie.",
                "postDate": "2026-09-06 22:17:53",
                "justificationStatus": status,
                "lessons": [],
                "dateFrom": "2026-09-04",
                "dateTo": "2026-09-04",
                "justifiedAbsences": 1,
                "notifiedTeachers": [{"name": "Anna Nowak"}],
            },
            {
                "id": 2,
                "messageFromParent": "Choroba.",
                "postDate": "2026-09-16 08:00:00",
                "justificationStatus": "new",
                "lessons": [],
                "dateFrom": "2026-09-14",
                "dateTo": "2026-09-15",
                "justifiedAbsences": 0,
                "notifiedTeachers": [],
            },
        ],
    }


ATTENDANCE = {
    "Attendances": [
        {"Id": 1, "Date": "2026-09-04", "Type": {"Id": 1}},
        {"Id": 2, "Date": "2026-09-15", "Type": {"Id": 1}},
        {"Id": 3, "Date": "2026-09-22", "Type": {"Id": 1}},
    ]
}
TYPES = {"Types": [{"Id": 1, "Name": "Nieobecność", "IsPresenceKind": False}]}


async def test_justifications_sensor_and_awaiting_days(hass) -> None:
    client = build_mock_client(
        async_get_justifications=_justifications(),
        async_get_attendances=ATTENDANCE,
        async_get_attendance_types=TYPES,
    )
    entry = await setup_integration(hass, client)
    registry = er.async_get(hass)

    justifications = hass.states.get(
        registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_justifications")
    )
    assert justifications.state == "1"  # the "new" one is still pending
    assert justifications.attributes["accepted"] == 1
    assert [j["id"] for j in justifications.attributes["recent"]] == [2, 1]

    unexcused = hass.states.get(
        registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_unexcused_absences")
    )
    assert unexcused.attributes["recent_dates"] == ["2026-09-22", "2026-09-15", "2026-09-04"]
    assert unexcused.attributes["awaiting_justification"] == ["2026-09-22"]
    assert unexcused.attributes["justification_sent"] == ["2026-09-15", "2026-09-04"]


async def test_justification_status_change_fires_event(hass) -> None:
    events = async_capture_events(hass, EVENT_JUSTIFICATION_STATUS)
    entry = make_config_entry()
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    client = build_mock_client(async_get_justifications=_justifications("new"))
    coordinator = LibrusDataUpdateCoordinator(hass, entry, client, timedelta(minutes=20))
    await coordinator._async_update_data()

    client.async_get_justifications.return_value = _justifications("accept")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["id"] == 1
    assert events[0].data["previous_status"] == "new"
    assert events[0].data["accepted"] is True
    assert events[0].data["teachers"] == ["Anna Nowak"]
