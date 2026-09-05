"""Sensor platform for the Librus Synergia (unofficial) integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import LibrusConfigEntry, librus_device_info
from .const import ATTR_SUBJECT_ID
from .coordinator import LibrusDataUpdateCoordinator
from .librus_api.models import GradeCategoryData, GradeData


def _parse_grade_value(value: str) -> float | None:
    """Convert a Librus grade string ("5+", "4-", "3", "bz"...) to a number.

    The "+"/"-" modifiers (+0.5 / -0.25) follow the convention used by most
    third-party Polish gradebook average calculators; non-numeric marks
    (unprepared, absent, etc.) return None and are excluded from any
    average. UNVERIFIED against a real account's actual grade strings - see
    scripts/manual_smoke_test.py.
    """
    value = value.strip()
    if not value:
        return None
    modifier = 0.0
    if value.endswith("+"):
        modifier = 0.5
        value = value[:-1]
    elif value.endswith("-"):
        modifier = -0.25
        value = value[:-1]
    try:
        return float(value.replace(",", ".")) + modifier
    except ValueError:
        return None


def _calculate_average(
    grades: list[GradeData],
    categories: dict[int, GradeCategoryData],
    *,
    subject_id: int | None = None,
) -> float | None:
    """Weighted grade average, excluding semester/final proposition entries
    and categories marked as not counting toward the average."""
    weighted_sum = 0.0
    weight_total = 0.0
    for grade in grades:
        if grade.is_semester_proposition or grade.is_final_proposition:
            continue
        if subject_id is not None and grade.subject_id != subject_id:
            continue
        category = categories.get(grade.category_id) if grade.category_id is not None else None
        if category is not None and not category.count_to_average:
            continue
        numeric = _parse_grade_value(grade.value)
        if numeric is None:
            continue
        weight = category.weight if category is not None else 1
        weighted_sum += numeric * weight
        weight_total += weight
    if weight_total <= 0:
        return None
    return round(weighted_sum / weight_total, 2)


def _latest_grade(grades: list[GradeData], *, subject_id: int | None = None) -> GradeData | None:
    candidates = [
        g
        for g in grades
        if not g.is_semester_proposition
        and not g.is_final_proposition
        and (subject_id is None or g.subject_id == subject_id)
        and g.add_date
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda g: g.add_date)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LibrusConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Librus Synergia sensors from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            LibrusOverallAverageSensor(coordinator, entry),
            LibrusAttendanceSensor(coordinator, entry),
            LibrusLuckyNumberSensor(coordinator, entry),
            LibrusUnreadAnnouncementsSensor(coordinator, entry),
            LibrusBehaviourNoticesSensor(coordinator, entry),
        ]
    )

    # Subjects are only known from live account data - discover new ones as
    # the coordinator sees them and add an average sensor per subject.
    known_subject_ids: set[int] = set()

    def _add_new_subjects() -> None:
        data = coordinator.data
        if data is None:
            return
        # Fall back to subject ids seen on grades even if the (unverified)
        # Subjects lookup hasn't resolved a name for it yet.
        seen_ids = set(data.subjects) | {
            g.subject_id for g in data.grades if g.subject_id is not None
        }
        new_ids = seen_ids - known_subject_ids
        if not new_ids:
            return
        known_subject_ids.update(new_ids)
        async_add_entities(
            LibrusSubjectAverageSensor(coordinator, entry, subject_id) for subject_id in new_ids
        )

    _add_new_subjects()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_subjects))


class LibrusSensorBase(CoordinatorEntity[LibrusDataUpdateCoordinator], SensorEntity):
    """Common bits for every Librus Synergia sensor."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry, key: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = librus_device_info(entry)


class LibrusOverallAverageSensor(LibrusSensorBase):
    """Weighted average across every subject."""

    _attr_translation_key = "overall_average"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "overall_average")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return _calculate_average(self.coordinator.data.grades, self.coordinator.data.grade_categories)


class LibrusSubjectAverageSensor(LibrusSensorBase):
    """Weighted average for a single subject, discovered dynamically."""

    _attr_translation_key = "subject_average"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(
        self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry, subject_id: int
    ) -> None:
        super().__init__(coordinator, entry, f"subject_{subject_id}_average")
        self._subject_id = subject_id

    @property
    def _subject_name(self) -> str:
        if self.coordinator.data is None:
            return str(self._subject_id)
        return self.coordinator.data.subjects.get(self._subject_id, str(self._subject_id))

    @property
    def translation_placeholders(self) -> dict[str, str]:
        return {"subject": self._subject_name}

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return _calculate_average(
            self.coordinator.data.grades,
            self.coordinator.data.grade_categories,
            subject_id=self._subject_id,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        grades = self.coordinator.data.grades
        latest = _latest_grade(grades, subject_id=self._subject_id)
        proposed = next(
            (g for g in grades if g.subject_id == self._subject_id and g.is_semester_proposition),
            None,
        )
        final = next(
            (g for g in grades if g.subject_id == self._subject_id and g.is_final_proposition),
            None,
        )
        count = sum(
            1
            for g in grades
            if g.subject_id == self._subject_id
            and not g.is_semester_proposition
            and not g.is_final_proposition
        )
        return {
            ATTR_SUBJECT_ID: self._subject_id,
            "latest_grade": latest.value if latest else None,
            "latest_grade_date": latest.add_date if latest else None,
            "grade_count": count,
            "proposed_semester_grade": proposed.value if proposed else None,
            "final_grade": final.value if final else None,
        }


class LibrusAttendanceSensor(LibrusSensorBase):
    """Total recorded attendance entries, with a per-type breakdown."""

    _attr_translation_key = "attendance"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:calendar-check"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "attendance")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return len(self.coordinator.data.attendances)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        data = self.coordinator.data
        breakdown: dict[str, int] = {}
        for attendance in data.attendances:
            if attendance.type_id is None:
                continue
            name = data.attendance_types.get(attendance.type_id, str(attendance.type_id))
            breakdown[name] = breakdown.get(name, 0) + 1
        return {"breakdown": breakdown}


class LibrusLuckyNumberSensor(LibrusSensorBase):
    """Today's "szczęśliwy numerek" (lucky number)."""

    _attr_translation_key = "lucky_number"
    _attr_icon = "mdi:dice-5"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "lucky_number")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None or self.coordinator.data.lucky_number is None:
            return None
        return self.coordinator.data.lucky_number.number


class LibrusUnreadAnnouncementsSensor(LibrusSensorBase):
    """Count of school notices ("ogłoszenia") not yet marked read."""

    _attr_translation_key = "unread_announcements"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:bullhorn"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "unread_announcements")

    def _unread(self) -> list[Any]:
        if self.coordinator.data is None:
            return []
        return [n for n in self.coordinator.data.school_notices if not n.was_read]

    @property
    def native_value(self) -> int:
        return len(self._unread())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"titles": [n.subject for n in self._unread()[:10]]}


class LibrusBehaviourNoticesSensor(LibrusSensorBase):
    """Count of behaviour notices ("uwagi"), with a short recent-items list."""

    _attr_translation_key = "behaviour_notices"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "behaviour_notices")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return len(self.coordinator.data.notes)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        recent = sorted(
            (n for n in self.coordinator.data.notes if n.date),
            key=lambda n: n.date,
            reverse=True,
        )[:5]
        return {
            "recent": [
                {"date": n.date, "positive": n.positive, "text": n.text[:200]} for n in recent
            ]
        }
