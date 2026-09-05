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
    ENDPOINT_CLASSROOMS,
    ENDPOINT_GRADE_CATEGORIES,
    ENDPOINT_GRADES,
    ENDPOINT_HOMEWORKS,
    ENDPOINT_LUCKY_NUMBERS,
    ENDPOINT_ME,
    ENDPOINT_NOTES,
    ENDPOINT_SCHOOL_NOTICES,
    ENDPOINT_SUBJECTS,
    ENDPOINT_TEACHERS,
    ENDPOINT_TIMETABLES,
    LOGIN_HEADERS,
    MAX_OAUTH_REDIRECTS,
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

    async def async_ensure_session_valid(self, password: str) -> None:
        """Log in only if the assumed session lifetime has elapsed."""
        if self.is_session_valid():
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
        url = f"{DATA_BASE_URL}/{endpoint}"
        try:
            async with self._session.get(
                url, headers={"User-Agent": USER_AGENT}, params=params
            ) as response:
                if response.status == 503:
                    raise LibrusServerMaintenanceError(
                        f"Librus is under maintenance (HTTP 503) on {endpoint}."
                    )
                payload = await self._async_read_json(response)
                if response.status in (401, 403):
                    raise LibrusInvalidCredentialsError(
                        f"Session rejected on {endpoint} (HTTP {response.status})."
                    )
                if response.status >= 400:
                    raise LibrusUnexpectedResponseError(
                        f"HTTP {response.status} from {endpoint}: {payload!r}"
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
