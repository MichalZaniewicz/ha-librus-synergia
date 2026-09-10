"""Typed shapes for parsed Librus API data.

Field names come from szkolny-eu/szkolny-android's source reading the same
`api.librus.pl/2.0` endpoints this client calls. Parsing is defensive
(missing keys default sensibly) because Librus doesn't publish a schema and
per-school variations are known to exist upstream (see RustySnek/librus-apix's
README: "some schools have different librus setups which may cause
errors/warnings").
"""

from __future__ import annotations

import re
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

    `positive`'s enum meaning is CONFIRMED (2026-09-06) via
    szkolny-eu/szkolny-android's actual `LibrusApiNotices.kt` parser (not a
    live populated example - the test account has zero notes - but the
    reference implementation's own condition handling Librus's real API is
    strong enough evidence): `0` = negative, `1` = positive, anything else
    (`2`, or absent) = neutral. Kept as the raw int for diagnostics; use
    `sentiment` for the resolved label.
    """

    id: int
    text: str
    category_id: int | None
    teacher_id: int | None
    date: str | None
    positive: int | None

    @property
    def sentiment(self) -> str | None:
        """Resolved `positive` label - `None` only when `positive` itself
        is `None` (no value at all), distinct from an explicit neutral."""
        if self.positive is None:
            return None
        if self.positive == 0:
            return "negative"
        if self.positive == 1:
            return "positive"
        return "neutral"


@dataclass(slots=True)
class AttendanceData:
    id: int
    lesson_id: int | None
    lesson_no: int | None
    date: str | None
    semester: int | None
    type_id: int | None


_EXCUSED_ABSENCE_NAME_RE = re.compile(r"uspr\.?", re.IGNORECASE)


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

    @property
    def is_excused_absence(self) -> bool:
        """Best-effort: Librus has no explicit "excused" flag
        (`IsPresenceKind` only says present/not), so a non-presence type
        whose name contains "uspr." (skrót od "usprawiedliwiona") is taken
        as an excused absence. Shared by the sensor layer and the
        new-absence event so both classify the same way."""
        return not self.is_presence_kind and _EXCUSED_ABSENCE_NAME_RE.search(self.name) is not None


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
    """A Wiadomości (private message) preview from a mailbox's LIST
    endpoint.

    CONFIRMED live (2026-09-06): `content` here is Librus's own TRUNCATED
    preview (base64-encoded in the raw response, decoded to plain text) -
    not the full body, despite an earlier session's finding to the
    contrary (that finding held for a short message that happened to fit
    within the truncation length; a longer real message exposed the
    truncation). The full body needs the `get_message` service
    (`services.py`), which is a SEPARATE, deliberate code path from the
    routine polling this dataclass feeds - CONFIRMED live that fetching a
    single message's detail marks it read server-side, while listing
    (what populates this dataclass) does not (`readDate` stayed `null`
    for a genuinely unread message across repeated list fetches).

    `mailbox` records which mailbox this came from ("inbox",
    "substitutions", "alerts", ...) - needed so a card can pass the right
    value back to the `get_message` service.
    """

    id: str
    sender_name: str
    topic: str
    content: str
    send_date: str | None
    read_date: str | None
    has_attachment: bool
    mailbox: str = "inbox"


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
class HomeworkAssignmentData:
    """A real homework assignment ("zadanie domowe") - distinct from the
    general agenda feed (`HomeworkEventData`, from `HomeWorks`), which
    also covers tests/trips/etc. Fields CONFIRMED (2026-09-06) via
    szkolny-eu/szkolny-android's `LibrusApiHomework.kt`, but never seen
    populated (empty on the test account) - unlike `HomeworkEventData`,
    the reference parser shows NO `Subject` field here."""

    id: int
    topic: str
    text: str
    teacher_id: int | None
    date: str | None
    due_date: str | None


@dataclass(slots=True)
class BehaviourGradeData:
    """A formal "ocena zachowania" (behaviour grade) - distinct from
    `NoteData` ("uwagi", free-text remarks). Fields CONFIRMED (2026-09-06)
    via szkolny-eu/szkolny-android's `LibrusApiBehaviourGrades.kt`, but
    never seen populated (empty on the test account)."""

    id: int
    value: float | None
    short_name: str
    semester: int | None
    category_id: int | None
    teacher_id: int | None
    add_date: str | None
    text: str
    comments: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DescriptiveGradeData:
    """An alternate, non-numeric grading system - CONFIRMED enabled for
    this school via the `Units` endpoint (unlike `PointGrades`, which is
    disabled here). Fields CONFIRMED (2026-09-06) via szkolny-eu/
    szkolny-android's `LibrusApiDescriptiveGrades.kt`, but never seen
    populated (empty on the test account). `skill_id`/`category_id` are
    kept raw - their own name-lookup endpoints weren't probed yet."""

    id: int
    subject_id: int | None
    value: str
    skill_id: int | None
    category_id: int | None
    add_date: str | None


@dataclass(slots=True)
class ParentTeacherConferenceData:
    """A scheduled parent-teacher meeting ("wywiadówka"/"zebranie").
    Fields CONFIRMED (2026-09-06) via szkolny-eu/szkolny-android's
    `LibrusApiPtMeetings.kt`. CONFIRMED live (separately) that this kind of
    meeting already surfaces via the general `HomeWorks` agenda feed too
    (see `_homework_to_event`) - this is a defensive extra merge into the
    Agenda calendar in case one exists here without a `HomeWorks`
    counterpart, not the primary source. Never seen populated on this
    account either way."""

    id: int
    topic: str
    teacher_id: int | None
    date: str | None
    time: str | None


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
    unread_messages_by_mailbox: dict[str, int] = field(default_factory=dict)
    messages: list[MessageData] = field(default_factory=list)
    school: SchoolData | None = None
    school_class: ClassData | None = None
    free_days: list[FreeDayData] = field(default_factory=list)
    homework_categories: dict[int, str] = field(default_factory=dict)
    note_categories: dict[int, str] = field(default_factory=dict)
    behaviour_grade_categories: dict[int, str] = field(default_factory=dict)
    homework_assignments: list[HomeworkAssignmentData] = field(default_factory=list)
    behaviour_grades: list[BehaviourGradeData] = field(default_factory=list)
    descriptive_grades: list[DescriptiveGradeData] = field(default_factory=list)
    parent_teacher_conferences: list[ParentTeacherConferenceData] = field(default_factory=list)
    # Full message CONTENT for a couple of the most actionable secondary
    # mailboxes (unlike unread_messages_by_mailbox above, which only ever
    # carries counts for every mailbox) - "substitutions" (zastępstwa) and
    # "alerts" (alerty) are the two a parent is most likely to want to
    # actually read, not just know a count for.
    substitution_messages: list[MessageData] = field(default_factory=list)
    alert_messages: list[MessageData] = field(default_factory=list)
    justification_messages: list[MessageData] = field(default_factory=list)
