"""Tests for LibrusApiClient against a mocked HTTP layer (aioresponses).

Deliberately does not depend on Home Assistant or pytest-homeassistant-
custom-component - this suite only needs aiohttp + aioresponses, so it can
run without a full HA install. The full HA-integrated test suite (config
flow, coordinator, entities) is a follow-up item - see the project notes.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
import pytest
from aioresponses import aioresponses
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
    LibrusUnexpectedResponseError,
)

AUTHORIZATION_REDIRECT_URL = (
    "https://api.librus.pl/OAuth/Authorization?client_id=46&response_type=code"
    "&scope=mydata&state=fakestate123"
)


def _mock_successful_login(session: aiohttp.ClientSession, mocked: aioresponses) -> None:
    mocked.get(
        SYNERGIA_PORTAL_LOGIN_URL,
        status=302,
        headers={"Location": AUTHORIZATION_REDIRECT_URL},
    )
    mocked.get(AUTHORIZATION_REDIRECT_URL, status=200, body="")
    mocked.post(
        API_OAUTH_AUTHORIZATION_URL,
        status=200,
        payload={"status": "ok", "goTo": "/OAuth/Authorization/2FA?client_id=46"},
    )
    hop1 = urljoin(API_OAUTH_AUTHORIZATION_WITH_SCOPE_URL, "/OAuth/Authorization/2FA?client_id=46")
    hop2 = "https://api.librus.pl/OAuth/Authorization/PerformLogin?client_id=46"
    final = "https://synergia.librus.pl/loguj/portalRodzina?code=abc&state=fakestate123"
    mocked.get(hop1, status=302, headers={"Location": hop2})
    mocked.get(hop2, status=302, headers={"Location": final})
    mocked.get(final, status=200, body="<html>logged in</html>")
    # aioresponses (0.7.9, against aiohttp 3.10) doesn't drive aiohttp's real
    # Set-Cookie -> cookie-jar pipeline the way a live server response does
    # (confirmed live against the real Librus servers on 2026-09-05 that
    # this DOES happen for real) - seed the jar directly here to exercise
    # the client's post-login cookie-jar reads without fighting the mocking
    # library's gap.
    session.cookie_jar.update_cookies(
        {"oauth_token": "faketoken123"}, response_url=URL("https://synergia.librus.pl/")
    )


@pytest.mark.asyncio
async def test_login_success_sets_session_valid() -> None:
    async with aiohttp.ClientSession() as session:
        with aioresponses() as mocked:
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
        with aioresponses() as mocked:
            mocked.get(
                SYNERGIA_PORTAL_LOGIN_URL,
                status=302,
                headers={"Location": AUTHORIZATION_REDIRECT_URL},
            )
            mocked.get(AUTHORIZATION_REDIRECT_URL, status=200, body="")
            mocked.post(
                API_OAUTH_AUTHORIZATION_URL,
                status=200,
                payload={"status": "error"},
            )
            client = LibrusApiClient(session, "1234567u")
            with pytest.raises(LibrusInvalidCredentialsError):
                await client.async_login("wrong-password")

    assert not client.is_session_valid()


@pytest.mark.asyncio
async def test_login_captcha_marker_raises() -> None:
    async with aiohttp.ClientSession() as session:
        with aioresponses() as mocked:
            mocked.get(
                SYNERGIA_PORTAL_LOGIN_URL,
                status=302,
                headers={"Location": AUTHORIZATION_REDIRECT_URL},
            )
            mocked.get(AUTHORIZATION_REDIRECT_URL, status=200, body="")
            mocked.post(
                API_OAUTH_AUTHORIZATION_URL,
                status=200,
                body='{"status": "error", "message": "please solve the recaptcha"}',
                content_type="application/json",
            )
            client = LibrusApiClient(session, "1234567u")
            with pytest.raises(LibrusCaptchaRequiredError):
                await client.async_login("whatever")


@pytest.mark.asyncio
async def test_get_grades_after_login() -> None:
    async with aiohttp.ClientSession() as session:
        with aioresponses() as mocked:
            _mock_successful_login(session, mocked)
            client = LibrusApiClient(session, "1234567u")
            await client.async_login("correct-password")

            mocked.get(
                f"{DATA_BASE_URL}/Grades",
                status=200,
                payload={"Grades": [{"Id": 1, "Grade": "5", "Category": {"Id": 10}}]},
            )
            payload = await client.async_get_grades()

    assert payload["Grades"][0]["Grade"] == "5"


@pytest.mark.asyncio
async def test_maintenance_response_raises() -> None:
    async with aiohttp.ClientSession() as session:
        with aioresponses() as mocked:
            _mock_successful_login(session, mocked)
            client = LibrusApiClient(session, "1234567u")
            await client.async_login("correct-password")

            mocked.get(f"{DATA_BASE_URL}/Grades", status=503, body="")
            with pytest.raises(LibrusServerMaintenanceError):
                await client.async_get_grades()


@pytest.mark.asyncio
async def test_non_json_response_raises_unexpected() -> None:
    async with aiohttp.ClientSession() as session:
        with aioresponses() as mocked:
            _mock_successful_login(session, mocked)
            client = LibrusApiClient(session, "1234567u")
            await client.async_login("correct-password")

            mocked.get(f"{DATA_BASE_URL}/Grades", status=200, body="<html>not json</html>")
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
