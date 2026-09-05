"""Tests for LibrusApiClient against a directly-mocked aiohttp session.

Deliberately does not depend on Home Assistant or pytest-homeassistant-
custom-component - this suite only needs aiohttp, so it can run without a
full HA install.

Mocks `aiohttp.ClientSession.get`/`.post` directly (a dict of URL -> canned
response) rather than using `aioresponses`: that library's last release
(0.7.9) doesn't support aiohttp>=3.11's `ClientResponse.__init__` signature
change, and pinning aiohttp back to satisfy it drags `pytest-homeassistant-
custom-component`'s own dependency resolution down to a homeassistant
release that predates `ConfigFlowResult` - breaking every OTHER test file
in this suite that imports from `homeassistant.config_entries`. Direct
session mocking has no aiohttp-version dependency at all.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urljoin

import aiohttp
import pytest
from yarl import URL

# See scripts/manual_smoke_test.py for why this is appended, not inserted at
# index 0 (custom_components/librus_synergia also has a calendar.py, which
# would otherwise shadow the stdlib `calendar` module).
sys.path.append(
    str(Path(__file__).resolve().parent.parent / "custom_components" / "librus_synergia")
)

from librus_api.client import LibrusApiClient  # noqa: E402
from librus_api.const import (  # noqa: E402
    API_OAUTH_AUTHORIZATION_URL,
    API_OAUTH_AUTHORIZATION_WITH_SCOPE_URL,
    DATA_BASE_URL,
    SYNERGIA_PORTAL_LOGIN_URL,
)
from librus_api.exceptions import (  # noqa: E402
    LibrusCaptchaRequiredError,
    LibrusInvalidCredentialsError,
    LibrusServerMaintenanceError,
    LibrusSessionExpiredError,
    LibrusUnexpectedResponseError,
)

AUTHORIZATION_REDIRECT_URL = (
    "https://api.librus.pl/OAuth/Authorization?client_id=46&response_type=code"
    "&scope=mydata&state=fakestate123"
)


class _FakeResponse:
    """A minimal stand-in for `aiohttp.ClientResponse` - usable directly as
    its own async context manager, matching what `async with session.get(
    ...) as response:` needs."""

    def __init__(
        self,
        *,
        status: int = 200,
        json_data: Any = None,
        text_data: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.url = URL("https://example.invalid/")
        self._text = json.dumps(json_data) if json_data is not None else text_data

    async def json(self, content_type: str | None = "application/json") -> Any:
        # Mirrors real aiohttp: content_type=None bypasses the content-type
        # check but still parses the body as JSON, raising a plain
        # ValueError (json.JSONDecodeError) if it isn't valid JSON.
        return json.loads(self._text)

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def __await__(self):
        # client.py uses both `resp = await session.get(...)` and
        # `async with session.get(...) as resp:` in different places -
        # real aiohttp's request context manager supports both, so this
        # fake must too.
        async def _self() -> "_FakeResponse":
            return self

        return _self().__await__()


class _MockedSession:
    """Patches `session.get`/`session.post` to look up a canned
    `_FakeResponse` by exact URL, registered ahead of time via `.get()`/
    `.post()` - the same shape the old aioresponses-based tests used."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._gets: dict[str, _FakeResponse] = {}
        self._posts: dict[str, _FakeResponse] = {}
        self._patches = [
            patch.object(session, "get", side_effect=self._handle_get),
            patch.object(session, "post", side_effect=self._handle_post),
        ]

    def get(self, url: str, **kwargs: Any) -> None:
        self._gets[url] = _FakeResponse(**kwargs)

    def post(self, url: str, **kwargs: Any) -> None:
        self._posts[url] = _FakeResponse(**kwargs)

    def _handle_get(self, url: Any, **_kwargs: Any) -> _FakeResponse:
        response = self._gets.get(str(url))
        if response is None:
            raise AssertionError(f"Unexpected GET {url}")
        return response

    def _handle_post(self, url: Any, **_kwargs: Any) -> _FakeResponse:
        response = self._posts.get(str(url))
        if response is None:
            raise AssertionError(f"Unexpected POST {url}")
        return response

    def __enter__(self) -> "_MockedSession":
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        for p in self._patches:
            p.stop()


def _mock_successful_login(session: aiohttp.ClientSession, mocked: _MockedSession) -> None:
    mocked.get(
        SYNERGIA_PORTAL_LOGIN_URL,
        status=302,
        headers={"Location": AUTHORIZATION_REDIRECT_URL},
    )
    mocked.get(AUTHORIZATION_REDIRECT_URL, status=200, text_data="")
    mocked.post(
        API_OAUTH_AUTHORIZATION_URL,
        status=200,
        json_data={"status": "ok", "goTo": "/OAuth/Authorization/2FA?client_id=46"},
    )
    hop1 = urljoin(API_OAUTH_AUTHORIZATION_WITH_SCOPE_URL, "/OAuth/Authorization/2FA?client_id=46")
    hop2 = "https://api.librus.pl/OAuth/Authorization/PerformLogin?client_id=46"
    final = "https://synergia.librus.pl/loguj/portalRodzina?code=abc&state=fakestate123"
    mocked.get(hop1, status=302, headers={"Location": hop2})
    mocked.get(hop2, status=302, headers={"Location": final})
    mocked.get(final, status=200, text_data="<html>logged in</html>")
    # The fake responses above carry no real Set-Cookie handling - seed the
    # jar directly to exercise the client's post-login cookie-jar read
    # (confirmed live against the real Librus servers on 2026-09-05 that a
    # genuine response DOES populate this).
    session.cookie_jar.update_cookies(
        {"oauth_token": "faketoken123"}, response_url=URL("https://synergia.librus.pl/")
    )


@pytest.mark.asyncio
async def test_login_success_sets_session_valid() -> None:
    async with aiohttp.ClientSession() as session:
        with _MockedSession(session) as mocked:
            _mock_successful_login(session, mocked)
            client = LibrusApiClient(session, "1234567u")
            session_data = await client.async_login("correct-password")

    assert client.is_session_valid()
    assert session_data.logged_in_at > 0
    cookie_names = {c["name"] for c in session_data.cookies}
    assert "oauth_token" in cookie_names


@pytest.mark.asyncio
async def test_login_rejected_credentials_raises() -> None:
    async with aiohttp.ClientSession() as session:
        with _MockedSession(session) as mocked:
            mocked.get(
                SYNERGIA_PORTAL_LOGIN_URL,
                status=302,
                headers={"Location": AUTHORIZATION_REDIRECT_URL},
            )
            mocked.get(AUTHORIZATION_REDIRECT_URL, status=200, text_data="")
            mocked.post(API_OAUTH_AUTHORIZATION_URL, status=200, json_data={"status": "error"})
            client = LibrusApiClient(session, "1234567u")
            with pytest.raises(LibrusInvalidCredentialsError):
                await client.async_login("wrong-password")

    assert not client.is_session_valid()


@pytest.mark.asyncio
async def test_login_captcha_marker_raises() -> None:
    async with aiohttp.ClientSession() as session:
        with _MockedSession(session) as mocked:
            mocked.get(
                SYNERGIA_PORTAL_LOGIN_URL,
                status=302,
                headers={"Location": AUTHORIZATION_REDIRECT_URL},
            )
            mocked.get(AUTHORIZATION_REDIRECT_URL, status=200, text_data="")
            mocked.post(
                API_OAUTH_AUTHORIZATION_URL,
                status=200,
                json_data={"status": "error", "message": "please solve the recaptcha"},
            )
            client = LibrusApiClient(session, "1234567u")
            with pytest.raises(LibrusCaptchaRequiredError):
                await client.async_login("whatever")


@pytest.mark.asyncio
async def test_get_grades_after_login() -> None:
    async with aiohttp.ClientSession() as session:
        with _MockedSession(session) as mocked:
            _mock_successful_login(session, mocked)
            client = LibrusApiClient(session, "1234567u")
            await client.async_login("correct-password")

            mocked.get(
                f"{DATA_BASE_URL}/Grades",
                status=200,
                json_data={"Grades": [{"Id": 1, "Grade": "5", "Category": {"Id": 10}}]},
            )
            payload = await client.async_get_grades()

    assert payload["Grades"][0]["Grade"] == "5"


@pytest.mark.asyncio
async def test_data_fetch_session_rejected_raises_session_expired() -> None:
    """CONFIRMED live (2026-09-05): a data endpoint can reject an
    already-established session (HTTP 401/403) - e.g. Librus's real session
    lifetime running shorter than our own conservative elapsed-time
    estimate. This must raise LibrusSessionExpiredError, NOT
    LibrusInvalidCredentialsError - the coordinator treats the two very
    differently (force a silent re-login + retry vs. surface Home
    Assistant's reauth flow to the user)."""
    async with aiohttp.ClientSession() as session:
        with _MockedSession(session) as mocked:
            _mock_successful_login(session, mocked)
            client = LibrusApiClient(session, "1234567u")
            await client.async_login("correct-password")

            mocked.get(f"{DATA_BASE_URL}/Grades", status=401, json_data={})
            with pytest.raises(LibrusSessionExpiredError):
                await client.async_get_grades()


@pytest.mark.asyncio
async def test_maintenance_response_raises() -> None:
    async with aiohttp.ClientSession() as session:
        with _MockedSession(session) as mocked:
            _mock_successful_login(session, mocked)
            client = LibrusApiClient(session, "1234567u")
            await client.async_login("correct-password")

            mocked.get(f"{DATA_BASE_URL}/Grades", status=503, text_data="")
            with pytest.raises(LibrusServerMaintenanceError):
                await client.async_get_grades()


@pytest.mark.asyncio
async def test_non_json_response_raises_unexpected() -> None:
    async with aiohttp.ClientSession() as session:
        with _MockedSession(session) as mocked:
            _mock_successful_login(session, mocked)
            client = LibrusApiClient(session, "1234567u")
            await client.async_login("correct-password")

            mocked.get(f"{DATA_BASE_URL}/Grades", status=200, text_data="<html>not json</html>")
            with pytest.raises(LibrusUnexpectedResponseError):
                await client.async_get_grades()


@pytest.mark.asyncio
async def test_import_session_restores_validity_without_network() -> None:
    """A restored session (e.g. after a HA restart) should be considered
    valid without any request being made, as long as it hasn't aged out."""
    from librus_api.client import LibrusSessionData

    async with aiohttp.ClientSession() as session:
        client = LibrusApiClient(session, "1234567u")
        assert not client.is_session_valid()
        client.import_session(
            LibrusSessionData(
                cookies=[
                    {"name": "oauth_token", "value": "restored", "domain": "synergia.librus.pl"}
                ],
                logged_in_at=time.time(),
            )
        )
        assert client.is_session_valid()
