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
from datetime import date, datetime, time, timedelta
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
    CONF_QUIET_HOURS_ENABLED,
    CONF_QUIET_HOURS_END,
    CONF_QUIET_HOURS_START,
    DEFAULT_ANNOUNCEMENTS_ENABLED,
    DEFAULT_BEHAVIOUR_GRADES_ENABLED,
    DEFAULT_DESCRIPTIVE_GRADES_ENABLED,
    DEFAULT_FREE_DAYS_ENABLED,
    DEFAULT_MESSAGES_ENABLED,
    DEFAULT_QUIET_HOURS_ENABLED,
    DEFAULT_QUIET_HOURS_END,
    DEFAULT_QUIET_HOURS_START,
    DOMAIN,
    EVENT_ACHIEVEMENT_UNLOCKED,
    EVENT_NEW_ABSENCE,
    EVENT_NEW_ANNOUNCEMENT,
    EVENT_NEW_GRADE,
    EVENT_NEW_HOMEWORK,
    EVENT_NEW_MESSAGE,
    CORE_ENDPOINT_LABELS,
    EVENT_NEW_NOTE,
    EVENT_TIMETABLE_CHANGED,
    ISSUE_OPTIONAL_ENDPOINT_DEGRADED,
    ISSUE_SCHOOL_YEAR_ROLLOVER,
    LUCKY_NUMBER_PUBLISH_HOUR,
    OPTIONAL_ENDPOINT_LABELS,
    REFERENCE_DATA_ENDPOINT_LABELS,
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

# Kindergarten discovery (see `_async_maybe_discover_kindergarten`): how
# long to wait before trying again after finding nothing, and how many
# candidate LIDs to probe per attempt.
_KINDERGARTEN_DISCOVERY_RETRY = timedelta(hours=24)
_KINDERGARTEN_MAX_CANDIDATES = 6
_LID_USER_PREFIX = "LID-AUTH-USER-"


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


def parse_grade_value(value: str) -> float | None:
    """Convert a Librus grade string ("5+", "4-", "3", "bz"...) to a number.

    The "+"/"-" modifiers (+0.5 / -0.25) follow the convention used by most
    third-party Polish gradebook average calculators. CONFIRMED live
    (2026-09-05) via the `Grades/Types` reference endpoint that every
    non-numeric value Librus actually uses (`bz`, `np`, `nk`, `uł`, `nł`,
    `zl`, `nz`, `zw`, `uc`, `nu`, bare `+`/`-`) correctly falls through to
    returning None here and is excluded from the average.

    The +0.5 half is now CONFIRMED correct (2026-09-22): a raw probe of
    Librus's own `/Grades` API (real `4+`/`5+` grades that appeared live,
    2026-09) showed it carries NO numeric value for a modified grade at
    all - only the string (`"Grade": "4+"`), so this function computing
    4.5 from that string is not, by itself, proof of anything (it's the
    same assumed convention checking itself). The real independent check
    was cross-referencing the real Librus app's own displayed average for
    that `4+` grade - also 4.5, confirmed by the account owner. -0.25
    remains unverified - no `-`-modified grade has appeared live yet to
    cross-check the same way.

    Public (not underscore-prefixed) - shared by sensor.py's average
    calculation AND the good-grade-streak/achievement logic below, which
    is why it lives here rather than in sensor.py (coordinator.py must
    never import from sensor.py - the dependency only runs the other way).
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


# "Dobra" ocena, for the good-grade-streak sensor/achievements below - 4
# ("dobry") and up. Not user-configurable (unlike e.g. the Low Grade Alert
# blueprint's own threshold input) - keeping this one fixed avoids a
# second, subtly different "what counts as good" knob to document.
_GOOD_GRADE_STREAK_THRESHOLD = 4.0


def good_grade_streak(grades: list[GradeData]) -> int:
    """Consecutive most-recent NUMERIC grades >= _GOOD_GRADE_STREAK_THRESHOLD,
    counting back from the newest until the first one below it. Semester/
    final grades (proposed OR actual) excluded (not day-to-day grades,
    same as the average calculation). A non-numeric mark (bz/np/...) is
    SKIPPED, not counted as breaking the streak - it isn't really a "bad
    grade", just an administrative mark, and penalizing it would feel
    unfair for what this is meant to be: a small, motivating "passa" a
    student can watch grow."""
    dated = sorted(
        (
            g
            for g in grades
            if g.add_date
            and not g.is_semester_proposition
            and not g.is_final_proposition
            and not g.is_semester
            and not g.is_final
        ),
        key=lambda g: g.add_date,
        reverse=True,
    )
    streak = 0
    for grade in dated:
        value = parse_grade_value(grade.value)
        if value is None:
            continue
        if value < _GOOD_GRADE_STREAK_THRESHOLD:
            break
        streak += 1
    return streak


def _days_since(dates: list[str], school_class: ClassData | None, today: date) -> int | None:
    """Shared by days_since_last_absence/days_since_last_negative_note
    below - falls back to days since the school year started
    (`ClassData.begin_school_year`) when `dates` is empty, so a student
    with a genuinely perfect record shows a real, growing streak instead
    of `unknown`. `None` only when there's truly nothing to anchor to."""
    reference = max(dates) if dates else (school_class.begin_school_year if school_class else None)
    if not reference:
        return None
    try:
        reference_date = date.fromisoformat(reference[:10])
    except ValueError:
        return None
    return max(0, (today - reference_date).days)


def days_since_last_absence(
    attendances: list[AttendanceData],
    attendance_types: dict[int, AttendanceTypeData],
    school_class: ClassData | None,
    today: date,
) -> int | None:
    dates = [
        a.date
        for a in attendances
        if a.date and (t := attendance_types.get(a.type_id)) is not None and not t.is_presence_kind
    ]
    return _days_since(dates, school_class, today)


def days_since_last_negative_note(
    notes: list[NoteData], school_class: ClassData | None, today: date
) -> int | None:
    dates = [n.date for n in notes if n.date and n.sentiment == "negative"]
    return _days_since(dates, school_class, today)


# Milestone thresholds for the streak-based achievements fired by
# `LibrusDataUpdateCoordinator._check_achievements` - crossing one fires
# EVENT_ACHIEVEMENT_UNLOCKED exactly once.
_GOOD_GRADE_STREAK_MILESTONES = (5, 10, 20)
_STREAK_DAY_MILESTONES = (7, 30, 90)

# Human-readable Polish titles carried in the event payload
# (`{{ trigger.event.data.title }}`). Achievements are this integration's
# own invention - Librus has no such concept, so there's no "real" name to
# resolve from account data the way EVENT_NEW_GRADE etc. resolve a subject
# name - hardcoded Polish, matching every blueprint's own briefing/digest
# text elsewhere in this codebase (this integration is Poland-only by
# nature, Librus itself being Polish-schools-only).
_ACHIEVEMENT_TITLES: dict[str, str] = {
    "first_six": "Pierwsza szóstka!",
    "good_grade_streak_5": "5 dobrych ocen z rzędu",
    "good_grade_streak_10": "10 dobrych ocen z rzędu",
    "good_grade_streak_20": "20 dobrych ocen z rzędu",
    "attendance_streak_7": "Tydzień bez nieobecności",
    "attendance_streak_30": "Miesiąc bez nieobecności",
    "attendance_streak_90": "3 miesiące bez nieobecności",
    "behaviour_streak_7": "Tydzień bez uwagi",
    "behaviour_streak_30": "Miesiąc bez uwagi",
    "behaviour_streak_90": "3 miesiące bez uwagi",
}


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
        self._cached_subjects: dict[int | str, str] = {}
        self._cached_teachers: dict[int | str, str] = {}
        self._cached_classrooms: dict[int | str, str] = {}
        self._cached_lesson_subjects: dict[int, int] = {}
        # Kindergarten (przedszkole) accounts - issue #5 / PR #8. Their
        # standard `Timetables` 403s; the real timetable lives in a separate
        # `/gateway/ms/kindergartens/...` API keyed by the CHILD's LID
        # (`LID-AUTH-USER-...`), which has to be discovered - see
        # `_async_maybe_discover_kindergarten`. `None` = not a kindergarten
        # account (or not discovered yet).
        self._kindergarten_lid: str | None = None
        self._kindergarten_group_id: str | None = None
        self._kindergarten_source: str | None = None
        self._kindergarten_next_discovery: datetime | None = None
        # Set by `_fetch_timetable_or_unpublished` on a confirmed 403 - the
        # ONLY trigger for kindergarten discovery, so an ordinary account
        # (whose Timetables works) never makes a single extra request.
        self._timetable_forbidden = False
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
        # Attendances ids are usually int-able but not always (a "t"-prefixed
        # id like "t41685" has been observed live) - see AttendanceData.id.
        self._known_absence_ids: set[int | str] | None = None
        # Synthetic "date|period|kind|subject" signatures for cancelled /
        # substitution lessons - not a real id from the API, just enough to
        # not re-fire EVENT_TIMETABLE_CHANGED for a disruption already seen.
        self._known_timetable_disruptions: set[str] | None = None
        # Achievement keys already unlocked (e.g. "good_grade_streak_10") -
        # same seed-silently-then-union pattern as every set above, applied
        # to a small fixed vocabulary of milestones instead of growing API
        # ids. See _check_achievements.
        self._known_achievements: set[str] | None = None

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

    @property
    def is_kindergarten(self) -> bool:
        """Whether this account's timetable comes from the kindergarten API."""
        return self._kindergarten_lid is not None

    @property
    def kindergarten_diagnostics(self) -> dict[str, Any]:
        """Kindergarten discovery state for `diagnostics.py` - no LIDs."""
        return {
            "detected": self._kindergarten_lid is not None,
            "source": self._kindergarten_source,
            "group_known": self._kindergarten_group_id is not None,
            "timetable_forbidden": self._timetable_forbidden,
            "next_discovery": (
                self._kindergarten_next_discovery.isoformat()
                if self._kindergarten_next_discovery
                else None
            ),
        }

    @property
    def reference_data_fetched_at(self) -> datetime | None:
        """When `_async_refresh_reference_data` last completed (`None` if
        never yet, e.g. right after setup). Public for `diagnostics.py` -
        reference data (subjects/teachers/school/class/...) is only
        refreshed at most once every 24h, so this tells a diagnostics
        reader whether it's looking at genuinely fresh data or something
        cached from up to a day ago."""
        return self._reference_data_fetched_at

    @property
    def degraded_endpoints(self) -> dict[str, datetime]:
        """Snapshot of `_optional_endpoint_first_failure` - label -> the
        timestamp it started failing (cleared on recovery). Public so
        `diagnostics.py` can surface it: for an account where several
        endpoints are degrading (a confirmed-403 module-unavailable case,
        or a genuinely flaky one), this is the single most direct answer
        to "what's actually going on" - a `{}` here means every degradable
        endpoint succeeded on the last cycle. A copy, not the live dict,
        so a diagnostics consumer can't accidentally mutate coordinator
        state.

        Covers all four groups that can degrade to empty instead of
        failing the whole cycle: OPTIONAL_ENDPOINT_LABELS (tier 2 of the
        core fetch), CORE_ENDPOINT_LABELS (tier 1), REFERENCE_DATA_
        ENDPOINT_LABELS, and MISC_DEGRADABLE_ENDPOINT_LABELS (Timetable/
        LuckyNumbers/Messages/Messages-Secondary, each guarding its own
        call outside any shared gather). BUG FIX (live feedback, issue
        #5's account): the last three groups were NOT covered when this
        property was first added - only OPTIONAL_ENDPOINT_LABELS/
        CORE_ENDPOINT_LABELS were, which made the very diagnostics dump
        built to debug that account's degraded state genuinely
        incomplete (it couldn't explain why Class was unknown while
        School wasn't, since reference-data failures were silently
        DEBUG-logged only). Should have covered every degrade path from
        the start rather than needing a second round to notice the gap."""
        return dict(self._optional_endpoint_first_failure)

    async def _fetch_timetable_or_unpublished(self, week_start: date) -> dict[str, Any]:
        """Fetch one week's raw `Timetable` payload, treating a CONFIRMED
        403 as "this class's timetable isn't published yet" (issue #4,
        reported live) rather than a session problem - Synergia's own web
        UI shows an explicit "Plan lekcji klasy ... nie został jeszcze
        opublikowany" message for this exact case, and a fresh re-login
        can never fix it (the login itself succeeds fine, as reported). A
        genuine 401 still means the session actually died and is left to
        propagate, so the normal forced-relogin-and-retry-once recovery
        (see `_async_update_data` / `async_fetch_timetable_week`) still
        runs for that case.

        BUG FIX (live feedback, issue #5's account): this predates
        `degraded_endpoints`/the repair-issue tracking and never fed it -
        a persistently-403ing Timetable was invisible in diagnostics even
        though the calendar was correctly showing empty. Now tracked
        under the "Timetable" label, same as every other degrade path.
        Called from both TIER 1 (this week/next week) and the on-demand
        `async_fetch_timetable_week` path - both count as the same
        endpoint for tracking purposes.

        Kindergarten accounts (issue #5 / PR #8): once the child's LID is
        known, the week comes from the kindergarten API instead - its
        `timetableEntries` payload is understood by `merge_timetables`
        directly, so callers don't branch on account type. Same 403 degrade,
        same "Timetable" label."""
        try:
            if self._kindergarten_lid is not None:
                payload = await self._client.async_get_kindergarten_timetable(
                    self._kindergarten_lid, week_start, week_start + timedelta(days=6)
                )
            else:
                payload = await self._client.async_get_timetable(week_start)
        except LibrusSessionExpiredError as err:
            if err.status_code == 403:
                self._note_optional_endpoint_failure("Timetable")
                if self._kindergarten_lid is None:
                    self._timetable_forbidden = True
                return {}
            raise
        self._timetable_forbidden = False
        self._note_optional_endpoint_recovery("Timetable")
        return payload

    async def _async_maybe_discover_kindergarten(self, me_payload: dict[str, Any]) -> bool:
        """Try to find a kindergarten child's LID, returning True only when
        one was newly found (so the caller refetches this cycle's timetable).

        Gated on the standard `Timetables` having just 403'd - a regular
        student account never gets here. Also rate-limited to once per
        `_KINDERGARTEN_DISCOVERY_RETRY`, so a school whose ordinary timetable
        is simply unpublished (issue #4, also a 403) costs a handful of
        requests a day, not every cycle. Every probe is non-fatal: nothing
        here can raise into the update cycle or trigger reauth (the original
        PR #8 version re-raised a 403 from these auxiliary endpoints, which
        the coordinator would have treated as a dead session)."""
        if self._kindergarten_lid is not None or not self._timetable_forbidden:
            return False
        now = dt_util.utcnow()
        if self._kindergarten_next_discovery is not None and now < self._kindergarten_next_discovery:
            return False
        self._kindergarten_next_discovery = now + _KINDERGARTEN_DISCOVERY_RETRY
        if not await self._async_discover_kindergarten(me_payload):
            _LOGGER.debug("Timetables is forbidden and no kindergarten timetable was found")
            return False
        _LOGGER.info(
            "Kindergarten account detected - using the kindergarten timetable API (via %s)",
            self._kindergarten_source,
        )
        # Pick up activity names/classrooms/group in this same cycle rather
        # than up to 24h later.
        self._reference_data_fetched_at = None
        return True

    async def _async_probe(self, coro: Any) -> dict[str, Any]:
        """Await one discovery request, degrading ANY Librus error to `{}`."""
        try:
            result = await coro
        except LibrusError as err:
            _LOGGER.debug("Kindergarten discovery probe failed: %s", err)
            return {}
        return result if isinstance(result, dict) else {}

    async def _async_discover_kindergarten(self, me_payload: dict[str, Any]) -> bool:
        """Collect candidate `LID-AUTH-USER-...` identifiers and keep the
        first one the kindergarten timetable endpoint returns entries for.

        A parent login can expose both the parent's and the child's LID, and
        the timetable endpoint is the decisive check between them (PR #8's
        finding, from Synergia's own web UI). Candidate sources, in order:
        `/Me`, `Auth/TokenInfo` (+ `Auth/UserInfo/<lid>`), and `Users/<id>`
        for the account's own numeric ids."""
        candidates: dict[str, str] = {}  # lid -> where it came from

        def add(values: list[str], source: str) -> None:
            for value in values:
                candidates.setdefault(value, source)

        me = me_payload.get("Me") if isinstance(me_payload, dict) else None
        me = me if isinstance(me, dict) else {}
        add(_collect_lid_user_identifiers(me.get("User")), "Me.User")
        add(_collect_lid_user_identifiers(me), "Me")

        token_info = await self._async_probe(self._client.async_get_token_info())
        token_lid = _extract_token_user_identifier(token_info)
        if token_lid:
            add([token_lid], "Auth/TokenInfo")
            user_info = await self._async_probe(self._client.async_get_user_info(token_lid))
            add(_collect_lid_user_identifiers(user_info), "Auth/UserInfo")

        account = me.get("Account")
        account = account if isinstance(account, dict) else {}
        numeric_ids = [
            value
            for value in (account.get("UserId"), account.get("Id"))
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        ]
        for numeric_id in dict.fromkeys(numeric_ids):
            user_record = await self._async_probe(self._client.async_get_user(numeric_id))
            add(_collect_lid_user_identifiers(user_record), f"Users/{numeric_id}")

        today = dt_util.now().date()
        for lid, source in list(candidates.items())[:_KINDERGARTEN_MAX_CANDIDATES]:
            payload = await self._async_probe(
                self._client.async_get_kindergarten_timetable(
                    lid, today - timedelta(days=30), today + timedelta(days=60)
                )
            )
            entries = payload.get("timetableEntries")
            if not isinstance(entries, list) or not entries:
                continue
            self._kindergarten_lid = lid
            self._kindergarten_source = source
            child = await self._async_probe(self._client.async_get_kindergartener(lid))
            child_data = child.get("data")
            group_id = child_data.get("groupIdentifier") if isinstance(child_data, dict) else None
            self._kindergarten_group_id = group_id if isinstance(group_id, str) and group_id else None
            return True
        self._kindergarten_source = f"not_found ({len(candidates)} candidates)"
        return False

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
            return await self._fetch_timetable_or_unpublished(week_start)
        except LibrusSessionExpiredError:
            await self._client.async_ensure_session_valid(
                self.config_entry.data[CONF_PASSWORD], force=True
            )
            return await self._fetch_timetable_or_unpublished(week_start)

    async def async_fetch_message(self, mailbox: str, message_id: str) -> Any:
        """Fetch one message's full body on demand, for `services.py`'s
        `get_message` service - deliberately outside the coordinator's
        normal poll cycle (see `LibrusApiClient.async_get_message`'s
        docstring for why: CONFIRMED live this marks the message read
        server-side, so it must only ever run on a user's own explicit
        action, never automatically).

        BUG FIX (live feedback, 2026-09-23, same session as the messages-
        polling fixes above): this used to only recover the MAIN Synergia
        session (`async_ensure_session_valid`) on a `LibrusSessionExpiredError`
        - correct for `async_fetch_timetable_week` (Timetable lives on that
        same main session), but `async_get_message` lives on the SEPARATE
        wiadomosci.librus.pl session instead, which this project has now
        confirmed dies independently and far more often. The retry used to
        reuse the same now-stale Wiadomości cookies and fail again, this
        time uncaught - surfacing as a real "couldn't load message" error
        to whoever just clicked a message in a card. Now also forces a
        fresh Wiadomości bootstrap before retrying, mirroring
        `_async_bootstrap_and_fetch_primary_messages`'s own recovery."""
        assert self.config_entry is not None
        try:
            return await self._client.async_get_message(mailbox, message_id)
        except LibrusSessionExpiredError:
            await self._client.async_ensure_session_valid(
                self.config_entry.data[CONF_PASSWORD], force=True
            )
            self._messages_bootstrapped = False
            self._messages_available = await self._client.async_bootstrap_messages()
            self._messages_bootstrapped = True
            return await self._client.async_get_message(mailbox, message_id)

    async def _async_update_data(self) -> LibrusData:
        assert self.config_entry is not None
        # `self.data is not None` guard: the FIRST refresh always runs for
        # real, even if it happens to land inside the quiet-hours window -
        # there's nothing to fall back to yet, and every other coordinator
        # in this codebase expects async_config_entry_first_refresh to
        # actually populate data. Subsequent cycles during the window just
        # return the last-known data unchanged - a normal, supported
        # DataUpdateCoordinator pattern (entities keep their last state,
        # nothing goes stale/unavailable) and skips the network round-trip
        # entirely, not just the parsing - see _in_quiet_hours.
        if self.data is not None and self._in_quiet_hours():
            return self.data
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

        if await self._async_maybe_discover_kindergarten(me_payload):
            try:
                timetable_this_week, timetable_next_week = await asyncio.gather(
                    self._fetch_timetable_or_unpublished(week_start),
                    self._fetch_timetable_or_unpublished(next_week_start),
                )
            except LibrusError as err:
                # Just found - the next cycle fetches it normally.
                _LOGGER.debug("Kindergarten timetable fetch failed right after discovery: %s", err)

        # BUG FIX (code review, 0.7.4): these three used to run sequentially,
        # one `await` after another, even though none of them reads state
        # any of the others writes. Lucky number and reference data both
        # hit the SAME main Synergia domain and are gathered together
        # below.
        #
        # BUG FIX (live feedback, 2026-09-23): messages is deliberately NOT
        # in that same gather (it originally was, briefly, same session as
        # the fix above) - `_async_refresh_reference_data` alone fires 10
        # concurrent requests; bundling the separate wiadomosci.librus.pl
        # bootstrap+fetch into that exact same burst (up to ~15 simultaneous
        # requests sharing one aiohttp session/connector) is a real, live-
        # identified suspect for why that session specifically started
        # dying far more often than observed before - not proven as the
        # sole cause (no pre-2026-09-23 diagnostics exist to compare
        # against), but cheap and safe to remove as a variable regardless.
        # Still concurrent with the OTHER two (not back to fully
        # sequential), just not sharing their exact same burst.
        lucky_number, _ = await asyncio.gather(
            self._async_get_lucky_number(today),
            self._async_refresh_reference_data(),
        )
        messages_result = await self._async_get_messages()
        (
            unread_count,
            unread_by_mailbox,
            messages,
            substitution_messages,
            alert_messages,
            justification_messages,
        ) = messages_result

        me = _parse_me(me_payload)
        grades = _parse_grades(grades_payload, _parse_comment_text_map(grade_comments_payload))
        school_notices = _parse_school_notices(notices_payload)
        notes = _parse_notes(notes_payload)
        homeworks = _parse_homeworks(homeworks_payload)
        attendances = _parse_attendances(attendances_payload)
        attendance_types = _parse_attendance_types(attendance_types_payload)
        timetable = merge_timetables(timetable_this_week, timetable_next_week)
        self._async_fire_new_item_events(
            grades, school_notices, notes, messages, homeworks, me.display_name
        )
        self._fire_timetable_change_events(timetable, today, me.display_name)
        self._fire_new_absence_events(attendances, attendance_types, me.display_name)
        self._check_achievements(grades, attendances, attendance_types, notes, today, me.display_name)

        return LibrusData(
            me=me,
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
            lesson_subjects=self._cached_lesson_subjects,
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

    def _in_quiet_hours(self) -> bool:
        """Whether `dt_util.now()` currently falls inside the configured
        quiet-hours window (off by default - see CONF_QUIET_HOURS_ENABLED).
        Handles a window that wraps midnight (e.g. 23:00 -> 06:00, the
        default) the same way any "overnight range" check has to: it's
        NOT simply start <= now <= end once start > end."""
        if not self._feature_enabled(CONF_QUIET_HOURS_ENABLED, DEFAULT_QUIET_HOURS_ENABLED):
            return False
        start = self._option_time(CONF_QUIET_HOURS_START, DEFAULT_QUIET_HOURS_START)
        end = self._option_time(CONF_QUIET_HOURS_END, DEFAULT_QUIET_HOURS_END)
        now = dt_util.now().time()
        if start <= end:
            return start <= now < end
        return now >= start or now < end

    def _option_time(self, key: str, default: str) -> time:
        raw = self.config_entry.options.get(key, default) if self.config_entry else default
        return dt_util.parse_time(raw) or dt_util.parse_time(default)

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
        genuinely load-bearing sensors; TIER 2 is the newer supplementary
        endpoints. Both now use `return_exceptions=True` and degrade a
        CONFIRMED 403 to empty (see `_degrade_core_payload`/
        `_degrade_optional_payload`) - a genuine 401 anywhere still
        propagates and still drives the forced-relogin-and-retry-once
        logic in `_async_update_data`, unchanged.

        `Me` is deliberately fetched separately, BEFORE either tier, and
        stays fully fatal on any failure (401 or 403) - see
        CORE_ENDPOINT_LABELS' own comment for why.

        BUG FIX (issue #5, reported live): TIER 1 used to be one plain
        `asyncio.gather()` with no `return_exceptions=True` at all - a
        CONFIRMED 403 on `Attendances/Types` (a preschool-account login
        that only has the Wiadomości module enabled) took down the whole
        setup, even though it just meant "this account doesn't have the
        attendance module", the exact same class of thing `Timetables`
        already got this treatment for once (issue #4). Generalized to
        the whole tier now, not just Timetables, since the next limited-
        access account type would otherwise just report the same bug
        again with a different endpoint name.

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

        me_payload = await self._client.async_get_me()

        core_results = await asyncio.gather(
            self._client.async_get_grades(),
            self._client.async_get_grade_categories(),
            self._client.async_get_notes(),
            self._client.async_get_attendances(),
            self._client.async_get_attendance_types(),
            self._fetch_timetable_or_unpublished(week_start),
            self._fetch_timetable_or_unpublished(next_week_start),
            self._client.async_get_homeworks(),
            self._maybe(announcements_enabled, self._client.async_get_school_notices),
            return_exceptions=True,
        )
        # A genuine 401 anywhere in this tier means the session actually
        # died - propagate it immediately (before degrading any 403s)
        # so the existing forced-relogin-and-retry-once recovery still
        # runs exactly as before. A dead session can plausibly 403
        # unrelated endpoints too in the same broken cycle, so it's not
        # safe to interpret THOSE as "confirmed module-unavailable" once
        # a real 401 has shown up anywhere in the same batch.
        for result in core_results:
            if isinstance(result, LibrusSessionExpiredError) and result.status_code == 401:
                raise result
        core = (
            me_payload,
            *(
                self._degrade_core_payload(label, result)
                for label, result in zip(CORE_ENDPOINT_LABELS, core_results)
            ),
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

    def _degrade_core_payload(
        self, label: str, result: dict[str, Any] | BaseException
    ) -> dict[str, Any]:
        """Turn one TIER-1 `return_exceptions=True` gather result into a
        payload, degrading a CONFIRMED 403 to an empty dict - "this
        account/school type doesn't have this module" (issue #5, see
        CORE_ENDPOINT_LABELS' own comment for the full story). Every
        parser fed from TIER 1 already treats an empty payload the same
        as a genuinely empty account, so this is the same degrade path
        `_degrade_optional_payload` uses for TIER 2, just narrower:

        Unlike TIER 2 (genuinely optional endpoints, where ANY LibrusError
        is safe to swallow), TIER 1 is the load-bearing tier - only a
        CONFIRMED 403 (`LibrusSessionExpiredError` specifically, not any
        LibrusError) is treated as "module unavailable". A 401 never
        reaches here at all (the caller re-raises any 401 across the
        whole tier before calling this, see `_async_fetch_core_payloads`).
        A connection error, an unexpected-shape response, or any other
        LibrusError subtype still fails the whole cycle - those aren't a
        confirmed "this module doesn't exist for this account" signal,
        just a transient or genuinely-wrong-shaped failure that's worth
        surfacing (and retrying next cycle) rather than silently hiding.

        Shares the same repair-issue tracking as `_degrade_optional_payload`
        (`_note_optional_endpoint_failure`/`_note_optional_endpoint_recovery`)
        - a persistently-403ing core endpoint is just as worth a "hasn't
        responded in over a week" repair issue as a supplementary one."""
        if isinstance(result, BaseException):
            if isinstance(result, LibrusSessionExpiredError) and result.status_code == 403:
                _LOGGER.debug(
                    "Core endpoint '%s' returned a confirmed 403 (module "
                    "likely unavailable for this account/school) - "
                    "degrading to empty instead of failing the whole cycle: %s",
                    label,
                    result,
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
            self._note_optional_endpoint_failure("LuckyNumbers")
            return self._cached_lucky_number
        self._note_optional_endpoint_recovery("LuckyNumbers")
        lucky = _parse_lucky_number(payload)
        if lucky is not None:
            self._cached_lucky_number = lucky
            self._lucky_number_fetched_date = today
        return self._cached_lucky_number

    def _degrade_reference_result(
        self, label: str, result: dict[str, Any] | BaseException
    ) -> dict[str, Any]:
        """Turn one `return_exceptions=True` reference-data gather result
        into a payload, degrading a LibrusError to `{}` (which every
        parser below already treats the same as a genuinely empty account)
        instead of letting one failing endpoint wipe out the other nine's
        already-successful results too - the exact same all-or-nothing
        gather bug `_degrade_optional_payload` fixed for the core-data
        fetch's own supplementary tier (code review), applied here to
        reference data instead. Anything that ISN'T a LibrusError (a real
        bug, or asyncio.CancelledError) is re-raised, same as there.

        Also feeds the SAME `degraded_endpoints`/repair-issue tracking as
        `_degrade_optional_payload`/`_degrade_core_payload` - see
        `REFERENCE_DATA_ENDPOINT_LABELS`' own comment (const.py) for why
        this wasn't wired in originally and why that was a real gap."""
        if isinstance(result, BaseException):
            if isinstance(result, LibrusError):
                _LOGGER.debug(
                    "Reference-data endpoint '%s' fetch failed (non-fatal): %s",
                    label,
                    result,
                    exc_info=result,
                )
                self._note_optional_endpoint_failure(label)
                return {}
            raise result
        self._note_optional_endpoint_recovery(label)
        return result

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

        BUG FIX (code review): this used to be ONE plain `asyncio.gather()`
        (no `return_exceptions=True`) wrapped in a single try/except - one
        of these 10 endpoints failing raised and discarded the other nine's
        already-successful results too, and the whole refresh was skipped
        for this cycle (retried again next cycle, hammering all 10 every
        time until they all happen to succeed together). None of this is
        core data (same "supplementary" classification `_async_fetch_core_
        payloads`' own TIER 2 already uses), so it's now fetched with
        `return_exceptions=True` and each result degraded independently via
        `_degrade_reference_result` - one endpoint failing only empties
        THAT ONE cache for this cycle, never blocks the other nine, and
        `_reference_data_fetched_at` still advances (this is deliberately a
        24h-cached "confirmed real, empty" degrade, not a per-cycle retry -
        a permanently-broken endpoint no longer gets hammered every single
        coordinator cycle forever).
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
        results = await asyncio.gather(
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
            self._client.async_get_lessons(),
            return_exceptions=True,
        )
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
            lessons_payload,
        ) = (
            self._degrade_reference_result(label, result)
            for label, result in zip(REFERENCE_DATA_ENDPOINT_LABELS, results)
        )
        self._cached_subjects = _parse_id_name_map(subjects_payload, ("Subjects",))
        self._cached_teachers = _parse_id_name_map(teachers_payload, ("Users", "Teachers"))
        self._cached_classrooms = _parse_id_name_map(classrooms_payload, ("Classrooms",))
        self._cached_lesson_subjects = _parse_lesson_subjects(lessons_payload)
        self._cached_school = _parse_school(schools_payload)
        self._cached_class = _parse_class(classes_payload)
        if self._kindergarten_lid is not None:
            await self._async_refresh_kindergarten_reference_data(teachers_payload)
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

    async def _async_refresh_kindergarten_reference_data(
        self, teachers_payload: dict[str, Any]
    ) -> None:
        """Merge kindergarten lookups into the ordinary ones (PR #8's
        findings from Synergia's web UI): activity types act as subjects,
        `Auth/Classrooms` identifiers map to room symbols, teachers match
        `Users[].AccountId` (the LIDs in `timetableEntries[].teachers`), and
        the child's group stands in for the class."""
        group_id = self._kindergarten_group_id
        results = await asyncio.gather(
            self._client.async_get_kindergarten_activity_types(),
            self._client.async_get_kindergarten_classrooms(),
            self._maybe(
                group_id is not None,
                lambda: self._client.async_get_kindergarten_group(group_id),
            ),
            return_exceptions=True,
        )
        activity_payload, classrooms_payload, group_payload = (
            self._degrade_reference_result(label, result)
            for label, result in zip(
                ("Kindergarten/ActivityTypes", "Kindergarten/Classrooms", "Kindergarten/Group"),
                results,
            )
        )
        self._cached_subjects.update(_parse_kindergarten_activity_types(activity_payload))
        self._cached_classrooms.update(_parse_kindergarten_classrooms(classrooms_payload))
        self._cached_teachers.update(_parse_kindergarten_teachers(teachers_payload))
        group = _parse_kindergarten_group(group_payload)
        if group is not None:
            self._cached_class = group

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

    async def _async_bootstrap_and_fetch_primary_messages(
        self,
    ) -> tuple[int, dict[str, int], list[MessageData]]:
        """Ensure the Wiadomości session is bootstrapped (respecting
        `_messages_bootstrapped` - skipped if already done this login), then
        fetch the primary inbox/unread-count data. Raises `LibrusError` on
        ANY genuine failure (bootstrap or fetch) so the caller can retry;
        does NOT set `_messages_bootstrapped = False` itself on failure -
        that's the caller's call to make (it needs to happen exactly once
        across a normal-attempt-then-retry pair, not once per attempt).

        A clean `async_bootstrap_messages() == False` (module not enabled
        for this school) is NOT an error - sets `_messages_available =
        False` and returns an empty result instead of raising, same as
        always."""
        if not self._messages_bootstrapped:
            self._messages_available = await self._client.async_bootstrap_messages()
            self._messages_bootstrapped = True
        if not self._messages_available:
            return 0, {}, []
        unread_payload, inbox_payload = await asyncio.gather(
            self._client.async_get_unread_messages_count(),
            self._client.async_get_messages(limit=10),
        )
        return _parse_messages(unread_payload, inbox_payload)

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

        BUG FIX (live feedback, 2026-09-23 - "też tak kurwa mam", reproduced
        on the maintainer's own account too): `_messages_bootstrapped` used
        to only ever get set `True`, never back to `False` - so once EITHER
        the bootstrap call OR the primary fetch below raised a real
        `LibrusError`, messages stayed silently empty FOREVER, confirmed
        live via a real account's own `last_reported` advancing on schedule
        while `last_updated` stayed frozen on the stale empty result. Only
        a full HA restart/reload cleared it. Fixed by resetting the flag on
        failure so the NEXT cycle gets a fresh bootstrap attempt.

        BUG FIX #2 (same day, same live account, confirmed by direct
        repeated observation): fixing #1 alone still left a real, visible
        gap - the dedicated wiadomosci.librus.pl session turned out to die
        far more often than expected (repeatedly, well within an hour, on
        a real account - unlike the main Synergia session's own ~20h
        lifetime), so "retry next cycle" meant the sensor could sit empty
        for however long the poll interval is. The main session has had an
        IMMEDIATE same-cycle retry for this exact class of problem since
        v0.4.2 (forced relogin + retry once, before ever surfacing a gap to
        the user) - messages never got the equivalent. `_async_bootstrap_
        and_fetch_primary` below is now called up to twice in a row: once
        normally, once more immediately if that raised, mirroring the main
        session's own proven pattern instead of waiting out a whole poll
        cycle.
        """
        if self.config_entry is not None and not self.config_entry.options.get(
            CONF_MESSAGES_ENABLED, DEFAULT_MESSAGES_ENABLED
        ):
            # Turned off in the options flow - don't bootstrap the separate
            # wiadomosci.librus.pl session or make any messages calls.
            self._messages_available = False
            return 0, {}, [], [], [], []

        try:
            unread_count, unread_by_mailbox, inbox_messages = (
                await self._async_bootstrap_and_fetch_primary_messages()
            )
        except LibrusError:
            _LOGGER.debug(
                "Messages primary fetch failed - retrying immediately with a "
                "fresh bootstrap before giving up for this cycle",
                exc_info=True,
            )
            # Same "force a fresh attempt, retry once, never more than once
            # per cycle" shape as _async_update_data's own recovery for the
            # main session - the dedicated wiadomosci session may have died
            # independently of it.
            self._messages_bootstrapped = False
            try:
                unread_count, unread_by_mailbox, inbox_messages = (
                    await self._async_bootstrap_and_fetch_primary_messages()
                )
            except LibrusError:
                _LOGGER.debug("Messages primary fetch failed again after retry (non-fatal)", exc_info=True)
                self._note_optional_endpoint_failure("Messages")
                # Still leave a fresh bootstrap scheduled for NEXT cycle too,
                # in case this keeps failing beyond just one retry.
                self._messages_bootstrapped = False
                return 0, {}, [], [], [], []
        if not self._messages_available:
            return 0, {}, [], [], [], []
        self._note_optional_endpoint_recovery("Messages")

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
            self._note_optional_endpoint_recovery("Messages/Secondary")
        except LibrusError:
            _LOGGER.debug(
                "Secondary mailbox (substitutions/alerts/justifications) fetch failed (non-fatal)",
                exc_info=True,
            )
            self._note_optional_endpoint_failure("Messages/Secondary")

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
        student: str,
    ) -> None:
        entry_id = self.config_entry.entry_id if self.config_entry else None
        # Resolved names are included alongside the raw ids so an automation
        # (e.g. a notification blueprint) can use {{ trigger.event.data.
        # subject }} directly, without its own lookup against the sensor
        # attributes just to say which subject/teacher a grade or note was
        # about. `student` (the resolved child's name, not the login/parent's -
        # see MeData) is included the same way for a multi-child household's
        # blueprint to say WHOSE grade/note/etc. this is, since one blueprint
        # instance's action runs for every config entry that fires the event.
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
            student=student,
        )
        self._known_notice_ids = self._fire_for_new_ids(
            EVENT_NEW_ANNOUNCEMENT,
            entry_id,
            self._known_notice_ids,
            {n.id: {"subject": n.subject} for n in notices},
            student=student,
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
            student=student,
        )
        self._known_message_ids = self._fire_for_new_ids(
            EVENT_NEW_MESSAGE,
            entry_id,
            self._known_message_ids,
            {m.id: {"sender": m.sender_name, "topic": m.topic} for m in messages},
            student=student,
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
            student=student,
        )

    def _fire_timetable_change_events(
        self, timetable: dict[date, list[LessonData]], today: date, student: str
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
            EVENT_TIMETABLE_CHANGED,
            entry_id,
            self._known_timetable_disruptions,
            items,
            student=student,
        )

    def _fire_new_absence_events(
        self,
        attendances: list[AttendanceData],
        attendance_types: dict[int, AttendanceTypeData],
        student: str,
    ) -> None:
        """Fire EVENT_NEW_ABSENCE for a newly-seen real absence record
        (any non-presence type - excused or not, `excused` in the payload
        says which). Seeded silently on the first sync like the other
        events."""
        entry_id = self.config_entry.entry_id if self.config_entry else None
        items: dict[int | str, dict[str, Any]] = {}
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
            EVENT_NEW_ABSENCE, entry_id, self._known_absence_ids, items, student=student
        )

    def _check_achievements(
        self,
        grades: list[GradeData],
        attendances: list[AttendanceData],
        attendance_types: dict[int, AttendanceTypeData],
        notes: list[NoteData],
        today: date,
        student: str,
    ) -> None:
        """Fires EVENT_ACHIEVEMENT_UNLOCKED for a handful of objective,
        data-derived gamification milestones - deliberately never an
        invented points/scoring system, which would have no basis in
        anything Librus actually reports and would feel arbitrary/made up.

        Reuses `_fire_for_new_ids` exactly like every other event above -
        each achievement KEY (e.g. "good_grade_streak_10") is treated as
        an "item id" that's either currently unlocked or not, seeded
        silently on the first sync, and unioned (not replaced) so nothing
        re-fires once achieved even if the underlying streak later
        resets (a bad grade breaking a 10-grade streak must not "revoke"
        the achievement already earned)."""
        entry_id = self.config_entry.entry_id if self.config_entry else None
        unlocked: set[str] = set()

        day_to_day_grades = [
            g
            for g in grades
            if not g.is_semester_proposition
            and not g.is_final_proposition
            and not g.is_semester
            and not g.is_final
        ]
        if any(parse_grade_value(g.value) == 6.0 for g in day_to_day_grades):
            unlocked.add("first_six")

        streak = good_grade_streak(grades)
        for milestone in _GOOD_GRADE_STREAK_MILESTONES:
            if streak >= milestone:
                unlocked.add(f"good_grade_streak_{milestone}")

        attendance_days = days_since_last_absence(
            attendances, attendance_types, self._cached_class, today
        )
        if attendance_days is not None:
            for milestone in _STREAK_DAY_MILESTONES:
                if attendance_days >= milestone:
                    unlocked.add(f"attendance_streak_{milestone}")

        behaviour_days = days_since_last_negative_note(notes, self._cached_class, today)
        if behaviour_days is not None:
            for milestone in _STREAK_DAY_MILESTONES:
                if behaviour_days >= milestone:
                    unlocked.add(f"behaviour_streak_{milestone}")

        self._known_achievements = self._fire_for_new_ids(
            EVENT_ACHIEVEMENT_UNLOCKED,
            entry_id,
            self._known_achievements,
            {key: {"title": _ACHIEVEMENT_TITLES[key]} for key in unlocked},
            student=student,
        )

    def _fire_for_new_ids(
        self,
        event: str,
        entry_id: str | None,
        known: set[Any] | None,
        items: dict[Any, dict[str, Any]],
        *,
        student: str | None = None,
    ) -> set[Any]:
        current_ids = set(items)
        if known is None:
            # First-ever refresh for this entry: seed silently. Firing here
            # would replay the account's whole history as "new" on install.
            return current_ids
        new_ids = current_ids - known
        for item_id in new_ids:
            self.hass.bus.async_fire(
                event, {"entry_id": entry_id, "id": item_id, "student": student, **items[item_id]}
            )
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
        # BUG FIX (code review): `bool(item.get("CountToTheAverage", True))`
        # only applied the `True` default when the KEY was absent - an
        # explicit JSON `null` resolved to `bool(None)` == False. Same
        # "present but null" failure mode already hit and fixed once for
        # Users[].FirstName (see const.py's/CLAUDE.md's "Empirically
        # confirmed" notes) - an explicit null must default to True
        # ("counts unless explicitly told False"), same as a missing key.
        raw_count_to_average = item.get("CountToTheAverage")
        count_to_average = True if raw_count_to_average is None else bool(raw_count_to_average)
        # BUG FIX (code review): `int(item.get("Weight") or 1)` silently
        # coerced a legitimate API `Weight: 0` to `1` via Python's
        # falsy-zero evaluation (`0 or 1` == `1`) - only a genuinely
        # absent/None Weight should default to 1.
        raw_weight = item.get("Weight")
        weight = int(raw_weight) if raw_weight is not None else 1
        result[item_id] = GradeCategoryData(
            id=item_id,
            name=item.get("Name", ""),
            count_to_average=count_to_average,
            weight=weight,
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
                # See GradeData.is_semester/is_final's own docstring - the
                # ACTUAL semester/year grade, distinct from the proposed
                # one above. Not confirmed live yet (no real semester-end
                # data on the test account), but confirmed via the
                # reference parser's own field names.
                is_semester=bool(item.get("IsSemester")),
                is_final=bool(item.get("IsFinal")),
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
        raw_id = item["Id"]
        try:
            item_id: int | str = int(raw_id)
        except (TypeError, ValueError):
            # CONFIRMED live: some records use a "t"-prefixed id (e.g.
            # "t41685") instead of a plain numeric one - see AttendanceData.
            item_id = str(raw_id)
        raw_type_id = type_.get("Id")
        type_id: int | str | None
        if raw_type_id is None:
            type_id = None
        else:
            try:
                type_id = int(raw_type_id)
            except (TypeError, ValueError):
                # BUG FIX (code review): same defensive fallback as the
                # sibling `id` field above - not confirmed live for Type.Id
                # specifically, but this API has already proven the record's
                # own Id can be "t"-prefixed, so Type.Id could plausibly do
                # the same someday. A raw str here just misses
                # attendance_types.get(...) (keyed by int) and is treated
                # as an unknown type, instead of crashing the whole
                # coordinator update.
                type_id = str(raw_type_id)
        attendances.append(
            AttendanceData(
                id=item_id,
                lesson_id=lesson.get("Id"),
                lesson_no=item.get("LessonNo"),
                date=item.get("Date"),
                semester=item.get("Semester"),
                type_id=type_id,
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
    teacher_id = _as_int(teacher.get("Id"))
    return LessonData(
        lesson_no=_as_int(raw.get("LessonNo")),
        hour_from=raw.get("HourFrom"),
        hour_to=raw.get("HourTo"),
        subject_id=_as_int(subject.get("Id")),
        teacher_id=teacher_id,
        teacher_ids=(teacher_id,) if teacher_id is not None else (),
        classroom_id=_as_int(classroom.get("Id")),
        is_canceled=bool(raw.get("IsCanceled")),
        is_substitution=bool(raw.get("IsSubstitutionClass")),
    )


def _merge_kindergarten_entries(
    entries: list[Any], result: dict[date, list[LessonData]]
) -> None:
    """Add kindergarten `timetableEntries` (PR #8) to `result`.

    Unlike `Timetables`, identifiers are LID strings
    (`activityTypeIdentifier`, `classroomIdentifier`, `teachers[]`) and
    there's no lesson number - entries are plain time blocks, so
    `lesson_no` stays None."""
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        try:
            day = date.fromisoformat(str(raw.get("date"))[:10])
        except ValueError:
            continue
        activity_id = raw.get("activityTypeIdentifier")
        classroom_id = raw.get("classroomIdentifier")
        raw_teachers = raw.get("teachers")
        teacher_ids = tuple(
            str(value)
            for value in (raw_teachers if isinstance(raw_teachers, list) else ())
            if isinstance(value, (str, int)) and str(value)
        )
        # Only "planned" has been seen live; the rest is a best guess.
        entry_type = str(raw.get("type") or "planned").lower()
        result.setdefault(day, []).append(
            LessonData(
                lesson_no=None,
                hour_from=raw.get("startTime"),
                hour_to=raw.get("endTime"),
                subject_id=str(activity_id) if activity_id is not None else None,
                teacher_id=teacher_ids[0] if teacher_ids else None,
                classroom_id=str(classroom_id) if classroom_id is not None else None,
                is_canceled="cancel" in entry_type,
                is_substitution="substitut" in entry_type,
                teacher_ids=teacher_ids,
            )
        )


def merge_timetables(*payloads: dict[str, Any]) -> dict[date, list[LessonData]]:
    """Merge one or more `Timetables?weekStart=...` responses into a single
    date-keyed dict of lessons.

    CONFIRMED live: each date maps to a list of PERIOD SLOTS (one per
    lesson-number, always the same length even on days with no school),
    each itself a list of 0+ lesson dicts (more than one when a period is
    split into parallel groups, e.g. two language classes at once) - NOT a
    flat list of lessons per day as the reverse-engineered spec assumed.

    Also accepts the kindergarten API's `timetableEntries` payload (see
    `_merge_kindergarten_entries`), so the calendar/coordinator don't need
    to know which kind of account they're serving.
    """
    result: dict[date, list[LessonData]] = {}
    for payload in payloads:
        entries = payload.get("timetableEntries")
        if isinstance(entries, list):
            _merge_kindergarten_entries(entries, result)
            continue
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


def _collect_lid_user_identifiers(value: Any) -> list[str]:
    """Every `LID-AUTH-USER-...` string anywhere in a payload, in order."""
    found: list[str] = []
    if isinstance(value, str):
        if value.startswith(_LID_USER_PREFIX):
            found.append(value)
    elif isinstance(value, dict):
        for nested in value.values():
            found.extend(_collect_lid_user_identifiers(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_collect_lid_user_identifiers(nested))
    return list(dict.fromkeys(found))


def _extract_token_user_identifier(payload: dict[str, Any]) -> str | None:
    for key in ("UserIdentifier", "userIdentifier", "Identifier", "identifier"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith(_LID_USER_PREFIX):
            return value
    return None


def _parse_kindergarten_activity_types(payload: dict[str, Any]) -> dict[int | str, str]:
    """`kindergartens/activities-types` -> identifier: name ("Religia"...)."""
    items = payload.get("activitiesTypes")
    if not isinstance(items, list):
        return {}
    result: dict[int | str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        identifier = item.get("identifier")
        name = item.get("name")
        if isinstance(identifier, str) and identifier and isinstance(name, str) and name:
            result[identifier] = name
    return result


def _parse_kindergarten_teachers(payload: dict[str, Any]) -> dict[int | str, str]:
    """`Users` keyed by `AccountId` - what kindergarten `teachers[]` holds."""
    items = payload.get("Users")
    if not isinstance(items, list):
        return {}
    result: dict[int | str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        identifier = item.get("AccountId")
        if not isinstance(identifier, (str, int)) or isinstance(identifier, bool):
            continue
        name = f"{item.get('FirstName') or ''} {item.get('LastName') or ''}".strip()
        if name:
            result[str(identifier)] = name
    return result


def _parse_kindergarten_classrooms(payload: dict[str, Any]) -> dict[int | str, str]:
    """`Auth/Classrooms` -> identifier: room name.

    Prefers `name` ("sala 1") over the bare `symbol` ("1") - reported on a
    live kindergarten account (0.7.8-beta.1) that the symbol alone reads as
    a meaningless number in the cards, which show the room verbatim."""
    items = payload.get("data")
    if not isinstance(items, list):
        return {}
    result: dict[int | str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        identifier = item.get("identifier")
        room = item.get("name") or item.get("symbol")
        if isinstance(identifier, (str, int)) and room:
            result[str(identifier)] = str(room)
    return result


def _parse_kindergarten_group(payload: dict[str, Any]) -> ClassData | None:
    """A kindergarten group mapped onto the class model (name + first tutor)."""
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        return None
    tutors = payload.get("tutors")
    tutor_id = next(
        (value for value in tutors if isinstance(value, str) and value),
        None,
    ) if isinstance(tutors, list) else None
    return ClassData(
        number=None,
        symbol=name,
        tutor_id=tutor_id,
        begin_school_year=None,
        end_first_semester=None,
        end_school_year=None,
    )


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


def resolve_sender_name(payload: dict[str, Any]) -> str:
    """Resolve a message's sender display name from a `senderName` field,
    falling back to `senderFirstName`/`senderLastName` combined when it's
    absent/empty - CONFIRMED live to be the identical shape on both the
    mailbox list endpoints (`_parse_message_list` below) AND the single
    full-message endpoint (`services.py`'s `get_message` handler).

    Extracted (code review) - this exact ~3-line resolution used to be
    duplicated between the two call sites. Public (not underscore-
    prefixed) so `services.py` can reuse it: coordinator.py must never
    import from services.py, so the shared helper has to live on this
    side of that one-way dependency (same rule `parse_grade_value`'s
    docstring above documents for sensor.py)."""
    return payload.get("senderName") or (
        f"{payload.get('senderFirstName', '')} {payload.get('senderLastName', '')}".strip()
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
        sender_name = resolve_sender_name(item)
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


def _parse_lesson_subjects(payload: dict[str, Any]) -> dict[int, int]:
    """lesson_id -> subject_id, from `Lessons` (CONFIRMED live 2026-09-23 -
    see const.py's ENDPOINT_LESSONS note). Used to resolve which subject an
    `AttendanceData.lesson_id` belongs to."""
    items = payload.get("Lessons")
    if not isinstance(items, list):
        return {}
    result: dict[int, int] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        subject = item.get("Subject") or {}
        subject_id = subject.get("Id")
        if subject_id is not None:
            result[int(item["Id"])] = int(subject_id)
    return result
