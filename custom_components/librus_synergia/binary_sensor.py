"""Binary sensors: is today / tomorrow a school day, is school on right now.

Built for automations - an alarm clock that only rings on school days,
heating or a "do not disturb" mode during lessons. All from the cached
timetable (see school_day.py), re-checked at each school-day transition (a
first lesson starting, a last lesson ending, midnight) so the state flips on
time rather than at the next coordinator refresh.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from librus_synergia.models import HomeworkEventData, LibrusData

from . import LibrusConfigEntry, librus_device_info
from .coordinator import LibrusDataUpdateCoordinator
from .exam_prep import category_name, exam_kind, exam_text, is_exam
from .forecast import SubjectForecast, subject_forecasts
from .school_day import (
    SchoolDay,
    SchoolDayRefresh,
    SkipUnchangedUpdates,
    in_school,
    school_days,
)


def _day_attrs(school_day: SchoolDay | None) -> dict[str, Any]:
    if school_day is None:
        return {}
    return {
        "first_lesson_start": school_day.first_start.isoformat(),
        "last_lesson_end": school_day.last_end.isoformat(),
        "first_lesson_no": school_day.first_lesson.lesson_no,
    }


class LibrusSchoolBinarySensor(
    SchoolDayRefresh,
    SkipUnchangedUpdates,
    CoordinatorEntity[LibrusDataUpdateCoordinator],
    BinarySensorEntity,
):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LibrusDataUpdateCoordinator,
        entry: LibrusConfigEntry,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = librus_device_info(entry)

    def _school_day(self) -> SchoolDay | None:
        raise NotImplementedError

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return _day_attrs(self._school_day())


class LibrusSchoolDayTodaySensor(LibrusSchoolBinarySensor):

    def __init__(
        self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, "school_day_today")

    def _school_day(self) -> SchoolDay | None:
        days = school_days(self.coordinator.data, self.coordinator.memo_owner)
        return days.get(dt_util.now().date())

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self._school_day() is not None


class LibrusSchoolDayTomorrowSensor(LibrusSchoolBinarySensor):

    def __init__(
        self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, "school_day_tomorrow")

    def _school_day(self) -> SchoolDay | None:
        return school_days(self.coordinator.data, self.coordinator.memo_owner).get(
            dt_util.now().date() + timedelta(days=1)
        )

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self._school_day() is not None


class LibrusInSchoolSensor(LibrusSchoolBinarySensor):
    """On from the first lesson's start to the last lesson's end today,
    breaks included - by the timetable, not by where the child really is."""


    def __init__(
        self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, "in_school")

    def _school_day(self) -> SchoolDay | None:
        days = school_days(self.coordinator.data, self.coordinator.memo_owner)
        return days.get(dt_util.now().date())

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        days = school_days(self.coordinator.data, self.coordinator.memo_owner)
        return in_school(days, dt_util.now())


class LibrusGradeRiskSensor(
    SkipUnchangedUpdates, CoordinatorEntity[LibrusDataUpdateCoordinator], BinarySensorEntity
):
    """On while any subject's average points to a 1 (see forecast.py).
    Subjects whose forecast fell by a grade recently are listed too, but
    don't turn it on on their own."""

    _attr_has_entity_name = True
    _attr_translation_key = "grade_risk"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_grade_risk"
        self._attr_device_info = librus_device_info(entry)

    def _forecasts(self) -> list[SubjectForecast]:
        return subject_forecasts(
            self.coordinator.data,
            dt_util.now().date(),
            self.coordinator.grade_thresholds,
            weighted=self.coordinator.weighted_average,
            owner=self.coordinator.memo_owner,
            point_thresholds=self.coordinator.point_grade_thresholds,
        )

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return any(f.at_risk for f in self._forecasts())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        forecasts = self._forecasts()
        return {
            "subjects": [
                {
                    "subject": f.subject,
                    "average": f.average,
                    "sixes_to_next": f.sixes_to_next,
                }
                for f in forecasts
                if f.at_risk
            ],
            "declining": [f.subject for f in forecasts if f.declining],
        }


# Window of the Test within 24 hours sensor.
EXAM_SOON = timedelta(hours=24)


def _parse_hhmm(value: str | None) -> time | None:
    try:
        return datetime.strptime((value or "")[:5], "%H:%M").time()
    except ValueError:
        return None


def exam_moments(
    data: LibrusData | None, owner: Any = None
) -> list[tuple[datetime, HomeworkEventData]]:
    """When each test in the Agenda starts, soonest first: its own time if
    the entry has one, else that day's first lesson of its subject, else
    the day's first lesson, else 8:00."""
    if data is None:
        return []
    days = school_days(data, owner)
    out: list[tuple[datetime, HomeworkEventData]] = []
    for item in data.homeworks:
        try:
            day = date.fromisoformat((item.date or "")[:10])
        except ValueError:
            continue
        if not is_exam(data, item):
            continue
        at = _parse_hhmm(item.time_from)
        if at is None and item.subject_id is not None:
            starts = sorted(
                start
                for lesson in data.timetable.get(day, [])
                if not lesson.is_canceled
                and str(lesson.subject_id) == str(item.subject_id)
                and (start := _parse_hhmm(lesson.hour_from)) is not None
            )
            at = starts[0] if starts else None
        if at is not None:
            moment = dt_util.as_local(datetime.combine(day, at))
        elif day in days:
            moment = days[day].first_start
        else:
            moment = dt_util.as_local(datetime.combine(day, time(8, 0)))
        out.append((moment, item))
    out.sort(key=lambda pair: pair[0])
    return out


class LibrusExamSoonSensor(
    SkipUnchangedUpdates, CoordinatorEntity[LibrusDataUpdateCoordinator], BinarySensorEntity
):
    """On from 24 hours before a test (or a short one) until it starts -
    for a lamp at the desk or "no games tonight". Switched exactly on time
    by a timer at the next window edge, re-armed on every update."""

    _attr_has_entity_name = True
    _attr_translation_key = "exam_soon"
    _always_write = True
    _unsub_timer: CALLBACK_TYPE | None = None

    def __init__(
        self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_exam_soon"
        self._attr_device_info = librus_device_info(entry)

    def _in_window(self, now: datetime) -> list[tuple[datetime, HomeworkEventData]]:
        return [
            pair
            for pair in exam_moments(self.coordinator.data, self.coordinator.memo_owner)
            if now < pair[0] <= now + EXAM_SOON
        ]

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return bool(self._in_window(dt_util.now()))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if data is None:
            return {}
        exams = []
        for moment, item in self._in_window(dt_util.now()):
            exams.append(
                {
                    "start": moment.isoformat(),
                    "subject": data.subjects.get(item.subject_id)
                    if item.subject_id is not None
                    else None,
                    "kind": exam_kind(data, item),
                    "category": category_name(data, item),
                    "text": exam_text(data, item),
                }
            )
        return {
            "exams": exams,
            "tests": sum(1 for e in exams if e["kind"] == "test"),
            "quizzes": sum(1 for e in exams if e["kind"] == "quiz"),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_timer)
        self._arm_timer()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._arm_timer()
        super()._handle_coordinator_update()

    @callback
    def _cancel_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    @callback
    def _arm_timer(self) -> None:
        """At the next moment a test enters the 24-hour window or starts."""
        self._cancel_timer()
        if self.hass is None:
            return
        now = dt_util.now()
        edges = [
            edge
            for moment, _ in exam_moments(self.coordinator.data, self.coordinator.memo_owner)
            for edge in (moment - EXAM_SOON, moment)
            if edge > now
        ]
        if not edges:
            return
        self._unsub_timer = async_track_point_in_time(
            self.hass, self._async_edge, min(edges) + timedelta(seconds=1)
        )

    @callback
    def _async_edge(self, _now: datetime) -> None:
        self._unsub_timer = None
        self.async_write_ha_state()
        self._arm_timer()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LibrusConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            LibrusSchoolDayTodaySensor(coordinator, entry),
            LibrusSchoolDayTomorrowSensor(coordinator, entry),
            LibrusInSchoolSensor(coordinator, entry),
            LibrusGradeRiskSensor(coordinator, entry),
            LibrusExamSoonSensor(coordinator, entry),
        ]
    )
