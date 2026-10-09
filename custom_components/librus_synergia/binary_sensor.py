"""Binary sensors: is today / tomorrow a school day, is school on right now.

Built for automations - an alarm clock that only rings on school days,
heating or a "do not disturb" mode during lessons. All from the cached
timetable (see school_day.py), re-checked every minute so the state flips
on time rather than at the next coordinator refresh.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import LibrusConfigEntry, librus_device_info
from .coordinator import LibrusDataUpdateCoordinator
from .forecast import SubjectForecast, subject_forecasts
from .school_day import MinuteRefresh, SchoolDay, in_school, school_days


def _day_attrs(school_day: SchoolDay | None) -> dict[str, Any]:
    if school_day is None:
        return {}
    return {
        "first_lesson_start": school_day.first_start.isoformat(),
        "last_lesson_end": school_day.last_end.isoformat(),
        "first_lesson_no": school_day.first_lesson.lesson_no,
    }


class LibrusSchoolBinarySensor(
    MinuteRefresh, CoordinatorEntity[LibrusDataUpdateCoordinator], BinarySensorEntity
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
    _attr_icon = "mdi:school-outline"

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
    _attr_icon = "mdi:calendar-arrow-right"

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

    _attr_icon = "mdi:bag-personal-outline"

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
    CoordinatorEntity[LibrusDataUpdateCoordinator], BinarySensorEntity
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
        ]
    )
