"""Typed shapes for parsed Librus API data.

Field names come from szkolny-eu/szkolny-android's source reading the same
`api.librus.pl/2.0` endpoints this client calls. Parsing is defensive
(missing keys default sensibly) because Librus doesn't publish a schema and
per-school variations are known to exist upstream (see RustySnek/librus-apix's
README: "some schools have different librus setups which may cause
errors/warnings").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(slots=True)
class MeData:
    account_id: int | None
    first_name: str
    last_name: str

    @property
    def display_name(self) -> str:
        name = f"{self.first_name} {self.last_name}".strip()
        return name or "Uczeń"


@dataclass(slots=True)
class GradeCategoryData:
    id: int
    name: str
    count_to_average: bool
    weight: int


@dataclass(slots=True)
class GradeData:
    id: int
    value: str
    category_id: int | None
    subject_id: int | None
    semester: int | None
    add_date: str | None
    is_semester_proposition: bool
    is_final_proposition: bool
    comments: list[str] = field(default_factory=list)


@dataclass(slots=True)
class NoteData:
    """A behaviour notice ("uwaga").

    `positive`'s exact enum meaning (which of 0/1/2 is positive/neutral/
    negative) is UNVERIFIED - see manual_smoke_test.py. Kept as the raw int
    until confirmed against a live account.
    """

    id: int
    text: str
    category_id: int | None
    teacher_id: int | None
    date: str | None
    positive: int | None


@dataclass(slots=True)
class AttendanceData:
    id: int
    lesson_id: int | None
    lesson_no: int | None
    date: str | None
    semester: int | None
    type_id: int | None


@dataclass(slots=True)
class AttendanceTypeData:
    """CONFIRMED live: `IsPresenceKind` is real and meaningful - e.g. Id 100
    "Obecność" (present) and Id 2 "Spóźnienie" (late) are both presence-kind
    (the student was there), while Id 1 "Nieobecność" (absence) and Id 3
    "Nieobecność uspr." (excused absence) are not. This is what lets the
    attendance sensor show real absences as its primary state instead of a
    raw count of every record (which is mostly ordinary "present" marks)."""

    id: int
    name: str
    is_presence_kind: bool


@dataclass(slots=True)
class LessonData:
    lesson_no: int | None
    hour_from: str | None
    hour_to: str | None
    subject_id: int | None
    teacher_id: int | None
    classroom_id: int | None
    is_canceled: bool
    is_substitution: bool


@dataclass(slots=True)
class HomeworkEventData:
    """An agenda/event entry from the `HomeWorks` endpoint - despite the
    name, this is Librus's general events feed (tests, trips, homework),
    not the separate `HomeWorkAssignments` endpoint. CONFIRMED live
    (2026-09-05) that `HomeWorkAssignments` is real and reachable (not
    404/error) - it simply returned no entries for this account/week, so
    it's not wired into `LibrusData` yet. Revisit once real assignments
    exist to confirm its field names before trusting a parser for it."""

    id: int
    date: str | None
    content: str
    category_id: int | None
    subject_id: int | None
    time_from: str | None


@dataclass(slots=True)
class SchoolNoticeData:
    # CONFIRMED live: unlike every other endpoint, ids here are strings
    # (e.g. "LID-NBOARD-NOTICE-9093-..."), not ints.
    id: str
    subject: str
    content: str
    start_date: str | None
    end_date: str | None
    creation_date: str | None
    was_read: bool = False


@dataclass(slots=True)
class LuckyNumberData:
    day: str | None
    number: int


@dataclass(slots=True)
class MessageData:
    """A Wiadomości (private message) preview.

    CONFIRMED live (2026-09-05): the list endpoint already returns the full
    `content` (base64-encoded in the raw response, decoded to plain text
    here) - there is no need to ever call a per-message detail endpoint,
    which is what this integration deliberately avoids (see
    `LibrusApiClient`'s module-level comment on the Wiadomości methods):
    fetching a single message's detail almost certainly marks it read
    server-side, while listing does not (`readDate` stayed `null` for a
    genuinely unread message across repeated list fetches in testing).
    """

    id: str
    sender_name: str
    topic: str
    content: str
    send_date: str | None
    read_date: str | None
    has_attachment: bool


@dataclass(slots=True)
class SchoolData:
    """CONFIRMED live via the `Schools` endpoint."""

    name: str
    town: str | None
    street: str | None
    building_number: str | None
    post_code: str | None
    head_teacher_name: str | None
    email: str | None
    phone_number: str | None


@dataclass(slots=True)
class ClassData:
    """CONFIRMED live via the `Classes` endpoint. `symbol` combined with
    `number` gives the usual short class name (e.g. 7 + "d" -> "7d")."""

    number: int | None
    symbol: str
    tutor_id: int | None
    begin_school_year: str | None
    end_first_semester: str | None
    end_school_year: str | None

    @property
    def display_name(self) -> str:
        if self.number is not None and self.symbol:
            return f"{self.number}{self.symbol}"
        return self.symbol or (str(self.number) if self.number is not None else "")


@dataclass(slots=True)
class FreeDayData:
    """A school- or class-wide free day/break, from `SchoolFreeDays` or
    `ClassFreeDays` (same shape, confirmed live for both - `ClassFreeDays`
    was empty at the time but the endpoint and shape are real)."""

    id: int
    name: str
    date_from: str
    date_to: str


@dataclass(slots=True)
class LibrusData:
    """Everything the coordinator fetches in one update cycle."""

    me: MeData
    grades: list[GradeData]
    grade_categories: dict[int, GradeCategoryData]
    notes: list[NoteData]
    attendances: list[AttendanceData]
    attendance_types: dict[int, AttendanceTypeData]
    timetable: dict[date, list[LessonData]]
    homeworks: list[HomeworkEventData]
    school_notices: list[SchoolNoticeData]
    lucky_number: LuckyNumberData | None
    subjects: dict[int, str]
    teachers: dict[int, str]
    classrooms: dict[int, str]
    messages_available: bool = False
    unread_message_count: int = 0
    messages: list[MessageData] = field(default_factory=list)
    school: SchoolData | None = None
    school_class: ClassData | None = None
    free_days: list[FreeDayData] = field(default_factory=list)
    homework_categories: dict[int, str] = field(default_factory=dict)
