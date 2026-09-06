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
from .librus_api.models import (
    AttendanceTypeData,
    BehaviourGradeData,
    GradeCategoryData,
    GradeData,
    LibrusData,
)


def _parse_grade_value(value: str) -> float | None:
    """Convert a Librus grade string ("5+", "4-", "3", "bz"...) to a number.

    The "+"/"-" modifiers (+0.5 / -0.25) follow the convention used by most
    third-party Polish gradebook average calculators. CONFIRMED live
    (2026-09-05) via the `Grades/Types` reference endpoint that every
    non-numeric value Librus actually uses (`bz`, `np`, `nk`, `uł`, `nł`,
    `zl`, `nz`, `zw`, `uc`, `nu`, bare `+`/`-`) correctly falls through to
    returning None here and is excluded from the average - the numeric
    +/- MODIFIER convention itself is still a third-party inference, not
    something Librus documents, since no real numeric grade has been
    issued on the test account yet to check the exact value it produces.
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
            LibrusUnreadMessagesSensor(coordinator, entry),
            LibrusSchoolSensor(coordinator, entry),
            LibrusClassSensor(coordinator, entry),
            LibrusHomeworkAssignmentsSensor(coordinator, entry),
            LibrusBehaviourGradeSensor(coordinator, entry),
            LibrusDescriptiveGradesSensor(coordinator, entry),
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
            "latest_grade_comments": latest.comments if latest else [],
            "grade_count": count,
            "proposed_semester_grade": proposed.value if proposed else None,
            "final_grade": final.value if final else None,
        }


def _attendance_type(data: LibrusData, type_id: int | None) -> AttendanceTypeData | None:
    return data.attendance_types.get(type_id) if type_id is not None else None


class LibrusAttendanceSensor(LibrusSensorBase):
    """Count of real absences - excludes "present"/"late"/"excused" marks.

    CONFIRMED live: the overwhelming majority of attendance records are
    ordinary "Obecność" (present) marks (`IsPresenceKind: true`), so a raw
    total-record count mostly just tracks how many lessons happened, not
    anything a parent cares about. This counts only types the school itself
    classifies as NOT a presence kind (real absences, excused or not); the
    full breakdown (including presence marks) is still in attributes.
    """

    _attr_translation_key = "attendance"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:calendar-remove"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "attendance")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        data = self.coordinator.data
        return sum(
            1
            for a in data.attendances
            if (t := _attendance_type(data, a.type_id)) is not None and not t.is_presence_kind
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        data = self.coordinator.data
        breakdown: dict[str, int] = {}
        for attendance in data.attendances:
            attendance_type = _attendance_type(data, attendance.type_id)
            name = attendance_type.name if attendance_type is not None else str(attendance.type_id)
            breakdown[name] = breakdown.get(name, 0) + 1
        return {"breakdown": breakdown, "total_records": len(data.attendances)}


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
    """Count of school notices ("ogłoszenia") not yet marked read, with a
    recent-items attribute (subject/content preview/dates) matching the
    Behaviour notices and Unread messages sensors' pattern."""

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
        return {
            "recent": [
                {
                    "subject": n.subject,
                    "content": n.content[:200],
                    "start_date": n.start_date,
                    "end_date": n.end_date,
                    "creation_date": n.creation_date,
                }
                for n in self._unread()[:10]
            ]
        }


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
        categories = self.coordinator.data.note_categories
        return {
            "recent": [
                {
                    "date": n.date,
                    "sentiment": n.sentiment,
                    "category": categories.get(n.category_id) if n.category_id else None,
                    "text": n.text[:200],
                }
                for n in recent
            ]
        }


class LibrusHomeworkAssignmentsSensor(LibrusSensorBase):
    """Count of real homework assignments ("zadania domowe") - distinct
    from the Agenda calendar's general `HomeWorks` feed (tests/trips/etc.
    too). Fields CONFIRMED via szkolny-android's reference parser
    (2026-09-06), but never seen populated - the test account's
    `HomeWorkAssignments` endpoint has always been empty."""

    _attr_translation_key = "homework_assignments"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:notebook-edit-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "homework_assignments")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return len(self.coordinator.data.homework_assignments)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        teachers = self.coordinator.data.teachers
        recent = sorted(
            (a for a in self.coordinator.data.homework_assignments if a.due_date),
            key=lambda a: a.due_date,
        )[:10]
        return {
            "recent": [
                {
                    "topic": a.topic,
                    "text": a.text[:200],
                    "due_date": a.due_date,
                    "date": a.date,
                    "teacher": teachers.get(a.teacher_id) if a.teacher_id else None,
                }
                for a in recent
            ]
        }


class LibrusBehaviourGradeSensor(LibrusSensorBase):
    """A formal "ocena zachowania" (behaviour grade) - distinct from the
    Behaviour notices sensor above ("uwagi", free-text remarks). State is
    the most recent grade's short name (e.g. "wz" for "wzorowe") if any
    exist. Fields CONFIRMED via szkolny-android's reference parser
    (2026-09-06), but never seen populated."""

    _attr_translation_key = "behaviour_grade"
    _attr_icon = "mdi:medal-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "behaviour_grade")

    def _latest(self) -> BehaviourGradeData | None:
        if self.coordinator.data is None:
            return None
        graded = [g for g in self.coordinator.data.behaviour_grades if g.add_date]
        if not graded:
            return None
        return max(graded, key=lambda g: g.add_date)

    @property
    def native_value(self) -> str | None:
        latest = self._latest()
        return latest.short_name if latest else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        categories = self.coordinator.data.behaviour_grade_categories
        recent = sorted(
            (g for g in self.coordinator.data.behaviour_grades if g.add_date),
            key=lambda g: g.add_date,
            reverse=True,
        )[:5]
        return {
            "recent": [
                {
                    "short_name": g.short_name,
                    "value": g.value,
                    "category": categories.get(g.category_id) if g.category_id else None,
                    "date": g.add_date,
                    "text": g.text[:200],
                    "comments": g.comments,
                }
                for g in recent
            ]
        }


class LibrusDescriptiveGradesSensor(LibrusSensorBase):
    """An alternate, non-numeric grading system - CONFIRMED enabled for
    this school (via `Units`), unlike `PointGrades`. Fields CONFIRMED via
    szkolny-android's reference parser (2026-09-06), but never seen
    populated."""

    _attr_translation_key = "descriptive_grades"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:text-box-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "descriptive_grades")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return len(self.coordinator.data.descriptive_grades)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        subjects = self.coordinator.data.subjects
        recent = sorted(
            (g for g in self.coordinator.data.descriptive_grades if g.add_date),
            key=lambda g: g.add_date,
            reverse=True,
        )[:5]
        return {
            "recent": [
                {
                    "subject": subjects.get(g.subject_id) if g.subject_id else None,
                    "value": g.value,
                    "skill_id": g.skill_id,
                    "category_id": g.category_id,
                    "date": g.add_date,
                }
                for g in recent
            ]
        }


class LibrusUnreadMessagesSensor(LibrusSensorBase):
    """Unread count in the main Wiadomości inbox, with a preview list.

    Reading this sensor never marks anything read in real Librus - the
    coordinator only ever calls the message LIST/count endpoints, never a
    single-message detail endpoint (see LibrusApiClient's Wiadomości
    methods). `unknown` if this Librus install doesn't have the messages
    module enabled at all (`messages_available=False`), rather than 0 -
    those are different situations and shouldn't look the same.
    """

    _attr_translation_key = "unread_messages"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:email-outline"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "unread_messages")

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self.coordinator.data.messages_available
        )

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.unread_message_count

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        return {
            "mailbox_breakdown": dict(self.coordinator.data.unread_messages_by_mailbox),
            "recent": [
                {
                    "sender": m.sender_name,
                    "topic": m.topic,
                    "content": m.content[:200],
                    "date": m.send_date,
                    "unread": m.read_date is None,
                    "has_attachment": m.has_attachment,
                }
                for m in self.coordinator.data.messages[:10]
            ]
        }


class LibrusSchoolSensor(LibrusSensorBase):
    """The student's school - state is the school name, attributes carry
    address/contact/head-teacher/bell-schedule details. Near-static
    (refreshed on the same 24h cadence as subjects/teachers/classrooms)."""

    _attr_translation_key = "school"
    _attr_icon = "mdi:school"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "school")

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None or self.coordinator.data.school is None:
            return None
        return self.coordinator.data.school.name or None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None or self.coordinator.data.school is None:
            return None
        school = self.coordinator.data.school
        return {
            "town": school.town,
            "street": school.street,
            "building_number": school.building_number,
            "post_code": school.post_code,
            "head_teacher": school.head_teacher_name,
            "email": school.email,
            "phone_number": school.phone_number,
        }


class LibrusClassSensor(LibrusSensorBase):
    """The student's class - state is the short class name (e.g. "7d"),
    attributes carry the homeroom teacher and semester boundary dates."""

    _attr_translation_key = "school_class"
    _attr_icon = "mdi:google-classroom"

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, entry: LibrusConfigEntry) -> None:
        super().__init__(coordinator, entry, "school_class")

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None or self.coordinator.data.school_class is None:
            return None
        return self.coordinator.data.school_class.display_name or None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None or self.coordinator.data.school_class is None:
            return None
        data = self.coordinator.data
        cls = data.school_class
        tutor = data.teachers.get(cls.tutor_id) if cls.tutor_id is not None else None
        return {
            "homeroom_teacher": tutor,
            "school_year_start": cls.begin_school_year,
            "first_semester_end": cls.end_first_semester,
            "school_year_end": cls.end_school_year,
        }
