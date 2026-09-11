"""Data update coordinator for the Librus Synergia (unofficial) integration.

Single coordinator, not split by cadence: unlike ha-suunto (live heart rate
vs. sleep/workouts genuinely differ in volatility), Librus data - grades,
attendance, timetable, announcements - all change at "a few times a day at
most" cadence, so one coordinator covers everything. The one exception (the
daily lucky number) is special-cased inline rather than given its own
coordinator, for the same reason ha-suunto special-cases its one-off VO2max
deep-scan inside its normal coordinator instead of adding new machinery for a
single edge case.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from datetime import date, datetime, timedelta
from html import unescape as html_unescape
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ANNOUNCEMENTS_ENABLED,
    CONF_BEHAVIOUR_GRADES_ENABLED,
    CONF_DESCRIPTIVE_GRADES_ENABLED,
    CONF_FREE_DAYS_ENABLED,
    CONF_MESSAGES_ENABLED,
    DEFAULT_ANNOUNCEMENTS_ENABLED,
    DEFAULT_BEHAVIOUR_GRADES_ENABLED,
    DEFAULT_DESCRIPTIVE_GRADES_ENABLED,
    DEFAULT_FREE_DAYS_ENABLED,
    DEFAULT_MESSAGES_ENABLED,
    DOMAIN,
    EVENT_NEW_ABSENCE,
    EVENT_NEW_ANNOUNCEMENT,
    EVENT_NEW_GRADE,
    EVENT_NEW_HOMEWORK,
    EVENT_NEW_MESSAGE,
    EVENT_NEW_NOTE,
    EVENT_TIMETABLE_CHANGED,
    ISSUE_OPTIONAL_ENDPOINT_DEGRADED,
    ISSUE_SCHOOL_YEAR_ROLLOVER,
    LUCKY_NUMBER_PUBLISH_HOUR,
    OPTIONAL_ENDPOINT_LABELS,
)
from .librus_api import LibrusApiClient, LibrusAuthError, LibrusError, LibrusSessionExpiredError
from .librus_api.models import (
    AttendanceData,
    AttendanceTypeData,
    BehaviourGradeData,
    ClassData,
    DescriptiveGradeData,
    FreeDayData,
    GradeCategoryData,
    GradeData,
    HomeworkAssignmentData,
    HomeworkEventData,
    LessonData,
    LibrusData,
    LuckyNumberData,
    MeData,
    MessageData,
    NoteData,
    ParentTeacherConferenceData,
    SchoolData,
    SchoolNoticeData,
)

_LOGGER = logging.getLogger(__name__)


def optional_endpoint_issue_id(entry_id: str, label: str) -> str:
    """Stable repair-issue id for one entry's one supplementary-endpoint
    degradation. Public (not underscore-prefixed) - `__init__.py::
    async_remove_entry` needs to compute the same ids to clear them when a
    config entry is deleted for good, without needing a live coordinator."""
    return f"{ISSUE_OPTIONAL_ENDPOINT_DEGRADED}_{entry_id}_{label.lower().replace('/', '_')}"


def school_year_issue_id(entry_id: str) -> str:
    """Stable repair-issue id for one entry's school-year-rollover check -
    see `optional_endpoint_issue_id`'s docstring for why this is public."""
    return f"{ISSUE_SCHOOL_YEAR_ROLLOVER}_{entry_id}"


class LibrusDataUpdateCoordinator(DataUpdateCoordinator[LibrusData]):
    """Fetches everything Librus Synergia exposes for one student."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: LibrusApiClient,
        scan_interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"Librus Synergia ({client.username})",
            update_interval=scan_interval,
            config_entry=entry,
        )
        self._client = client

        # Subjects/teachers/classrooms are near-static reference data -
        # refetched at most once a day rather than every cycle.
        self._reference_data_fetched_at: datetime | None = None
        self._cached_subjects: dict[int, str] = {}
        self._cached_teachers: dict[int, str] = {}
        self._cached_classrooms: dict[int, str] = {}
        self._cached_school: SchoolData | None = None
        self._cached_class: ClassData | None = None
        self._cached_homework_categories: dict[int, str] = {}
        self._cached_free_days: list[FreeDayData] = []
        self._cached_note_categories: dict[int, str] = {}
        self._cached_behaviour_grade_categories: dict[int, str] = {}

        # The lucky number is normally published once a day; skip refetching
        # it before LUCKY_NUMBER_PUBLISH_HOUR once today's value is cached.
        self._cached_lucky_number: LuckyNumberData | None = None
        self._lucky_number_fetched_date: date | None = None

        # Wiadomości (messages) needs a one-time-per-login bootstrap (a
        # separate session cookie on wiadomosci.librus.pl) - not every cycle,
        # and not every school has this module enabled, so failure here is
        # non-fatal (see _async_get_messages).
        self._messages_bootstrapped = False
        self._messages_available = False

        # New-item bus events. In-memory only, None = never populated (the
        # next cycle just seeds it instead of replaying history as "new" on
        # first install). A HA restart re-seeds quietly instead of persisting
        # across restarts - same tradeoff ha-suunto makes for its
        # EVENT_NEW_WORKOUT tracking, and for the same reason: a few hundred
        # small ids a year is cheap to hold, not worth a Store-backed file.
        self._known_grade_ids: set[int] | None = None
        # SchoolNotices ids are strings (e.g. "LID-NBOARD-NOTICE-..."),
        # confirmed live - unlike every other endpoint's plain int ids.
        self._known_notice_ids: set[str] | None = None
        self._known_note_ids: set[int] | None = None
        self._known_message_ids: set[str] | None = None
        self._known_homework_ids: set[int] | None = None
        self._known_absence_ids: set[int] | None = None
        # Synthetic "date|period|kind|subject" signatures for cancelled /
        # substitution lessons - not a real id from the API, just enough to
        # not re-fire EVENT_TIMETABLE_CHANGED for a disruption already seen.
        self._known_timetable_disruptions: set[str] | None = None

        # First-failure timestamp per OPTIONAL_ENDPOINT_LABELS entry - used
        # to raise a repair issue only once a supplementary endpoint has
        # failed on EVERY attempt for a week straight (not a single
        # hiccup). In-memory only, same "a HA restart just re-seeds
        # quietly" tradeoff as the new-item id sets above - a restart just
        # restarts the 7-day countdown, which is fine for something this
        # low-stakes.
        self._optional_endpoint_first_failure: dict[str, datetime] = {}

    @property
    def client(self) -> LibrusApiClient:
        """Expose the client so calendar entities can fetch arbitrary weeks
        on demand (dashboards can ask CalendarEntity.async_get_events for
        ranges outside this coordinator's current+next-week cache)."""
        return self._client

    async def async_fetch_timetable_week(self, week_start: date) -> Any:
        """Fetch one week's raw `Timetable` payload on demand, for
        `LibrusTimetableCalendar.async_get_events` serving a date range
        outside the current+next-week window this coordinator normally
        caches (e.g. a dashboard card asking for "this ISO week" while
        today is a Sunday, whose Monday has already rolled out of that
        window).

        BUG FIX (2026-09-06, found live): this on-demand path used to call
        `self.client.async_get_timetable(week_start)` directly, with none
        of `_async_update_data`'s forced-relogin-and-retry-once recovery
        for a mid-cycle session expiry. When the real Librus session died
        between the coordinator's own last successful poll and a
        dashboard's calendar REST request, the raw `LibrusSessionExpiredError`
        propagated straight out of `CalendarEntity.async_get_events` and
        crashed the whole `/api/calendars/<entity>` request with an
        unhandled 500 - confirmed live via `ha_config_get_calendar_events`
        returning exactly that 500 for a past-week range, traced to this
        exact exception in the error log. Same one-retry-only recovery as
        `_async_update_data`, just reusable outside the normal poll cycle.
        """
        assert self.config_entry is not None
        try:
            return await self._client.async_get_timetable(week_start)
        except LibrusSessionExpiredError:
            await self._client.async_ensure_session_valid(
                self.config_entry.data[CONF_PASSWORD], force=True
            )
            return await self._client.async_get_timetable(week_start)

    async def async_fetch_message(self, mailbox: str, message_id: str) -> Any:
        """Fetch one message's full body on demand, for `services.py`'s
        `get_message` service - deliberately outside the coordinator's
        normal poll cycle (see `LibrusApiClient.async_get_message`'s
        docstring for why: CONFIRMED live this marks the message read
        server-side, so it must only ever run on a user's own explicit
        action, never automatically). Same one-retry-only session-expiry
        recovery as `async_fetch_timetable_week`."""
        assert self.config_entry is not None
        try:
            return await self._client.async_get_message(mailbox, message_id)
        except LibrusSessionExpiredError:
            await self._client.async_ensure_session_valid(
                self.config_entry.data[CONF_PASSWORD], force=True
            )
            return await self._client.async_get_message(mailbox, message_id)

    async def _async_update_data(self) -> LibrusData:
        assert self.config_entry is not None
        try:
            await self._client.async_ensure_session_valid(self.config_entry.data[CONF_PASSWORD])
        except LibrusAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except LibrusError as err:
            raise UpdateFailed(str(err)) from err

        today = dt_util.now().date()
        week_start = today - timedelta(days=today.weekday())
        next_week_start = week_start + timedelta(days=7)

        try:
            core_payloads = await self._async_fetch_core_payloads(week_start, next_week_start)
        except LibrusSessionExpiredError:
            # CONFIRMED live (2026-09-05): Librus's real session lifetime can
            # run shorter than our own conservative ASSUMED_SESSION_LIFETIME_
            # SECONDS estimate - `async_ensure_session_valid` above thought
            # the session was still fresh, but a data endpoint rejected it
            # anyway. The stored password is still there for exactly this
            # case: force one fresh login and retry ONCE before ever
            # bothering the user with Home Assistant's reauth flow - never
            # retry more than once per cycle (avoid hammering Librus).
            try:
                await self._client.async_ensure_session_valid(
                    self.config_entry.data[CONF_PASSWORD], force=True
                )
                core_payloads = await self._async_fetch_core_payloads(week_start, next_week_start)
            except LibrusAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except LibrusError as err:
                raise UpdateFailed(str(err)) from err
        except LibrusAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except LibrusError as err:
            raise UpdateFailed(str(err)) from err

        # NOTE: this order must match _async_fetch_core_payloads' return -
        # tier 1 (core) fields first, then tier 2 (optional) fields, each
        # tier in the exact order its own gather() lists them.
        (
            me_payload,
            grades_payload,
            categories_payload,
            notes_payload,
            attendances_payload,
            attendance_types_payload,
            timetable_this_week,
            timetable_next_week,
            homeworks_payload,
            notices_payload,
            grade_comments_payload,
            homework_assignments_payload,
            behaviour_grades_payload,
            behaviour_grade_comments_payload,
            descriptive_grades_payload,
            parent_teacher_conferences_payload,
        ) = core_payloads

        lucky_number = await self._async_get_lucky_number(today)
        await self._async_refresh_reference_data()
        (
            unread_count,
            unread_by_mailbox,
            messages,
            substitution_messages,
            alert_messages,
            justification_messages,
        ) = await self._async_get_messages()

        grades = _parse_grades(grades_payload, _parse_comment_text_map(grade_comments_payload))
        school_notices = _parse_school_notices(notices_payload)
        notes = _parse_notes(notes_payload)
        homeworks = _parse_homeworks(homeworks_payload)
        attendances = _parse_attendances(attendances_payload)
        attendance_types = _parse_attendance_types(attendance_types_payload)
        timetable = merge_timetables(timetable_this_week, timetable_next_week)
        self._async_fire_new_item_events(grades, school_notices, notes, messages, homeworks)
        self._fire_timetable_change_events(timetable, today)
        self._fire_new_absence_events(attendances, attendance_types)

        return LibrusData(
            me=_parse_me(me_payload),
            grades=grades,
            grade_categories=_parse_grade_categories(categories_payload),
            notes=notes,
            attendances=attendances,
            attendance_types=attendance_types,
            timetable=timetable,
            homeworks=homeworks,
            school_notices=school_notices,
            lucky_number=lucky_number,
            subjects=self._cached_subjects,
            teachers=self._cached_teachers,
            classrooms=self._cached_classrooms,
            messages_available=self._messages_available,
            unread_message_count=unread_count,
            unread_messages_by_mailbox=unread_by_mailbox,
            messages=messages,
            substitution_messages=substitution_messages,
            alert_messages=alert_messages,
            justification_messages=justification_messages,
            school=self._cached_school,
            school_class=self._cached_class,
            free_days=self._cached_free_days,
            homework_assignments=_parse_homework_assignments(homework_assignments_payload),
            behaviour_grades=_parse_behaviour_grades(
                behaviour_grades_payload, _parse_comment_text_map(behaviour_grade_comments_payload)
            ),
            homework_categories=self._cached_homework_categories,
            note_categories=self._cached_note_categories,
            behaviour_grade_categories=self._cached_behaviour_grade_categories,
            descriptive_grades=_parse_descriptive_grades(descriptive_grades_payload),
            parent_teacher_conferences=_parse_parent_teacher_conferences(
                parent_teacher_conferences_payload
            ),
        )

    def _feature_enabled(self, key: str, default: bool) -> bool:
        """Read one of the options-flow feature toggles (see config_flow.py)
        - defaults to enabled (the pre-toggle behaviour) if the entry has
        never set it, or if called before a config_entry is attached."""
        if self.config_entry is None:
            return default
        return bool(self.config_entry.options.get(key, default))

    async def _maybe(self, enabled: bool, factory: Any) -> Any:
        """Skip a network call entirely when a feature is toggled off in the
        options flow, returning `{}` instead - the same shape every parser
        in this module already treats identically to a genuinely empty
        account, so no extra special-casing was needed downstream to wire
        these toggles up. `factory` is the client's bound method itself
        (not yet called), so a disabled feature never even builds the
        coroutine for its real network call."""
        if not enabled:
            return {}
        return await factory()

    async def _async_fetch_core_payloads(
        self, week_start: date, next_week_start: date
    ) -> tuple[Any, ...]:
        """The core (non-optional) data fetch - grades/attendance/timetable/
        agenda/announcements. Factored out of `_async_update_data` so it can
        be retried once, unmodified, after a forced re-login (see there).

        Split into two tiers, on purpose - previously all 16 endpoints were
        in ONE asyncio.gather(), so a single failure on any of them (e.g. a
        newer, less-exercised endpoint like DescriptiveGrades hiccuping)
        raised and discarded every OTHER endpoint's already-successful
        result too, wiping grades/attendance/timetable for the whole cycle
        over one unrelated endpoint. TIER 1 below is the original,
        genuinely load-bearing sensors - a failure there is still fatal and
        still drives the forced-relogin-and-retry-once logic in
        `_async_update_data`. TIER 2 is the newer supplementary endpoints -
        fetched with `return_exceptions=True` so one of them failing only
        degrades THAT ONE entity to empty this cycle, same "confirmed real,
        empty" degrade path this codebase already uses for a genuinely
        empty account.

        Announcements/BehaviourGrades/DescriptiveGrades are additionally
        gated by their own options-flow toggle via `_maybe` - disabled ones
        never hit the network at all, in either tier."""
        announcements_enabled = self._feature_enabled(
            CONF_ANNOUNCEMENTS_ENABLED, DEFAULT_ANNOUNCEMENTS_ENABLED
        )
        behaviour_grades_enabled = self._feature_enabled(
            CONF_BEHAVIOUR_GRADES_ENABLED, DEFAULT_BEHAVIOUR_GRADES_ENABLED
        )
        descriptive_grades_enabled = self._feature_enabled(
            CONF_DESCRIPTIVE_GRADES_ENABLED, DEFAULT_DESCRIPTIVE_GRADES_ENABLED
        )

        core = await asyncio.gather(
            self._client.async_get_me(),
            self._client.async_get_grades(),
            self._client.async_get_grade_categories(),
            self._client.async_get_notes(),
            self._client.async_get_attendances(),
            self._client.async_get_attendance_types(),
            self._client.async_get_timetable(week_start),
            self._client.async_get_timetable(next_week_start),
            self._client.async_get_homeworks(),
            self._maybe(announcements_enabled, self._client.async_get_school_notices),
        )

        optional_results = await asyncio.gather(
            self._client.async_get_grade_comments(),
            self._client.async_get_homework_assignments(),
            self._maybe(behaviour_grades_enabled, self._client.async_get_behaviour_grade_points),
            self._maybe(
                behaviour_grades_enabled, self._client.async_get_behaviour_grade_point_comments
            ),
            self._maybe(descriptive_grades_enabled, self._client.async_get_descriptive_grades),
            self._client.async_get_parent_teacher_conferences(),
            return_exceptions=True,
        )
        optional_payloads = [
            self._degrade_optional_payload(label, result)
            for label, result in zip(OPTIONAL_ENDPOINT_LABELS, optional_results)
        ]

        return (*core, *optional_payloads)

    def _degrade_optional_payload(
        self, label: str, result: dict[str, Any] | BaseException
    ) -> dict[str, Any]:
        """Turn one `return_exceptions=True` gather result into a payload,
        degrading a LibrusError to an empty dict (so its parser sees the
        same shape as a genuinely empty account) instead of letting it take
        down the rest of the core-data fetch. Anything that ISN'T a
        LibrusError (a real bug, or asyncio.CancelledError) is re-raised -
        only confirmed API-level failures are safe to swallow here.

        Also feeds the optional-endpoint-degraded repair issue tracking -
        a `_maybe()`-skipped (disabled-in-options) endpoint arrives here as
        a plain `{}`, which counts as a "success" for that tracking (it
        clears any previously-raised issue for it - turning a feature off
        isn't a failure worth flagging)."""
        if isinstance(result, BaseException):
            if isinstance(result, LibrusError):
                _LOGGER.debug(
                    "Optional endpoint '%s' fetch failed (non-fatal): %s",
                    label,
                    result,
                    exc_info=result,
                )
                self._note_optional_endpoint_failure(label)
                return {}
            raise result
        self._note_optional_endpoint_recovery(label)
        return result

    # How long a supplementary endpoint must fail on EVERY attempt before a
    # repair issue is raised for it - deliberately generous. Most of these
    # endpoints are "confirmed real, empty" for entire school years at a
    # time (see this project's own README/BACKLOG), so a short window would
    # constantly flag perfectly normal accounts; a week of unbroken
    # failures is a much stronger signal that something is actually wrong
    # (a permission change, an endpoint Librus removed, etc.) rather than
    # this account simply never having that kind of data.
    _OPTIONAL_ENDPOINT_DEGRADED_AFTER = timedelta(days=7)

    def _note_optional_endpoint_failure(self, label: str) -> None:
        if self.config_entry is None:
            return
        now = dt_util.utcnow()
        first_failed = self._optional_endpoint_first_failure.setdefault(label, now)
        if now - first_failed < self._OPTIONAL_ENDPOINT_DEGRADED_AFTER:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            optional_endpoint_issue_id(self.config_entry.entry_id, label),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_OPTIONAL_ENDPOINT_DEGRADED,
            translation_placeholders={"label": label, "since": first_failed.date().isoformat()},
        )

    def _note_optional_endpoint_recovery(self, label: str) -> None:
        """Always attempt the delete (a no-op if nothing was raised) rather
        than gating it on THIS coordinator instance's own in-memory
        failure tracking - the in-memory dict resets on every reload
        (options change, HA restart, ...), so a coordinator that comes back
        up already healthy would otherwise never clear an issue a PREVIOUS
        instance raised before that reload."""
        self._optional_endpoint_first_failure.pop(label, None)
        if self.config_entry is not None:
            ir.async_delete_issue(
                self.hass, DOMAIN, optional_endpoint_issue_id(self.config_entry.entry_id, label)
            )

    async def _async_get_lucky_number(self, today: date) -> LuckyNumberData | None:
        now = dt_util.now()
        if (
            self._cached_lucky_number is not None
            and self._lucky_number_fetched_date == today
            and now.hour < LUCKY_NUMBER_PUBLISH_HOUR
        ):
            return self._cached_lucky_number
        try:
            payload = await self._client.async_get_lucky_number()
        except LibrusError:
            _LOGGER.debug("Lucky number fetch failed (non-fatal)", exc_info=True)
            return self._cached_lucky_number
        lucky = _parse_lucky_number(payload)
        if lucky is not None:
            self._cached_lucky_number = lucky
            self._lucky_number_fetched_date = today
        return self._cached_lucky_number

    async def _async_refresh_reference_data(self) -> None:
        """Refresh near-static reference data at most once a day: subject/
        teacher/classroom name lookups, school/class identity, homework
        agenda categories, note categories, behaviour-grade categories, and
        the free-days calendar.

        The Subjects/Teachers/Classrooms endpoint names were UNVERIFIED when
        first written but are now CONFIRMED live, same as everything else
        fetched here (2026-09-05) - a failure is still treated as non-fatal
        for all of it, since none of this is core data (grades/attendance/
        timetable keep working without it; entities just fall back to a raw
        numeric id, or a missing school/class sensor/calendar).
        """
        now = dt_util.utcnow()
        if (
            self._reference_data_fetched_at is not None
            and now - self._reference_data_fetched_at < timedelta(hours=24)
        ):
            return
        free_days_enabled = self._feature_enabled(CONF_FREE_DAYS_ENABLED, DEFAULT_FREE_DAYS_ENABLED)
        behaviour_grades_enabled = self._feature_enabled(
            CONF_BEHAVIOUR_GRADES_ENABLED, DEFAULT_BEHAVIOUR_GRADES_ENABLED
        )
        try:
            (
                subjects_payload,
                teachers_payload,
                classrooms_payload,
                schools_payload,
                classes_payload,
                homework_categories_payload,
                school_free_days_payload,
                class_free_days_payload,
                note_categories_payload,
                behaviour_grade_categories_payload,
            ) = await asyncio.gather(
                self._client.async_get_subjects(),
                self._client.async_get_teachers(),
                self._client.async_get_classrooms(),
                self._client.async_get_schools(),
                self._client.async_get_classes(),
                self._client.async_get_homework_categories(),
                self._maybe(free_days_enabled, self._client.async_get_school_free_days),
                self._maybe(free_days_enabled, self._client.async_get_class_free_days),
                self._client.async_get_note_categories(),
                self._maybe(
                    behaviour_grades_enabled, self._client.async_get_behaviour_grade_point_categories
                ),
            )
        except LibrusError:
            _LOGGER.warning(
                "Could not refresh reference data (subjects/teachers/"
                "classrooms/school/class/categories/free days) - affected "
                "entities will show raw ids or go stale until the next "
                "successful refresh",
                exc_info=True,
            )
            return
        self._cached_subjects = _parse_id_name_map(subjects_payload, ("Subjects",))
        self._cached_teachers = _parse_id_name_map(teachers_payload, ("Users", "Teachers"))
        self._cached_classrooms = _parse_id_name_map(classrooms_payload, ("Classrooms",))
        self._cached_school = _parse_school(schools_payload)
        self._cached_class = _parse_class(classes_payload)
        self._check_school_year_rollover()
        self._cached_homework_categories = _parse_id_name_map(
            homework_categories_payload, ("Categories",)
        )
        # A disabled toggle's payload is already `{}` (via `_maybe`), which
        # `_parse_free_days`/`_parse_id_name_map` below already treat the
        # same as a genuinely empty account - no extra branching needed.
        self._cached_free_days = _parse_free_days(
            school_free_days_payload, "SchoolFreeDays"
        ) + _parse_free_days(class_free_days_payload, "ClassFreeDays")
        self._cached_note_categories = _parse_id_name_map(
            note_categories_payload, ("Categories",)
        )
        self._cached_behaviour_grade_categories = _parse_id_name_map(
            behaviour_grade_categories_payload, ("Categories",)
        )
        self._reference_data_fetched_at = now

    # How long past the cached Class record's own `end_school_year` date
    # before flagging it as possibly stale - generous on purpose. The
    # coordinator already re-fetches `Classes` every 24h (see above), so
    # this normally self-heals well within a day of the real school year
    # rolling over; this only fires if Librus itself hasn't published a new
    # Class record in over a month, which is worth a nudge to reload rather
    # than silently showing a school year that ended a month ago forever.
    _SCHOOL_YEAR_ROLLOVER_GRACE = timedelta(days=30)

    def _check_school_year_rollover(self) -> None:
        """Raise (or clear) the "school year rollover" repair issue based on
        whether the cached `ClassData.end_school_year` is well in the past.
        Called every time `_cached_class` is freshly refetched (i.e. at
        most once a day) - see `_async_refresh_reference_data`."""
        if self.config_entry is None:
            return
        issue_id = school_year_issue_id(self.config_entry.entry_id)
        end_school_year = self._cached_class.end_school_year if self._cached_class else None
        end_date: date | None = None
        if end_school_year:
            try:
                end_date = date.fromisoformat(end_school_year[:10])
            except ValueError:
                end_date = None
        if end_date is not None and dt_util.now().date() - end_date >= self._SCHOOL_YEAR_ROLLOVER_GRACE:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_SCHOOL_YEAR_ROLLOVER,
                translation_placeholders={"end_date": end_school_year},
                data={"entry_id": self.config_entry.entry_id},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    async def _async_get_messages(
        self,
    ) -> tuple[
        int,
        dict[str, int],
        list[MessageData],
        list[MessageData],
        list[MessageData],
        list[MessageData],
    ]:
        """Fetch unread counts (per mailbox) + a recent-messages preview
        from the separate Wiadomości subsystem - inbox (full, as ever),
        plus full CONTENT (not just counts) for "substitutions", "alerts"
        and "justifications", the secondary mailboxes most worth actually
        reading rather than just knowing a count for. "justifications" was
        added 2026-09-06 (user request: "usprawiedliwienia") on the
        strength of the SAME architecture already confirmed for
        substitutions/alerts - all of these are sibling keys in one
        unread-count response, and share the identical
        `{mailbox}/messages` list endpoint, so this is a low-risk extension
        of a pattern already proven, not a new guess. UNVERIFIED: whether a
        submitted justification's accept/reject status is actually visible
        in this mailbox's message content, or only the school's own
        response text - first real submission will confirm.

        Bootstraps the dedicated session cookie once per login (not every
        cycle). Some schools don't have this Librus module enabled at all -
        that's a normal, non-fatal outcome (`async_bootstrap_messages`
        returns False, checked via the "Brak dostępu" marker), not an error.
        Any other failure here is also non-fatal - messages are a bonus
        feature, not core data, and must never fail the whole update cycle.
        """
        if self.config_entry is not None and not self.config_entry.options.get(
            CONF_MESSAGES_ENABLED, DEFAULT_MESSAGES_ENABLED
        ):
            # Turned off in the options flow - don't bootstrap the separate
            # wiadomosci.librus.pl session or make any messages calls.
            self._messages_available = False
            return 0, {}, [], [], [], []

        if not self._messages_bootstrapped:
            try:
                self._messages_available = await self._client.async_bootstrap_messages()
            except LibrusError:
                _LOGGER.debug("Messages bootstrap failed (non-fatal)", exc_info=True)
                self._messages_available = False
            self._messages_bootstrapped = True
        if not self._messages_available:
            return 0, {}, [], [], [], []

        try:
            unread_payload, inbox_payload = await asyncio.gather(
                self._client.async_get_unread_messages_count(),
                self._client.async_get_messages(limit=10),
            )
        except LibrusError:
            _LOGGER.debug("Messages fetch failed (non-fatal)", exc_info=True)
            return 0, {}, [], [], [], []
        unread_count, unread_by_mailbox, inbox_messages = _parse_messages(unread_payload, inbox_payload)

        # BUG FIX (2026-09-06, found live): substitutions/alerts used to be
        # fetched in the SAME asyncio.gather() as the two calls above -
        # asyncio.gather() fails as a whole the moment ANY one of its
        # awaitables raises, so a failure fetching these two bonus
        # mailboxes was silently wiping out the otherwise-working
        # inbox/unread-count data too (confirmed live: mailbox_breakdown
        # went from real per-mailbox counts to an empty {} the moment this
        # was added in v0.4.13). Isolated into its own try/except so it can
        # only ever degrade to "no substitutions/alerts/justifications
        # shown", never take the core inbox data down with it.
        substitution_messages: list[MessageData] = []
        alert_messages: list[MessageData] = []
        justification_messages: list[MessageData] = []
        try:
            substitutions_payload, alerts_payload, justifications_payload = await asyncio.gather(
                self._client.async_get_messages(mailbox="substitutions", limit=10),
                self._client.async_get_messages(mailbox="alerts", limit=10),
                self._client.async_get_messages(mailbox="justifications", limit=10),
            )
            substitution_messages = _parse_message_list(substitutions_payload, "substitutions")
            alert_messages = _parse_message_list(alerts_payload, "alerts")
            justification_messages = _parse_message_list(justifications_payload, "justifications")
        except LibrusError:
            _LOGGER.debug(
                "Secondary mailbox (substitutions/alerts/justifications) fetch failed (non-fatal)",
                exc_info=True,
            )

        return (
            unread_count,
            unread_by_mailbox,
            inbox_messages,
            substitution_messages,
            alert_messages,
            justification_messages,
        )

    def _async_fire_new_item_events(
        self,
        grades: list[GradeData],
        notices: list[SchoolNoticeData],
        notes: list[NoteData],
        messages: list[MessageData],
        homeworks: list[HomeworkEventData],
    ) -> None:
        entry_id = self.config_entry.entry_id if self.config_entry else None
        # Resolved names are included alongside the raw ids so an automation
        # (e.g. a notification blueprint) can use {{ trigger.event.data.
        # subject }} directly, without its own lookup against the sensor
        # attributes just to say which subject/teacher a grade or note was
        # about.
        self._known_grade_ids = self._fire_for_new_ids(
            EVENT_NEW_GRADE,
            entry_id,
            self._known_grade_ids,
            {
                g.id: {
                    "subject_id": g.subject_id,
                    "subject": self._cached_subjects.get(g.subject_id, str(g.subject_id))
                    if g.subject_id is not None
                    else None,
                    "value": g.value,
                }
                for g in grades
            },
        )
        self._known_notice_ids = self._fire_for_new_ids(
            EVENT_NEW_ANNOUNCEMENT,
            entry_id,
            self._known_notice_ids,
            {n.id: {"subject": n.subject} for n in notices},
        )
        self._known_note_ids = self._fire_for_new_ids(
            EVENT_NEW_NOTE,
            entry_id,
            self._known_note_ids,
            {
                n.id: {
                    "positive": n.positive,
                    "sentiment": n.sentiment,
                    "teacher": self._cached_teachers.get(n.teacher_id, str(n.teacher_id))
                    if n.teacher_id is not None
                    else None,
                    "text": n.text,
                }
                for n in notes
            },
        )
        self._known_message_ids = self._fire_for_new_ids(
            EVENT_NEW_MESSAGE,
            entry_id,
            self._known_message_ids,
            {m.id: {"sender": m.sender_name, "topic": m.topic} for m in messages},
        )
        self._known_homework_ids = self._fire_for_new_ids(
            EVENT_NEW_HOMEWORK,
            entry_id,
            self._known_homework_ids,
            {
                h.id: {
                    "subject_id": h.subject_id,
                    "subject": self._cached_subjects.get(h.subject_id, str(h.subject_id))
                    if h.subject_id is not None
                    else None,
                    "category": self._cached_homework_categories.get(h.category_id)
                    if h.category_id is not None
                    else None,
                    "date": h.date,
                    "content": (h.content or "")[:200],
                }
                for h in homeworks
            },
        )

    def _fire_timetable_change_events(
        self, timetable: dict[date, list[LessonData]], today: date
    ) -> None:
        """Fire EVENT_TIMETABLE_CHANGED for any cancelled/substitution
        lesson on today or a later date that wasn't already known. Reuses
        the exact seed-silently-then-diff machinery of the *_new_* events -
        the "id" here is a synthetic date+period+kind+subject signature, so
        a disruption that scrolls out of the fetch window and back doesn't
        re-announce (union, not replace)."""
        entry_id = self.config_entry.entry_id if self.config_entry else None
        items: dict[str, dict[str, Any]] = {}
        for day, lessons in timetable.items():
            if day < today:
                continue
            for lesson in lessons:
                if not (lesson.is_canceled or lesson.is_substitution):
                    continue
                kind = "canceled" if lesson.is_canceled else "substitution"
                signature = f"{day.isoformat()}|{lesson.lesson_no}|{kind}|{lesson.subject_id}"
                subject = (
                    self._cached_subjects.get(lesson.subject_id, str(lesson.subject_id))
                    if lesson.subject_id is not None
                    else None
                )
                items[signature] = {
                    "date": day.isoformat(),
                    "lesson_no": lesson.lesson_no,
                    "kind": kind,
                    "subject_id": lesson.subject_id,
                    "subject": subject,
                    "hour_from": lesson.hour_from,
                }
        self._known_timetable_disruptions = self._fire_for_new_ids(
            EVENT_TIMETABLE_CHANGED, entry_id, self._known_timetable_disruptions, items
        )

    def _fire_new_absence_events(
        self,
        attendances: list[AttendanceData],
        attendance_types: dict[int, AttendanceTypeData],
    ) -> None:
        """Fire EVENT_NEW_ABSENCE for a newly-seen real absence record
        (any non-presence type - excused or not, `excused` in the payload
        says which). Seeded silently on the first sync like the other
        events."""
        entry_id = self.config_entry.entry_id if self.config_entry else None
        items: dict[int, dict[str, Any]] = {}
        for attendance in attendances:
            attendance_type = (
                attendance_types.get(attendance.type_id)
                if attendance.type_id is not None
                else None
            )
            if attendance_type is None or attendance_type.is_presence_kind:
                continue
            items[attendance.id] = {
                "date": attendance.date,
                "type": attendance_type.name,
                "excused": attendance_type.is_excused_absence,
                "lesson_no": attendance.lesson_no,
            }
        self._known_absence_ids = self._fire_for_new_ids(
            EVENT_NEW_ABSENCE, entry_id, self._known_absence_ids, items
        )

    def _fire_for_new_ids(
        self,
        event: str,
        entry_id: str | None,
        known: set[Any] | None,
        items: dict[Any, dict[str, Any]],
    ) -> set[Any]:
        current_ids = set(items)
        if known is None:
            # First-ever refresh for this entry: seed silently. Firing here
            # would replay the account's whole history as "new" on install.
            return current_ids
        new_ids = current_ids - known
        for item_id in new_ids:
            self.hass.bus.async_fire(event, {"entry_id": entry_id, "id": item_id, **items[item_id]})
        # Union, not replace: an item that later drops out of the fetch
        # window must not be re-announced if it reappears.
        return known | current_ids


# ----------------------------------------------------------------------
# Parsing. Defensive throughout (missing keys default sensibly) - Librus
# doesn't publish a schema and per-school variations are known to exist
# upstream (see RustySnek/librus-apix's README).
# ----------------------------------------------------------------------


def _parse_me(payload: dict[str, Any]) -> MeData:
    me = payload.get("Me") or {}
    account = me.get("Account") or {}
    # CONFIRMED live: `Account` is the LOGIN's own identity, which for a
    # child's account under a parent-managed portal is the PARENT's name
    # (e.g. Account.FirstName/LastName was the parent, while `User` was the
    # actual student) - `MeData` is meant to represent the student, so read
    # the name from `User`, keeping only the id from `Account`.
    student = me.get("User") or {}
    return MeData(
        account_id=account.get("Id"),
        first_name=student.get("FirstName", ""),
        last_name=student.get("LastName", ""),
    )


def _parse_grade_categories(payload: dict[str, Any]) -> dict[int, GradeCategoryData]:
    items = payload.get("Categories")
    if not isinstance(items, list):
        return {}
    result: dict[int, GradeCategoryData] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        item_id = int(item["Id"])
        result[item_id] = GradeCategoryData(
            id=item_id,
            name=item.get("Name", ""),
            count_to_average=bool(item.get("CountToTheAverage", True)),
            weight=int(item.get("Weight") or 1),
        )
    return result


def _parse_comment_text_map(payload: dict[str, Any] | None) -> dict[int, str]:
    """Parses a `{"Comments": [{"Id", "Text"}, ...]}`-shaped payload (used
    by both `Grades/Comments` and `BehaviourGrades/Points/Comments`,
    CONFIRMED live 2026-09-06 to share this shape) into an id->text map."""
    if not payload:
        return {}
    items = payload.get("Comments")
    if not isinstance(items, list):
        return {}
    result: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        text = item.get("Text")
        if text:
            result[int(item["Id"])] = text
    return result


def _resolve_comment_ids(raw: Any, comment_text_by_id: dict[int, str]) -> list[str]:
    """Resolves a per-grade/-behaviour-grade `Comments` field against a
    `_parse_comment_text_map` lookup.

    CONFIRMED (2026-09-06) via szkolny-android's reference parsers that
    this field is a list of ids into the separate Comments endpoint, NOT
    embedded `{"Text": ...}` objects as previously assumed here - but the
    exact per-id shape (bare int vs. `{"Id": ...}`) is still unconfirmed
    (empty on this account either way), so both are handled, plus the old
    embedded-`Text` shape as a fallback in case that turns out right after
    all.
    """
    if not isinstance(raw, list):
        return []
    resolved: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            if entry.get("Text"):
                resolved.append(str(entry["Text"]))
                continue
            comment_id = entry.get("Id")
        else:
            comment_id = entry
        if comment_id is None:
            continue
        try:
            text = comment_text_by_id.get(int(comment_id))
        except (TypeError, ValueError):
            text = None
        if text:
            resolved.append(text)
    return resolved


def _parse_grades(
    payload: dict[str, Any], comment_text_by_id: dict[int, str] | None = None
) -> list[GradeData]:
    items = payload.get("Grades")
    if not isinstance(items, list):
        return []
    grades: list[GradeData] = []
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        category = item.get("Category") or {}
        subject = item.get("Subject") or {}
        comments = _resolve_comment_ids(item.get("Comments"), comment_text_by_id or {})
        grades.append(
            GradeData(
                id=int(item["Id"]),
                value=str(item.get("Grade", "")),
                category_id=category.get("Id"),
                subject_id=subject.get("Id"),
                semester=item.get("Semester"),
                add_date=item.get("AddDate"),
                is_semester_proposition=bool(item.get("IsSemesterProposition")),
                is_final_proposition=bool(item.get("IsFinalProposition")),
                comments=comments,
            )
        )
    return grades


def _parse_notes(payload: dict[str, Any]) -> list[NoteData]:
    items = payload.get("Notes")
    if not isinstance(items, list):
        return []
    notes: list[NoteData] = []
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        category = item.get("Category") or {}
        teacher = item.get("Teacher") or {}
        notes.append(
            NoteData(
                id=int(item["Id"]),
                text=item.get("Text", ""),
                category_id=category.get("Id"),
                teacher_id=teacher.get("Id"),
                date=item.get("Date"),
                positive=item.get("Positive"),
            )
        )
    return notes


def _parse_attendances(payload: dict[str, Any]) -> list[AttendanceData]:
    items = payload.get("Attendances")
    if not isinstance(items, list):
        return []
    attendances: list[AttendanceData] = []
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        lesson = item.get("Lesson") or {}
        type_ = item.get("Type") or {}
        attendances.append(
            AttendanceData(
                id=int(item["Id"]),
                lesson_id=lesson.get("Id"),
                lesson_no=item.get("LessonNo"),
                date=item.get("Date"),
                semester=item.get("Semester"),
                type_id=type_.get("Id"),
            )
        )
    return attendances


def _parse_attendance_types(payload: dict[str, Any]) -> dict[int, AttendanceTypeData]:
    # CONFIRMED live: the response root key is "Types" (matching the
    # Attendances/Types endpoint path), not "AttendanceTypes". `IsPresenceKind`
    # is real - see AttendanceTypeData's docstring.
    items = payload.get("Types")
    if not isinstance(items, list):
        return {}
    result: dict[int, AttendanceTypeData] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        item_id = int(item["Id"])
        name = item.get("Name") or item.get("Short") or item.get("Shortcut") or ""
        result[item_id] = AttendanceTypeData(
            id=item_id, name=name, is_presence_kind=bool(item.get("IsPresenceKind"))
        )
    return result


def _as_int(value: Any) -> int | None:
    """Coerce an id to int. CONFIRMED live: Timetables returns Subject/
    Teacher/Classroom/Lesson ids as STRINGS ("41999"), unlike every other
    endpoint (Grades, Attendances, ...) which use plain ints - normalize
    here so lookups against `subjects`/`teachers`/`classrooms` (keyed by
    int) work regardless of which endpoint an id came from."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_lesson(raw: dict[str, Any]) -> LessonData:
    subject = raw.get("Subject") or {}
    teacher = raw.get("Teacher") or {}
    classroom = raw.get("Classroom") or {}
    return LessonData(
        lesson_no=_as_int(raw.get("LessonNo")),
        hour_from=raw.get("HourFrom"),
        hour_to=raw.get("HourTo"),
        subject_id=_as_int(subject.get("Id")),
        teacher_id=_as_int(teacher.get("Id")),
        classroom_id=_as_int(classroom.get("Id")),
        is_canceled=bool(raw.get("IsCanceled")),
        is_substitution=bool(raw.get("IsSubstitutionClass")),
    )


def merge_timetables(*payloads: dict[str, Any]) -> dict[date, list[LessonData]]:
    """Merge one or more `Timetables?weekStart=...` responses into a single
    date-keyed dict of lessons.

    CONFIRMED live: each date maps to a list of PERIOD SLOTS (one per
    lesson-number, always the same length even on days with no school),
    each itself a list of 0+ lesson dicts (more than one when a period is
    split into parallel groups, e.g. two language classes at once) - NOT a
    flat list of lessons per day as the reverse-engineered spec assumed.
    """
    result: dict[date, list[LessonData]] = {}
    for payload in payloads:
        timetable = payload.get("Timetable")
        if not isinstance(timetable, dict):
            continue
        for date_str, day_slots in timetable.items():
            if not isinstance(day_slots, list):
                continue
            try:
                day = date.fromisoformat(date_str)
            except (TypeError, ValueError):
                continue
            lessons: list[LessonData] = []
            for slot in day_slots:
                if not isinstance(slot, list):
                    continue
                lessons.extend(_parse_lesson(lesson) for lesson in slot if isinstance(lesson, dict))
            result[day] = lessons
    return result


def _parse_homeworks(payload: dict[str, Any]) -> list[HomeworkEventData]:
    items = payload.get("HomeWorks")
    if not isinstance(items, list):
        return []
    events: list[HomeworkEventData] = []
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        category = item.get("Category") or {}
        subject = item.get("Subject") or {}
        events.append(
            HomeworkEventData(
                id=int(item["Id"]),
                date=item.get("Date"),
                content=item.get("Content", ""),
                category_id=category.get("Id"),
                subject_id=subject.get("Id"),
                time_from=item.get("TimeFrom"),
            )
        )
    return events


def _parse_homework_assignments(payload: dict[str, Any]) -> list[HomeworkAssignmentData]:
    """Real homework assignments ("Zadania domowe") - distinct from the
    general `HomeWorks` agenda feed above (`_parse_homeworks`), which
    covers tests/trips/etc. too. Fields CONFIRMED (2026-09-06) via
    szkolny-android's `LibrusApiHomework.kt`. Notably NO `Subject` field
    appears in the reference parser - unlike the general agenda feed,
    there's no subject to resolve here. Still empty on this account, so
    unverified against a real populated example."""
    items = payload.get("HomeWorkAssignments")
    if not isinstance(items, list):
        return []
    assignments: list[HomeworkAssignmentData] = []
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        teacher = item.get("Teacher") or {}
        assignments.append(
            HomeworkAssignmentData(
                id=int(item["Id"]),
                topic=item.get("Topic", ""),
                text=item.get("Text", ""),
                teacher_id=teacher.get("Id"),
                date=item.get("Date"),
                due_date=item.get("DueDate"),
            )
        )
    return assignments


def _parse_behaviour_grades(
    payload: dict[str, Any], comment_text_by_id: dict[int, str] | None = None
) -> list[BehaviourGradeData]:
    """A formal "ocena zachowania" (behaviour grade) - distinct from Notes
    ("uwagi", free-text remarks). Fields CONFIRMED (2026-09-06) via
    szkolny-android's `LibrusApiBehaviourGrades.kt`. Still empty on this
    account, so unverified against a real populated example."""
    items = payload.get("Grades")
    if not isinstance(items, list):
        return []
    grades: list[BehaviourGradeData] = []
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        category = item.get("Category") or {}
        added_by = item.get("AddedBy") or {}
        comments = _resolve_comment_ids(item.get("Comments"), comment_text_by_id or {})
        grades.append(
            BehaviourGradeData(
                id=int(item["Id"]),
                value=item.get("Value"),
                short_name=item.get("ShortName", ""),
                semester=item.get("Semester"),
                category_id=category.get("Id"),
                teacher_id=added_by.get("Id"),
                add_date=item.get("AddDate"),
                text=item.get("Text", ""),
                comments=comments,
            )
        )
    return grades


def _parse_descriptive_grades(payload: dict[str, Any]) -> list[DescriptiveGradeData]:
    """An alternate, non-numeric grading system - CONFIRMED (via the
    `Units` endpoint) to be enabled for this school, unlike `PointGrades`.
    Fields CONFIRMED (2026-09-06) via szkolny-android's
    `LibrusApiDescriptiveGrades.kt`. `Skill`/`Category` are kept as raw ids
    - their own name-lookup endpoints (`DescriptiveGrades/Skills`,
    `/Types`) weren't probed this session, so no name to resolve them to
    yet. Still empty on this account, so unverified against a real
    populated example."""
    items = payload.get("Grades")
    if not isinstance(items, list):
        return []
    grades: list[DescriptiveGradeData] = []
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        subject = item.get("Subject") or {}
        skill = item.get("Skill") or {}
        category = item.get("Category") or {}
        grades.append(
            DescriptiveGradeData(
                id=int(item["Id"]),
                subject_id=subject.get("Id"),
                value=item.get("Grade", ""),
                skill_id=skill.get("Id"),
                category_id=category.get("Id"),
                add_date=item.get("AddDate"),
            )
        )
    return grades


def _parse_parent_teacher_conferences(payload: dict[str, Any]) -> list[ParentTeacherConferenceData]:
    """Fields CONFIRMED (2026-09-06) via szkolny-android's
    `LibrusApiPtMeetings.kt`. Live-verified separately that this kind of
    meeting already surfaces through `HomeWorks` too - see
    `ParentTeacherConferenceData`'s docstring. Never seen populated here."""
    items = payload.get("ParentTeacherConferences")
    if not isinstance(items, list):
        return []
    conferences: list[ParentTeacherConferenceData] = []
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        teacher = item.get("Teacher") or {}
        conferences.append(
            ParentTeacherConferenceData(
                id=int(item["Id"]),
                topic=item.get("Topic", ""),
                teacher_id=teacher.get("Id"),
                date=item.get("Date"),
                time=item.get("Time"),
            )
        )
    return conferences


def _parse_school_notices(payload: dict[str, Any]) -> list[SchoolNoticeData]:
    items = payload.get("SchoolNotices")
    if not isinstance(items, list):
        return []
    notices: list[SchoolNoticeData] = []
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        notices.append(
            SchoolNoticeData(
                id=str(item["Id"]),
                subject=item.get("Subject", ""),
                content=item.get("Content", ""),
                start_date=item.get("StartDate"),
                end_date=item.get("EndDate"),
                creation_date=item.get("CreationDate"),
                was_read=bool(item.get("WasRead")),
            )
        )
    return notices


def _parse_lucky_number(payload: dict[str, Any]) -> LuckyNumberData | None:
    raw = payload.get("LuckyNumber")
    if not isinstance(raw, dict) or raw.get("LuckyNumber") is None:
        return None
    try:
        number = int(raw["LuckyNumber"])
    except (TypeError, ValueError):
        return None
    return LuckyNumberData(day=raw.get("LuckyNumberDay"), number=number)


# CONFIRMED live (2026-09-06): the single-message endpoint's `Message`
# field (LibrusApiClient.async_get_message), once base64-decoded, is NOT
# plain text - it's a tiny XML wrapper,
# `<Message><Content><![CDATA[the real text...]]></Content></Message>`,
# and the real content lives inside the CDATA section. Found live: a
# card's expanded message view showed the literal
# "<Message><Content><![CDATA[" prefix leaking into the display. The list
# endpoint's `content` field does NOT do this (confirmed plain text, no
# wrapper) - decode_message_content is shared by both, so this regex is a
# no-op there (it simply won't match).
_MESSAGE_XML_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
# Defensive fallback for a CDATA section missing its closing "]]>" - e.g.
# if a future endpoint truncates this XML-wrapped text the way the list
# endpoint's plain-text `content` is known to (unconfirmed whether
# async_get_message's response can be truncated at all, but showing "cut
# off mid-sentence" beats showing raw XML markup either way).
_MESSAGE_XML_CDATA_OPEN_RE = re.compile(r"<!\[CDATA\[(.*)$", re.DOTALL)

# CONFIRMED live (2026-09-10): Librus rewrites every link in a message
# body into an <a href="https://liblink.pl/..." title="Link został
# skonwertowany...">...</a> tag (its own "link converter"), and the list
# endpoint's content can carry other light HTML (<br>, <p>). Rendered as
# plain text in the Wiadomości card that reads as raw tag soup. Flatten
# it: keep the link (its visible text, or the href), turn <br>/</p> into
# newlines, drop the rest, unescape entities.
_A_TAG_RE = re.compile(r'<a\b[^>]*?\bhref="([^"]*)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLOCK_END_RE = re.compile(r"</(?:p|div|li|h[1-6])>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def _flatten_message_html(text: str) -> str:
    if "<" not in text:
        return text

    def _anchor(match: re.Match[str]) -> str:
        href = match.group(1).strip()
        inner = _ANY_TAG_RE.sub("", match.group(2)).strip()
        if not inner or inner == href:
            return href
        return f"{inner} ({href})"

    text = _A_TAG_RE.sub(_anchor, text)
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _ANY_TAG_RE.sub("", text)
    text = html_unescape(text)
    return _MULTI_NL_RE.sub("\n\n", text).strip()


def decode_message_content(raw: str) -> str:
    """The list endpoint's `content` field (and the single-message
    endpoint's `Message` field - see `LibrusApiClient.async_get_message`)
    are base64-encoded (CONFIRMED live - decoding several real messages
    produced readable Polish text). Falls back to the raw string if the
    payload isn't valid base64 at all, rather than raising and losing the
    whole messages feature over one bad entry. Public (not
    underscore-prefixed) - shared with `services.py`'s `get_message`
    handler, which decodes the full-content field the same way.

    CONFIRMED live (2026-09-06): Librus truncates this field to a fixed
    BYTE length, which can land mid-multi-byte UTF-8 character (e.g. a
    Polish "ą"/"ę"/"ń") - a plain `.decode("utf-8")` then raises
    UnicodeDecodeError on an otherwise-valid message, and previously this
    fell all the way back to the raw, still-base64-encoded string (visible
    in the Wiadomości card as an unbroken hash-like blob causing horizontal
    scroll). Retry with `errors="ignore"` first, which just drops the
    incomplete trailing bytes and keeps the readable prefix - matches how
    every OTHER truncated message already reads (cut off mid-word, not
    mid-character).
    """
    try:
        decoded_bytes = base64.b64decode(raw)
    except ValueError:
        return raw
    try:
        text = decoded_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = decoded_bytes.decode("utf-8", errors="ignore")

    if (match := _MESSAGE_XML_CDATA_RE.search(text)) is not None:
        text = match.group(1)
    elif (match := _MESSAGE_XML_CDATA_OPEN_RE.search(text)) is not None:
        text = match.group(1)
    return _flatten_message_html(text)


# CONFIRMED live: the unread-count response is a per-mailbox breakdown
# ({"data": {"inbox": N, "notes": N, "alerts": N, "substitutions": N,
# "absences": N, "justifications": N, "trash": N, "archiveInbox": N, ...}}),
# not a flat number. The non-"archive*" ones are surfaced - "substitutions"
# in particular is likely the single most actionable one for a parent
# (schedule changes), and it costs nothing extra since this whole response
# is already being fetched for the plain inbox count.
_MESSAGE_MAILBOXES = (
    "inbox",
    "notes",
    "alerts",
    "substitutions",
    "absences",
    "justifications",
    "trash",
)


def _parse_message_list(list_payload: dict[str, Any], mailbox: str) -> list[MessageData]:
    """Shared by every mailbox's list endpoint - inbox, substitutions,
    alerts, ... all share the same response shape."""
    items = list_payload.get("data")
    if not isinstance(items, list):
        return []
    messages: list[MessageData] = []
    for item in items:
        if not isinstance(item, dict) or item.get("messageId") is None:
            continue
        sender_name = item.get("senderName") or (
            f"{item.get('senderFirstName', '')} {item.get('senderLastName', '')}".strip()
        )
        messages.append(
            MessageData(
                id=str(item["messageId"]),
                sender_name=sender_name,
                topic=item.get("topic", ""),
                content=decode_message_content(item.get("content", "")),
                send_date=item.get("sendDate"),
                read_date=item.get("readDate"),
                has_attachment=bool(item.get("isAnyFileAttached")),
                mailbox=mailbox,
            )
        )
    return messages


def _parse_messages(
    unread_payload: dict[str, Any], list_payload: dict[str, Any]
) -> tuple[int, dict[str, int], list[MessageData]]:
    unread_by_mailbox: dict[str, int] = {}
    unread_data = unread_payload.get("data")
    if isinstance(unread_data, dict):
        for mailbox in _MESSAGE_MAILBOXES:
            try:
                unread_by_mailbox[mailbox] = int(unread_data.get(mailbox) or 0)
            except (TypeError, ValueError):
                unread_by_mailbox[mailbox] = 0
    unread_count = unread_by_mailbox.get("inbox", 0)
    return unread_count, unread_by_mailbox, _parse_message_list(list_payload, "inbox")


def _parse_school(payload: dict[str, Any]) -> SchoolData | None:
    school = payload.get("School")
    if not isinstance(school, dict):
        return None
    head_first = school.get("NameHeadTeacher") or ""
    head_last = school.get("SurnameHeadTeacher") or ""
    head_name = f"{head_first} {head_last}".strip() or None
    return SchoolData(
        name=school.get("Name", ""),
        town=school.get("Town"),
        street=school.get("Street"),
        building_number=school.get("BuildingNumber"),
        post_code=school.get("PostCode"),
        head_teacher_name=head_name,
        email=school.get("Email"),
        phone_number=school.get("PhoneNumber"),
    )


def _parse_class(payload: dict[str, Any]) -> ClassData | None:
    cls = payload.get("Class")
    if not isinstance(cls, dict):
        return None
    tutor = cls.get("ClassTutor") or {}
    return ClassData(
        number=cls.get("Number"),
        symbol=cls.get("Symbol", ""),
        tutor_id=tutor.get("Id"),
        begin_school_year=cls.get("BeginSchoolYear"),
        end_first_semester=cls.get("EndFirstSemester"),
        end_school_year=cls.get("EndSchoolYear"),
    )


def _parse_free_days(payload: dict[str, Any], root_key: str) -> list[FreeDayData]:
    items = payload.get(root_key)
    if not isinstance(items, list):
        return []
    free_days: list[FreeDayData] = []
    for item in items:
        if (
            not isinstance(item, dict)
            or item.get("Id") is None
            or not item.get("DateFrom")
            or not item.get("DateTo")
        ):
            continue
        free_days.append(
            FreeDayData(
                id=int(item["Id"]),
                name=item.get("Name", ""),
                date_from=item["DateFrom"],
                date_to=item["DateTo"],
            )
        )
    return free_days


def _parse_id_name_map(payload: dict[str, Any], list_keys: tuple[str, ...]) -> dict[int, str]:
    items: Any = None
    for key in list_keys:
        if key in payload:
            items = payload[key]
            break
    if not isinstance(items, list):
        return {}
    result: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        # CONFIRMED live: some Users entries (school admin/secretariat
        # accounts) have FirstName explicitly `null`, not just absent - `or
        # ""` is required here, `.get(key, "")` alone does NOT catch a
        # present-but-None value.
        first = item.get("FirstName") or ""
        last = item.get("LastName") or ""
        # CONFIRMED live: Notes/Categories uses "CategoryName", not "Name"
        # like every other id-name lookup this helper is used for.
        name = item.get("Name") or item.get("CategoryName") or f"{first} {last}".strip()
        if name:
            result[int(item["Id"])] = name
    return result
