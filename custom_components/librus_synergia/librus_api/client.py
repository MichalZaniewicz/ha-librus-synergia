"""Async client for Librus's Synergia/API gateway.

See const.py's module docstring for why this is a cookie/login-form flow
(reverse-engineered from `emsi/librus_pyapi`, MIT) rather than the dead
OAuth password grant szkolny-android documented. Confirmed live on
2026-09-05 to complete without a captcha challenge for a normal login.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urljoin

import aiohttp
from yarl import URL

from .const import (
    API_OAUTH_AUTHORIZATION_URL,
    API_OAUTH_AUTHORIZATION_WITH_SCOPE_URL,
    ASSUMED_SESSION_LIFETIME_SECONDS,
    DATA_BASE_URL,
    ENDPOINT_ATTENDANCE_TYPES,
    ENDPOINT_ATTENDANCES,
    ENDPOINT_BEHAVIOUR_GRADES_POINTS,
    ENDPOINT_BEHAVIOUR_GRADES_POINTS_CATEGORIES,
    ENDPOINT_BEHAVIOUR_GRADES_POINTS_COMMENTS,
    ENDPOINT_CLASSES,
    ENDPOINT_CLASSROOMS,
    ENDPOINT_CLASS_FREE_DAYS,
    ENDPOINT_DESCRIPTIVE_GRADES,
    ENDPOINT_GRADE_CATEGORIES,
    ENDPOINT_GRADE_COMMENTS,
    ENDPOINT_GRADE_TYPES,
    ENDPOINT_GRADES,
    ENDPOINT_HOMEWORK_ASSIGNMENTS,
    ENDPOINT_HOMEWORK_CATEGORIES,
    ENDPOINT_HOMEWORKS,
    ENDPOINT_LUCKY_NUMBERS,
    ENDPOINT_ME,
    ENDPOINT_NOTE_CATEGORIES,
    ENDPOINT_NOTES,
    ENDPOINT_PARENT_TEACHER_CONFERENCES,
    ENDPOINT_POINT_GRADES,
    ENDPOINT_SCHOOL_FREE_DAYS,
    ENDPOINT_SCHOOL_NOTICES,
    ENDPOINT_SCHOOLS,
    ENDPOINT_SUBJECTS,
    ENDPOINT_TEACHERS,
    ENDPOINT_TEXT_GRADES,
    ENDPOINT_TIMETABLES,
    ENDPOINT_UNITS,
    ENDPOINT_VIRTUAL_CLASSES,
    LOGIN_HEADERS,
    MAX_OAUTH_REDIRECTS,
    MESSAGES_ACCESS_DENIED_MARKER,
    MESSAGES_BASE_URL,
    MESSAGES_BOOTSTRAP_URL,
    OAUTH_TOKEN_COOKIE,
    PERSISTED_COOKIE_NAMES,
    SESSION_EXPIRY_SAFETY_MARGIN_SECONDS,
    SYNERGIA_DOMAIN,
    SYNERGIA_PORTAL_LOGIN_URL,
    USER_AGENT,
)
from .exceptions import (
    LibrusCaptchaRequiredError,
    LibrusConnectionError,
    LibrusInvalidCredentialsError,
    LibrusServerMaintenanceError,
    LibrusSessionExpiredError,
    LibrusUnexpectedResponseError,
)

_CAPTCHA_MARKERS = ("captcha", "recaptcha", "g-recaptcha", "hcaptcha")


@dataclass(slots=True)
class LibrusSessionData:
    """A snapshot of the current session, for the caller to persist across
    Home Assistant restarts. Re-sending the same cookies (especially the
    long-lived DeviceCookie) on a later login is believed to be why a normal
    login skips captcha/2FA - see const.py's module docstring."""

    cookies: list[dict[str, str]] = field(default_factory=list)
    logged_in_at: float = 0.0  # epoch seconds


OnSessionUpdate = Callable[[LibrusSessionData], "Awaitable[None] | None"]


class LibrusApiClient:
    """Thin async wrapper around Librus's Synergia/API gateway.

    Never owns its own `aiohttp.ClientSession` - the caller (Home Assistant's
    `async_get_clientsession(hass)`) provides one, so this also plays nicely
    with `pytest-homeassistant-custom-component`'s `aioclient_mock` fixture.

    Unlike a bearer-token API, this client is inherently stateful in the
    session's cookie jar. It does not cache the password - callers must pass
    it to `async_ensure_session_valid`/`async_login` each time a (re)login is
    needed, and are responsible for deciding whether to persist it (see the
    project's credential-model notes: because this session expires roughly
    daily with no separate refresh grant, silent unattended operation
    requires storing the password, unlike ha-suunto's long-lived session
    key).
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        *,
        on_session_update: OnSessionUpdate | None = None,
    ) -> None:
        self._session = session
        self._username = username
        self._on_session_update = on_session_update
        self._logged_in_at: float = 0.0

    @property
    def username(self) -> str:
        return self._username

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def import_session(self, session_data: LibrusSessionData | None) -> None:
        """Re-inject previously-persisted cookies into the shared session's
        cookie jar, e.g. right after a Home Assistant restart."""
        if session_data is None:
            return
        self._logged_in_at = session_data.logged_in_at
        by_domain: dict[str, dict[str, str]] = {}
        for cookie in session_data.cookies:
            by_domain.setdefault(cookie["domain"], {})[cookie["name"]] = cookie["value"]
        for domain, cookies in by_domain.items():
            self._session.cookie_jar.update_cookies(cookies, response_url=URL(f"https://{domain}/"))

    def _export_session(self) -> LibrusSessionData:
        exported: list[dict[str, str]] = []
        for domain, names in PERSISTED_COOKIE_NAMES.items():
            jar_cookies = self._session.cookie_jar.filter_cookies(URL(f"https://{domain}/"))
            for name in names:
                morsel = jar_cookies.get(name)
                if morsel is not None:
                    exported.append({"name": name, "value": morsel.value, "domain": domain})
        return LibrusSessionData(cookies=exported, logged_in_at=self._logged_in_at)

    def is_session_valid(self) -> bool:
        if self._logged_in_at <= 0:
            return False
        age = time.time() - self._logged_in_at
        return age < (ASSUMED_SESSION_LIFETIME_SECONDS - SESSION_EXPIRY_SAFETY_MARGIN_SECONDS)

    async def async_ensure_session_valid(self, password: str, *, force: bool = False) -> None:
        """Log in if the assumed session lifetime has elapsed, or always if
        `force=True` - used by the coordinator to recover from a
        `LibrusSessionExpiredError` (Librus rejected the session earlier
        than our own elapsed-time estimate expected, see that exception's
        docstring)."""
        if not force and self.is_session_valid():
            return
        await self.async_login(password)

    async def async_login(self, password: str) -> LibrusSessionData:
        """Run the full login handshake (portalRodzina -> Authorization form
        POST -> manual redirect chain), confirming a session cookie was set.

        Raises one of the `librus_api` exceptions on failure. On success,
        persists the resulting cookies via `on_session_update` (if set) and
        returns them too.
        """
        try:
            resp = await self._session.get(SYNERGIA_PORTAL_LOGIN_URL, allow_redirects=False)
            authorization_url = resp.headers.get("Location")
            if not authorization_url:
                raise LibrusUnexpectedResponseError(
                    "Login step 1 (portalRodzina) returned no redirect Location."
                )

            # Sets API-side session cookies; the response body isn't used.
            await self._session.get(authorization_url, allow_redirects=False)

            data = {"action": "login", "login": self._username, "pass": password}
            resp = await self._session.post(
                API_OAUTH_AUTHORIZATION_URL, data=data, headers=LOGIN_HEADERS
            )
            text = await resp.text()
            self._raise_if_captcha(text, "login form response")
            try:
                login_response = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError) as err:
                raise LibrusUnexpectedResponseError(
                    f"Login response wasn't JSON: {text[:200]!r}"
                ) from err
            go_to = login_response.get("goTo") if isinstance(login_response, dict) else None
            if not go_to:
                # UNVERIFIED trigger condition for a genuinely wrong
                # password - see exceptions.py's module docstring.
                raise LibrusInvalidCredentialsError(f"Login rejected: {login_response!r}")

            current_url = urljoin(API_OAUTH_AUTHORIZATION_WITH_SCOPE_URL, go_to)
            for _ in range(MAX_OAUTH_REDIRECTS):
                resp = await self._session.get(current_url, allow_redirects=False)
                text = await resp.text()
                self._raise_if_captcha(text, "OAuth redirect chain")
                location = resp.headers.get("Location")
                if not location:
                    break
                current_url = urljoin(str(resp.url), location)
            else:
                raise LibrusUnexpectedResponseError("OAuth redirect chain exceeded the hop limit.")
        except aiohttp.ClientError as err:
            raise LibrusConnectionError(str(err)) from err

        jar_cookies = self._session.cookie_jar.filter_cookies(URL(f"https://{SYNERGIA_DOMAIN}/"))
        if OAUTH_TOKEN_COOKIE not in jar_cookies:
            raise LibrusUnexpectedResponseError(
                "Login appeared to finish but no oauth_token session cookie was set."
            )

        self._logged_in_at = time.time()
        session_data = self._export_session()
        if self._on_session_update is not None:
            result = self._on_session_update(session_data)
            if result is not None:
                await result
        return session_data

    @staticmethod
    def _raise_if_captcha(text: str, where: str) -> None:
        low = text.lower()
        if any(marker in low for marker in _CAPTCHA_MARKERS):
            raise LibrusCaptchaRequiredError(f"Captcha marker seen in {where}.")

    # ------------------------------------------------------------------
    # Data endpoints
    # ------------------------------------------------------------------

    async def _async_request(
        self, endpoint: str, *, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return await self._async_request_url(f"{DATA_BASE_URL}/{endpoint}", params=params)

    async def _async_request_url(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Same as `_async_request` but for a fully-formed URL, not just an
        endpoint under the Synergia gateway - needed for the separate
        Wiadomości subsystem, which lives on its own domain."""
        try:
            async with self._session.get(
                url, headers={"User-Agent": USER_AGENT}, params=params
            ) as response:
                if response.status == 503:
                    raise LibrusServerMaintenanceError(
                        f"Librus is under maintenance (HTTP 503) on {url}."
                    )
                payload = await self._async_read_json(response)
                if response.status in (401, 403):
                    # Distinct from LibrusInvalidCredentialsError (which
                    # means the login handshake itself was rejected) - this
                    # means an already-established session died mid-cycle,
                    # which the stored password can very likely fix without
                    # asking the user anything. See LibrusSessionExpiredError.
                    raise LibrusSessionExpiredError(
                        f"Session rejected on {url} (HTTP {response.status})."
                    )
                if response.status >= 400:
                    raise LibrusUnexpectedResponseError(
                        f"HTTP {response.status} from {url}: {payload!r}"
                    )
        except aiohttp.ClientError as err:
            raise LibrusConnectionError(str(err)) from err
        return payload

    @staticmethod
    async def _async_read_json(response: aiohttp.ClientResponse) -> dict[str, Any]:
        try:
            data = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError) as err:
            text = await response.text()
            raise LibrusUnexpectedResponseError(
                f"Non-JSON response (HTTP {response.status}): {text[:200]!r}"
            ) from err
        if isinstance(data, list):
            # CONFIRMED live (2026-09-06): at least one Wiadomości mailbox's
            # list endpoint ("substitutions" and/or "alerts") returns a
            # bare JSON array instead of the {"data": [...]} envelope every
            # other endpoint in this client uses - normalize instead of
            # raising, so one differently-shaped secondary mailbox doesn't
            # take the whole request down (was surfacing as
            # LibrusUnexpectedResponseError: "Expected a JSON object, got
            # list", which - before coordinator.py isolated the two
            # fetches - silently wiped out the otherwise-working inbox
            # unread-count/message-list data too, via a shared
            # asyncio.gather()).
            return {"data": data}
        if not isinstance(data, dict):
            raise LibrusUnexpectedResponseError(
                f"Expected a JSON object, got {type(data).__name__}"
            )
        return data

    async def async_get_me(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_ME)

    async def async_get_grades(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_GRADES)

    async def async_get_grade_categories(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_GRADE_CATEGORIES)

    async def async_get_notes(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_NOTES)

    async def async_get_attendances(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_ATTENDANCES)

    async def async_get_attendance_types(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_ATTENDANCE_TYPES)

    async def async_get_timetable(self, week_start: date) -> dict[str, Any]:
        return await self._async_request(
            ENDPOINT_TIMETABLES, params={"weekStart": week_start.isoformat()}
        )

    async def async_get_homeworks(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_HOMEWORKS)

    async def async_get_school_notices(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_SCHOOL_NOTICES)

    async def async_get_lucky_number(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_LUCKY_NUMBERS)

    async def async_get_subjects(self) -> dict[str, Any]:
        """UNVERIFIED endpoint name - see scripts/manual_smoke_test.py."""
        return await self._async_request(ENDPOINT_SUBJECTS)

    async def async_get_teachers(self) -> dict[str, Any]:
        """UNVERIFIED endpoint name - see scripts/manual_smoke_test.py."""
        return await self._async_request(ENDPOINT_TEACHERS)

    async def async_get_classrooms(self) -> dict[str, Any]:
        """UNVERIFIED endpoint name - see scripts/manual_smoke_test.py."""
        return await self._async_request(ENDPOINT_CLASSROOMS)

    async def async_get_homework_assignments(self) -> dict[str, Any]:
        """See const.py's note on ENDPOINT_HOMEWORK_ASSIGNMENTS."""
        return await self._async_request(ENDPOINT_HOMEWORK_ASSIGNMENTS)

    async def async_get_schools(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_SCHOOLS)

    async def async_get_classes(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_CLASSES)

    async def async_get_virtual_classes(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_VIRTUAL_CLASSES)

    async def async_get_school_free_days(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_SCHOOL_FREE_DAYS)

    async def async_get_class_free_days(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_CLASS_FREE_DAYS)

    async def async_get_homework_categories(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_HOMEWORK_CATEGORIES)

    async def async_get_parent_teacher_conferences(self) -> dict[str, Any]:
        """Wired into LibrusAgendaCalendar as a defensive extra merge - see
        ParentTeacherConferenceData's docstring."""
        return await self._async_request(ENDPOINT_PARENT_TEACHER_CONFERENCES)

    async def async_get_grade_types(self) -> dict[str, Any]:
        """Reference data confirming every valid `Grade` value string Librus
        uses (numeric 1-6 with +/- modifiers, plus non-numeric status marks
        like "bz"/"np"/"zw") - not currently consumed anywhere, kept for
        diagnostics/future use now that it's confirmed real."""
        return await self._async_request(ENDPOINT_GRADE_TYPES)

    async def async_get_note_categories(self) -> dict[str, Any]:
        """CONFIRMED live (2026-09-06) with real, populated data - wired
        into the coordinator's reference-data refresh."""
        return await self._async_request(ENDPOINT_NOTE_CATEGORIES)

    async def async_get_behaviour_grade_points(self) -> dict[str, Any]:
        """"Ocena zachowania" (formal behaviour grade) - distinct from
        Notes ("uwagi"). CONFIRMED reachable, empty on this account so
        far - wired into LibrusBehaviourGradeSensor via coordinator.py's
        core-data fetch."""
        return await self._async_request(ENDPOINT_BEHAVIOUR_GRADES_POINTS)

    async def async_get_behaviour_grade_point_categories(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_BEHAVIOUR_GRADES_POINTS_CATEGORIES)

    async def async_get_behaviour_grade_point_comments(self) -> dict[str, Any]:
        return await self._async_request(ENDPOINT_BEHAVIOUR_GRADES_POINTS_COMMENTS)

    async def async_get_grade_comments(self) -> dict[str, Any]:
        """CONFIRMED live to be a SEPARATE endpoint from /Grades - see
        const.py's ENDPOINT_GRADE_COMMENTS. Wired into _parse_grades'
        comment-id correlation."""
        return await self._async_request(ENDPOINT_GRADE_COMMENTS)

    async def async_get_units(self) -> dict[str, Any]:
        """School/unit configuration (which grade systems are enabled, bell
        schedule, behaviour-points settings). CONFIRMED real+populated, not
        wired into the coordinator/any entity yet."""
        return await self._async_request(ENDPOINT_UNITS)

    async def async_get_point_grades(self) -> dict[str, Any]:
        """CONFIRMED disabled for this account's school (see Units'
        GradesSettings.PointGradesEnabled) - not wired into any entity."""
        return await self._async_request(ENDPOINT_POINT_GRADES)

    async def async_get_descriptive_grades(self) -> dict[str, Any]:
        """CONFIRMED enabled for this account's school (see Units'
        GradesSettings.DescriptiveGradesEnabled) - wired into
        LibrusDescriptiveGradesSensor."""
        return await self._async_request(ENDPOINT_DESCRIPTIVE_GRADES)

    async def async_get_text_grades(self) -> dict[str, Any]:
        """Enablement for this account's school unknown (no config flag
        seen either way in Units) - not wired into any entity."""
        return await self._async_request(ENDPOINT_TEXT_GRADES)

    # ------------------------------------------------------------------
    # Wiadomości (messages) - a separate subsystem, own domain/session.
    #
    # IMPORTANT: `async_get_message` below (fetching one message's full
    # body via `/{mailbox}/messages/{id}`) is CONFIRMED live (2026-09-06)
    # to mark the message read server-side in the real Librus inbox - the
    # `readDate` field flips from null to a real timestamp immediately
    # after one GET, on a message that stayed unread across many prior
    # LIST-endpoint polls. This is exactly why it is NOT called from the
    # coordinator's routine polling (which only ever uses the list/count
    # endpoints above, matching "listing never marks anything read") -
    # it exists solely for `services.py`'s `get_message` service, invoked
    # only on a user's own deliberate action (clicking a message in a
    # card), same as opening a message in the real Librus app.
    # ------------------------------------------------------------------

    async def async_bootstrap_messages(self) -> bool:
        """One-time-per-login bootstrap for the Wiadomości subsystem.

        Returns False (not an error) if this account's school doesn't have
        the messages module enabled - some don't. Caller decides how often
        to call this (see coordinator.py); this method does no caching.
        """
        try:
            async with self._session.get(
                MESSAGES_BOOTSTRAP_URL, headers={"User-Agent": USER_AGENT}
            ) as response:
                text = await response.text()
        except aiohttp.ClientError as err:
            raise LibrusConnectionError(str(err)) from err
        self._raise_if_captcha(text, "messages bootstrap")
        return MESSAGES_ACCESS_DENIED_MARKER not in text

    async def async_get_unread_messages_count(self, mailbox: str = "inbox") -> dict[str, Any]:
        return await self._async_request_url(f"{MESSAGES_BASE_URL}/{mailbox}/unreadMessagesCount")

    async def async_get_messages(
        self, mailbox: str = "inbox", *, limit: int = 10, unread_only: bool = False
    ) -> dict[str, Any]:
        params = {"limit": str(limit)}
        if unread_only:
            params["unreadOnly"] = "1"
        return await self._async_request_url(
            f"{MESSAGES_BASE_URL}/{mailbox}/messages", params=params
        )

    async def async_get_message(self, mailbox: str, message_id: str) -> dict[str, Any]:
        """Fetch ONE message's full, untruncated body.

        CONFIRMED live (2026-09-06): real response root key is `"data"`,
        full content lives in a base64-encoded `"Message"` field (note the
        capital M - distinct from the list endpoint's lowercase `content`
        key). See the big comment above this method's section for why this
        marks the message read and must only be called from deliberate
        user action.
        """
        return await self._async_request_url(f"{MESSAGES_BASE_URL}/{mailbox}/messages/{message_id}")
