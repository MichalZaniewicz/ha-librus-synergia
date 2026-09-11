"""Tests for the Librus Synergia config flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.librus_synergia.const import (
    CONF_ANNOUNCEMENTS_ENABLED,
    CONF_BEHAVIOUR_GRADES_ENABLED,
    CONF_COOKIES,
    CONF_DESCRIPTIVE_GRADES_ENABLED,
    CONF_FREE_DAYS_ENABLED,
    CONF_MESSAGES_ENABLED,
    CONF_QUIET_HOURS_ENABLED,
    CONF_QUIET_HOURS_END,
    CONF_QUIET_HOURS_START,
    DOMAIN,
)
from custom_components.librus_synergia.librus_api import (
    LibrusAccountActionRequiredError,
    LibrusCaptchaRequiredError,
    LibrusConnectionError,
    LibrusInvalidCredentialsError,
)

from .conftest import build_mock_client

USER_INPUT = {CONF_USERNAME: "1234567u", CONF_PASSWORD: "correct-password"}


async def test_user_flow_success(hass) -> None:
    client = build_mock_client()
    with patch(
        "custom_components.librus_synergia.config_flow.LibrusApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM

        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    # Title comes from Me.User (the student), not Me.Account (the login
    # owner) - see config_flow.py's _login docstring/comment.
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "E-dziennik Kacper Zaniewicz"
    assert result["data"][CONF_USERNAME] == "1234567u"
    assert result["data"][CONF_PASSWORD] == "correct-password"
    assert result["data"][CONF_COOKIES]


async def test_user_flow_duplicate_aborts(hass) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="3461991", data=USER_INPUT).add_to_hass(hass)
    client = build_mock_client()
    with patch(
        "custom_components.librus_synergia.config_flow.LibrusApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("exception", "error_key"),
    [
        (LibrusInvalidCredentialsError("bad"), "invalid_auth"),
        (LibrusCaptchaRequiredError("captcha"), "captcha_needed"),
        (LibrusAccountActionRequiredError("action"), "account_action_required"),
        (LibrusConnectionError("net"), "cannot_connect"),
    ],
)
async def test_user_flow_error_mapping(hass, exception, error_key) -> None:
    client = build_mock_client()
    client.async_login.side_effect = exception
    with patch(
        "custom_components.librus_synergia.config_flow.LibrusApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error_key}


async def test_reauth_flow_success(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="3461991", data=USER_INPUT)
    entry.add_to_hass(hass)
    client = build_mock_client()

    with patch(
        "custom_components.librus_synergia.config_flow.LibrusApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
                "unique_id": entry.unique_id,
            },
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"


async def test_reauth_flow_still_captcha_gated(hass) -> None:
    """A reauth attempt that hits captcha again must show that specific
    reason, not a generic "wrong password" message."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="3461991", data=USER_INPUT)
    entry.add_to_hass(hass)
    client = build_mock_client()
    client.async_login.side_effect = LibrusCaptchaRequiredError("still blocked")

    with patch(
        "custom_components.librus_synergia.config_flow.LibrusApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
                "unique_id": entry.unique_id,
            },
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "whatever"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "captcha_needed"}


async def test_reconfigure_flow_updates_password(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="3461991", data=USER_INPUT)
    entry.add_to_hass(hass)
    client = build_mock_client()

    with patch(
        "custom_components.librus_synergia.config_flow.LibrusApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reconfigure"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: USER_INPUT[CONF_USERNAME], CONF_PASSWORD: "new-password"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"


async def test_reconfigure_flow_fixes_login_typo(hass) -> None:
    """The login field is editable here (unlike reauth) - the whole point
    is also covering "I mistyped the login when I first set this up"."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="3461991", data=USER_INPUT)
    entry.add_to_hass(hass)
    client = build_mock_client()

    with patch(
        "custom_components.librus_synergia.config_flow.LibrusApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "1234567u ", CONF_PASSWORD: "correct-password"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    # Stripped, same as the user-flow login field.
    assert entry.data[CONF_USERNAME] == "1234567u"


async def test_reconfigure_flow_aborts_on_different_account(hass) -> None:
    """The unique_id (`Me.Account.Id`, "3461991" for the mocked account) is
    the safety net - reconfiguring must not be a backdoor way to silently
    repoint one entry at a genuinely different Librus account."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="some-other-account", data=USER_INPUT)
    entry.add_to_hass(hass)
    client = build_mock_client()  # ME_PAYLOAD's account id is always 3461991

    with patch(
        "custom_components.librus_synergia.config_flow.LibrusApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "someone-elses-login", CONF_PASSWORD: "whatever"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    # The entry must be untouched - the mismatch is caught before any data
    # update happens.
    assert entry.data[CONF_USERNAME] == "1234567u"


async def test_reconfigure_flow_error_mapping(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="3461991", data=USER_INPUT)
    entry.add_to_hass(hass)
    client = build_mock_client()
    client.async_login.side_effect = LibrusInvalidCredentialsError("bad")

    with patch(
        "custom_components.librus_synergia.config_flow.LibrusApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: USER_INPUT[CONF_USERNAME], CONF_PASSWORD: "wrong"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_options_flow_round_trips_interval_and_messages_toggle(hass) -> None:
    """The other 4 feature toggles aren't touched here, so voluptuous fills
    them in from their own (all-True) defaults - options ends up with every
    field, not just the ones this test explicitly passed."""
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SCAN_INTERVAL: 45, CONF_MESSAGES_ENABLED: False},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_SCAN_INTERVAL: 45,
        CONF_MESSAGES_ENABLED: False,
        CONF_ANNOUNCEMENTS_ENABLED: True,
        CONF_BEHAVIOUR_GRADES_ENABLED: True,
        CONF_DESCRIPTIVE_GRADES_ENABLED: True,
        CONF_FREE_DAYS_ENABLED: True,
        CONF_QUIET_HOURS_ENABLED: False,
        CONF_QUIET_HOURS_START: "23:00:00",
        CONF_QUIET_HOURS_END: "06:00:00",
    }


async def test_options_flow_round_trips_all_feature_toggles(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: 20,
            CONF_MESSAGES_ENABLED: True,
            CONF_ANNOUNCEMENTS_ENABLED: False,
            CONF_BEHAVIOUR_GRADES_ENABLED: False,
            CONF_DESCRIPTIVE_GRADES_ENABLED: False,
            CONF_FREE_DAYS_ENABLED: False,
            CONF_QUIET_HOURS_ENABLED: True,
            CONF_QUIET_HOURS_START: "22:30:00",
            CONF_QUIET_HOURS_END: "07:15:00",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_SCAN_INTERVAL: 20,
        CONF_MESSAGES_ENABLED: True,
        CONF_ANNOUNCEMENTS_ENABLED: False,
        CONF_BEHAVIOUR_GRADES_ENABLED: False,
        CONF_DESCRIPTIVE_GRADES_ENABLED: False,
        CONF_FREE_DAYS_ENABLED: False,
        CONF_QUIET_HOURS_ENABLED: True,
        CONF_QUIET_HOURS_START: "22:30:00",
        CONF_QUIET_HOURS_END: "07:15:00",
    }
