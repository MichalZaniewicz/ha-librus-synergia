"""Copy to calendar (calendar_sync.py), emoji in calendar titles
(subject_emoji.py) and absence days (absence_days.py) - against fake data
and a fake target calendar, no Librus."""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntityFeature, CalendarEvent
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from librus_synergia.models import (
    AttendanceData,
    AttendanceTypeData,
    FreeDayData,
    HomeworkEventData,
    JustificationData,
    LibrusData,
    MeData,
)

from custom_components.librus_synergia.absence_days import absence_days, lessons_text
from custom_components.librus_synergia.calendar_sync import (
    async_sync_calendar,
    parse_categories,
    student_tag,
    sync_items,
)
from custom_components.librus_synergia.const import DOMAIN
from custom_components.librus_synergia.subject_emoji import (
    category_emoji,
    subject_emoji,
)

from .conftest import build_mock_client, setup_integration

# Tuesday.
_TODAY = date(2026, 10, 20)
_ENTRY = "01TESTENTRY"


def _data(**kw: Any) -> LibrusData:
    return LibrusData(
        me=MeData(account_id=1, first_name="Ola", last_name="Kowalska"),
        grades=[],
        grade_categories={},
        notes=[],
        attendances=kw.pop("attendances", []),
        attendance_types=kw.pop("attendance_types", {}),
        timetable={},
        homeworks=kw.pop(
            "homeworks",
            [
                HomeworkEventData(
                    id=1, date="2026-10-22", content="Dział 2", category_id=1,
                    subject_id=10, time_from=None,
                ),
                HomeworkEventData(
                    id=2, date="2026-10-23", content="Do Krakowa", category_id=2,
                    subject_id=None, time_from=None,
                ),
                # Past - never copied.
                HomeworkEventData(
                    id=3, date="2026-10-19", content="Dział 1", category_id=1,
                    subject_id=10, time_from=None,
                ),
            ],
        ),
        school_notices=[],
        lucky_number=None,
        subjects={10: "Matematyka", 11: "Chemia"},
        teachers={},
        classrooms={},
        homework_categories={1: "Sprawdzian", 2: "Wycieczka"},
        free_days=kw.pop(
            "free_days",
            [
                FreeDayData(id=5, name="Dzień Nauczyciela", date_from="2026-10-14", date_to="2026-10-14"),
                FreeDayData(id=6, name="Wszystkich Świętych", date_from="2026-11-02", date_to="2026-11-02"),
                # A Saturday - no day off from school.
                FreeDayData(id=7, name="Sobota", date_from="2026-10-24", date_to="2026-10-24"),
            ],
        ),
        **kw,
    )


# --- subject_emoji ---------------------------------------------------------


def test_subject_and_category_emoji() -> None:
    assert subject_emoji("Matematyka") == "📐"
    assert subject_emoji("Język polski") == "📖"
    assert subject_emoji("JĘZYK ANGIELSKI") == "🇬🇧"
    assert subject_emoji("Wiedza o społeczeństwie") == "🗳️"
    assert subject_emoji("Edukacja dla bezpieczeństwa") == "🚑"
    assert subject_emoji("Coś nowego") == "📚"
    assert category_emoji("Kartkówka") == "✏️"
    assert category_emoji("Sprawdzian") == "📝"
    assert category_emoji("Wycieczka") == "🚌"
    assert category_emoji("Inne") is None


# --- absence_days ----------------------------------------------------------

_TYPES = {
    1: AttendanceTypeData(id=1, name="Nieobecność", is_presence_kind=False),
    2: AttendanceTypeData(id=2, name="Spóźnienie", is_presence_kind=True),
    3: AttendanceTypeData(id=3, name="Nieobecność uspr.", is_presence_kind=False),
    100: AttendanceTypeData(id=100, name="Obecność", is_presence_kind=True),
}
_ids = itertools.count(1)


def _att(day: str, lesson_no: int, type_id: int, lesson_id: int | None = 7) -> AttendanceData:
    return AttendanceData(
        id=next(_ids), lesson_id=lesson_id, lesson_no=lesson_no, date=day, semester=1,
        type_id=type_id,
    )


def test_absence_days_group_by_date() -> None:
    data = _data(
        attendances=[
            _att("2026-10-05", 2, 1),
            _att("2026-10-05", 1, 1),
            _att("2026-10-05", 3, 2),
            _att("2026-10-05", 4, 100),  # present - not listed
            _att("2026-10-06", 1, 3),
            _att("2026-10-06", 2, 3),
            _att("2026-10-07", 3, 2),
            _att("2026-10-08", 1, 1),
            _att("2026-10-08", 2, 3),
        ],
        attendance_types=_TYPES,
        lesson_subjects={7: 10},
        justifications=[
            JustificationData(
                id=1, status="sent", message="", posted=None, date_from="2026-10-08",
                date_to="2026-10-08", lessons=[], justified_absences=1, has_attachment=False,
                teachers=[],
            )
        ],
    )
    days = absence_days(data)
    assert [d.date for d in days] == ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]
    monday, tuesday, wednesday, thursday = days
    assert monday.summary == "Nieobecność nieusprawiedliwiona (2 lekcje), spóźnienie"
    # Lesson order, subject from Lessons.
    assert monday.description.splitlines()[0] == "1. Matematyka – Nieobecność"
    assert tuesday.summary == "Nieobecność usprawiedliwiona (2 lekcje)"
    assert wednesday.summary == "Spóźnienie (lekcja 3)"
    assert thursday.summary == "Nieobecność: 1 uspr., 1 nieuspr."
    assert thursday.justification_sent is True
    assert thursday.description.endswith("Usprawiedliwienie wysłane")
    assert monday.justification_sent is False


def test_lessons_text_polish_plural() -> None:
    assert [lessons_text(n) for n in (1, 2, 4, 5, 12, 22, 25)] == [
        "1 lekcja", "2 lekcje", "4 lekcje", "5 lekcji", "12 lekcji", "22 lekcje", "25 lekcji",
    ]


# --- sync_items ------------------------------------------------------------


def test_parse_categories() -> None:
    assert parse_categories("Sprawdzian, kartkówka , ") == ["sprawdzian", "kartkowka"]
    assert parse_categories(["wycieczka", "zebranie, apel"]) == ["wycieczka", "zebranie", "apel"]
    assert parse_categories(None) == []


def test_sync_items_window_categories_free_days_and_prefix() -> None:
    items = sync_items(
        _data(), today=_TODAY, days=30, categories=[], free_days=True, prefix="Ola:"
    )
    assert [(i.key, i.summary, i.start) for i in items] == [
        ("a1", "Ola: [Sprawdzian] Matematyka: Dział 2", date(2026, 10, 22)),
        ("a2", "Ola: [Wycieczka] Do Krakowa", date(2026, 10, 23)),
        ("f6-2026-11-02", "Ola: Dzień wolny: Wszystkich Świętych", date(2026, 11, 2)),
    ]
    only_tests = sync_items(
        _data(), today=_TODAY, days=30, categories=["sprawdz"], free_days=False
    )
    assert [i.key for i in only_tests] == ["a1"]
    short = sync_items(_data(), today=_TODAY, days=3, categories=[], free_days=True)
    assert [i.key for i in short] == ["a1"]


def test_sync_items_with_emoji() -> None:
    items = sync_items(
        _data(), today=_TODAY, days=30, categories=[], free_days=False, emoji=True
    )
    assert items[0].summary == "📝 [Sprawdzian] Matematyka: Dział 2"
    assert items[1].summary == "🚌 [Wycieczka] Do Krakowa"


# --- async_sync_calendar ---------------------------------------------------


class _FakeCalendar:
    def __init__(self, features: CalendarEntityFeature) -> None:
        self.supported_features = features
        self.events: dict[str, CalendarEvent] = {}
        self._uids = itertools.count(1)

    def add(self, event: CalendarEvent) -> str:
        uid = f"u{next(self._uids)}"
        self.events[uid] = replace(event, uid=uid)
        return uid

    async def async_get_events(self, hass, start: datetime, end: datetime) -> list[CalendarEvent]:
        return [
            event
            for event in self.events.values()
            if event.start < end.date() and event.end > start.date()
        ]

    async def async_create_event(self, **kwargs: Any) -> None:
        self.add(
            CalendarEvent(
                start=kwargs["dtstart"], end=kwargs["dtend"], summary=kwargs["summary"],
                description=kwargs.get("description"),
            )
        )

    async def async_update_event(self, uid: str, event: dict[str, Any], **_: Any) -> None:
        self.events[uid] = CalendarEvent(
            start=event["dtstart"], end=event["dtend"], summary=event["summary"],
            description=event.get("description"), uid=uid,
        )

    async def async_delete_event(self, uid: str, **_: Any) -> None:
        del self.events[uid]


class _FakeComponent:
    def __init__(self, entity: _FakeCalendar) -> None:
        self.entity = entity

    def get_entity(self, entity_id: str) -> _FakeCalendar | None:
        return self.entity if entity_id == "calendar.family" else None


_ALL = (
    CalendarEntityFeature.CREATE_EVENT
    | CalendarEntityFeature.UPDATE_EVENT
    | CalendarEntityFeature.DELETE_EVENT
)


async def _sync(hass, calendar: _FakeCalendar, data: LibrusData, **kw: Any) -> dict[str, Any]:
    hass.data["calendar"] = _FakeComponent(calendar)
    items = sync_items(data, today=_TODAY, days=30, categories=[], free_days=True)
    return await async_sync_calendar(
        hass, entry_id=_ENTRY, target_entity_id="calendar.family", items=items,
        today=_TODAY, days=30, **kw,
    )


async def test_sync_creates_then_keeps_up_with_librus(hass) -> None:
    calendar = _FakeCalendar(_ALL)
    # Someone's own event on the same day - never touched.
    calendar.add(CalendarEvent(start=date(2026, 10, 22), end=date(2026, 10, 23), summary="Dentysta"))

    first = await _sync(hass, calendar, _data())
    assert first["created"] == 3
    tag = f"[librus-sync:{student_tag(_ENTRY)}:a1]"
    assert any(tag in (e.description or "") for e in calendar.events.values())

    # Nothing changed - nothing to do.
    again = await _sync(hass, calendar, _data())
    assert again["created"] == 0 and again["unchanged"] == 3

    # The test moved to Friday, the trip was cancelled.
    moved = _data(
        homeworks=[
            HomeworkEventData(
                id=1, date="2026-10-30", content="Dział 2", category_id=1,
                subject_id=10, time_from=None,
            )
        ]
    )
    result = await _sync(hass, calendar, moved)
    assert result["updated"] == 1 and result["deleted"] == 1
    summaries = sorted((e.summary, e.start) for e in calendar.events.values())
    assert summaries == [
        ("Dentysta", date(2026, 10, 22)),
        ("Dzień wolny: Wszystkich Świętych", date(2026, 11, 2)),
        ("[Sprawdzian] Matematyka: Dział 2", date(2026, 10, 30)),
    ]


async def test_sync_leaves_another_childs_events_alone(hass) -> None:
    calendar = _FakeCalendar(_ALL)
    calendar.add(
        CalendarEvent(
            start=date(2026, 10, 22), end=date(2026, 10, 23), summary="Brat: Sprawdzian",
            description="[librus-sync:abcdef:a99]",
        )
    )
    result = await _sync(hass, calendar, _data())
    assert result["deleted"] == 0
    assert any(e.summary == "Brat: Sprawdzian" for e in calendar.events.values())


async def test_sync_takes_over_copies_made_by_the_old_blueprint(hass) -> None:
    calendar = _FakeCalendar(_ALL)
    uid = calendar.add(
        CalendarEvent(
            start=date(2026, 10, 22), end=date(2026, 10, 23),
            summary="[Sprawdzian] Matematyka: Dział 2", description="Dział 2",
        )
    )
    result = await _sync(hass, calendar, _data())
    assert result["adopted"] == 1 and result["created"] == 2
    assert "[librus-sync:" in (calendar.events[uid].description or "")


async def test_sync_takes_over_old_copies_without_editing(hass) -> None:
    """Google Calendar only allows adding and deleting events (seen live,
    supported_features 3): an old copy is deleted and added again with the
    tag, so it can be moved or removed later."""
    calendar = _FakeCalendar(CalendarEntityFeature.CREATE_EVENT | CalendarEntityFeature.DELETE_EVENT)
    uid = calendar.add(
        CalendarEvent(
            start=date(2026, 10, 22), end=date(2026, 10, 23),
            summary="[Sprawdzian] Matematyka: Dział 2", description="Dział 2",
        )
    )
    result = await _sync(hass, calendar, _data())
    assert result["adopted"] == 1 and result["created"] == 2
    assert uid not in calendar.events
    [copy] = [e for e in calendar.events.values() if e.summary.startswith("[Sprawdzian]")]
    assert "[librus-sync:" in (copy.description or "")


async def test_sync_without_delete_support_only_adds(hass) -> None:
    calendar = _FakeCalendar(CalendarEntityFeature.CREATE_EVENT)
    await _sync(hass, calendar, _data())
    result = await _sync(hass, calendar, _data(homeworks=[]))
    assert result["deleted"] == 0 and result["not_removable"] == 2
    assert len(calendar.events) == 3


async def test_sync_dry_run_changes_nothing(hass) -> None:
    calendar = _FakeCalendar(_ALL)
    result = await _sync(hass, calendar, _data(), dry_run=True)
    assert result["created"] == 3
    assert [p["action"] for p in result["planned"]] == ["create"] * 3
    assert calendar.events == {}


async def test_sync_ignores_past_events(hass) -> None:
    calendar = _FakeCalendar(_ALL)
    past = (_TODAY - timedelta(days=3))
    calendar.add(
        CalendarEvent(
            start=past, end=past + timedelta(days=1), summary="Stary sprawdzian",
            description=f"[librus-sync:{student_tag(_ENTRY)}:a3]",
        )
    )
    result = await _sync(hass, calendar, _data())
    assert result["deleted"] == 0
    assert any(e.summary == "Stary sprawdzian" for e in calendar.events.values())


# --- Through Home Assistant -------------------------------------------------

_TOMORROW = (dt_util.now().date() + timedelta(days=1)).isoformat()


def _device(hass, entry) -> dr.DeviceEntry:
    registry = dr.async_get(hass)
    return dr.async_entries_for_config_entry(registry, entry.entry_id)[0]


def _calendar(hass, entry, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("calendar", DOMAIN, f"{entry.entry_id}_{key}")


def _client_with_lesson_and_test():
    return build_mock_client(
        async_get_timetable={
            "Timetable": {
                _TOMORROW: [
                    [
                        {
                            "LessonNo": "1",
                            "HourFrom": "08:00",
                            "HourTo": "08:45",
                            "Subject": {"Id": "10"},
                            "Teacher": {"Id": "1"},
                            "Classroom": {"Id": "1"},
                            "IsCanceled": False,
                            "IsSubstitutionClass": False,
                        }
                    ]
                ]
            }
        },
        async_get_homeworks={
            "HomeWorks": [
                {"Id": 1, "Content": "Dział 2", "Date": _TOMORROW, "Category": {"Id": 1}, "Subject": {"Id": 10}}
            ]
        },
        async_get_homework_categories={"Categories": [{"Id": 1, "Name": "Sprawdzian"}]},
        async_get_subjects={"Subjects": [{"Id": 10, "Name": "Matematyka"}]},
    )


async def _summaries(hass, entity_id: str) -> list[str]:
    start = dt_util.start_of_local_day()
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": entity_id,
            "start_date_time": start,
            "end_date_time": start + timedelta(days=3),
        },
        blocking=True,
        return_response=True,
    )
    return [event["summary"] for event in response[entity_id]["events"]]


async def test_calendar_titles_without_and_with_emoji(hass) -> None:
    entry = await setup_integration(hass, _client_with_lesson_and_test())
    assert await _summaries(hass, _calendar(hass, entry, "timetable")) == ["Matematyka"]
    assert await _summaries(hass, _calendar(hass, entry, "agenda")) == [
        "[Sprawdzian] Matematyka: Dział 2"
    ]


async def test_calendar_titles_with_emoji_option(hass) -> None:
    entry = await setup_integration(
        hass, _client_with_lesson_and_test(), options={"calendar_emoji": True}
    )
    assert await _summaries(hass, _calendar(hass, entry, "timetable")) == ["📐 Matematyka"]
    assert await _summaries(hass, _calendar(hass, entry, "agenda")) == [
        "📝 [Sprawdzian] Matematyka: Dział 2"
    ]


async def test_absences_calendar(hass) -> None:
    yesterday = (dt_util.now().date() - timedelta(days=1)).isoformat()
    client = build_mock_client(
        async_get_attendances={
            "Attendances": [
                {"Id": 1, "Date": yesterday, "LessonNo": 1, "Semester": 1, "Type": {"Id": 1}},
                {"Id": 2, "Date": yesterday, "LessonNo": 2, "Semester": 1, "Type": {"Id": 1}},
                {"Id": 3, "Date": yesterday, "LessonNo": 3, "Semester": 1, "Type": {"Id": 100}},
            ]
        },
        async_get_attendance_types={
            "Types": [
                {"Id": 100, "Name": "Obecność", "IsPresenceKind": True},
                {"Id": 1, "Name": "Nieobecność", "IsPresenceKind": False},
            ]
        },
    )
    entry = await setup_integration(hass, client)
    entity_id = _calendar(hass, entry, "absences")
    assert entity_id is not None
    start = dt_util.start_of_local_day() - timedelta(days=2)
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": entity_id, "start_date_time": start, "end_date_time": start + timedelta(days=3)},
        blocking=True,
        return_response=True,
    )
    events = response[entity_id]["events"]
    assert [e["summary"] for e in events] == ["Nieobecność nieusprawiedliwiona (2 lekcje)"]
    assert events[0]["start"] == yesterday


async def test_device_model_is_the_class(hass) -> None:
    entry = await setup_integration(hass, build_mock_client())
    device = _device(hass, entry)
    assert device is not None
    assert device.model == "Klasa 7d"


async def test_sync_calendar_action(hass) -> None:
    entry = await setup_integration(hass, _client_with_lesson_and_test())
    calendar = _FakeCalendar(_ALL)
    device = _device(hass, entry)
    real = hass.data["calendar"]
    hass.data["calendar"] = _FakeComponent(calendar)
    try:
        result = await hass.services.async_call(
            DOMAIN,
            "sync_calendar",
            {
                "device_id": device.id,
                "target_calendar": "calendar.family",
                "categories": "sprawdzian",
                "free_days": False,
                "prefix": "Ola:",
            },
            blocking=True,
            return_response=True,
        )
    finally:
        hass.data["calendar"] = real
    assert result["created"] == 1
    [event] = calendar.events.values()
    assert event.summary == "Ola: [Sprawdzian] Matematyka: Dział 2"
