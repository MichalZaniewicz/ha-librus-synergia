"""Config flow for the Librus Synergia (unofficial) integration.

Credential model: unlike a bearer-token OAuth API, this integration's
session (see librus_api.LibrusSessionData) is a cookie-based login with an
observed ~24h lifetime and no separate refresh grant. Silent, unattended
daily re-login therefore requires the password itself, not just a revocable
token - so, unlike ha-suunto's "password used once then discarded" model,
the password IS persisted here (alongside the session cookies, which matter
for staying recognized as a known device - see librus_api/const.py). This is
disclosed to the user in the setup form's description.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_COOKIES,
    CONF_SESSION_LOGGED_IN_AT,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
)
from .librus_api import (
    LibrusAccountActionRequiredError,
    LibrusApiClient,
    LibrusCaptchaRequiredError,
    LibrusConnectionError,
    LibrusError,
    LibrusInvalidCredentialsError,
    LibrusUnexpectedResponseError,
)

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
USER_SCHEMA = vol.Schema(
    {
        # Librus calls this field "login" (e.g. "1234567u"), not an email or
        # username in the usual sense - label follows Librus's own wording.
        vol.Required(CONF_USERNAME): TextSelector(TextSelectorConfig()),
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    }
)


class LibrusSynergiaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the login/password config + reauth flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow state."""
        self._reauth_username: str | None = None

    async def _login(self, username: str, password: str) -> dict[str, Any]:
        """Authenticate once, fetch a display name, and shape entry data.

        Returns ``{"data": ..., "title": ..., "unique_id": ...}``. Raises one
        of the `librus_api` exceptions on failure.
        """
        session = async_get_clientsession(self.hass)
        client = LibrusApiClient(session, username)
        session_data = await client.async_login(password)

        title = username
        unique_id = username.lower()
        try:
            me_payload = await client.async_get_me()
            me = me_payload.get("Me", {})
            account = me.get("Account", {})
            # `Account` is the LOGIN's own identity - for a child's login
            # under a parent-managed portal this is the PARENT's name
            # (confirmed live: Account was "Michał Zaniewicz", the parent,
            # while `User` below was "Kacper Zaniewicz", the actual
            # student) - the student ("User") is what the title/device name
            # should show, not whoever's name is on the login itself.
            student = me.get("User", {})
            student_name = f"{student.get('FirstName', '')} {student.get('LastName', '')}".strip()
            if student_name:
                title = f"E-dziennik {student_name}"
            else:
                account_name = f"{account.get('FirstName', '')} {account.get('LastName', '')}".strip()
                if account_name:
                    title = f"E-dziennik {account_name}"
            account_id = account.get("Id")
            if account_id is not None:
                unique_id = str(account_id)
        except LibrusError:
            # A friendly title/stable id is a nice-to-have, never fatal -
            # the login above already succeeded.
            _LOGGER.debug("Could not fetch a display name for the new entry", exc_info=True)

        return {
            "data": {
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
                CONF_COOKIES: session_data.cookies,
                CONF_SESSION_LOGGED_IN_AT: session_data.logged_in_at,
            },
            "title": title,
            "unique_id": unique_id,
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect login/password, authenticate once, store the session."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            try:
                info = await self._login(username, user_input[CONF_PASSWORD])
            except LibrusCaptchaRequiredError:
                errors["base"] = "captcha_needed"
            except LibrusAccountActionRequiredError:
                errors["base"] = "account_action_required"
            except LibrusInvalidCredentialsError:
                errors["base"] = "invalid_auth"
            except LibrusConnectionError:
                errors["base"] = "cannot_connect"
            except (LibrusUnexpectedResponseError, LibrusError):
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info["unique_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info["title"], data=info["data"])

        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauth when the stored session is no longer valid."""
        self._reauth_username = entry_data.get(CONF_USERNAME)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again and refresh the session."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        username = self._reauth_username or reauth_entry.data[CONF_USERNAME]

        if user_input is not None:
            try:
                info = await self._login(username, user_input[CONF_PASSWORD])
            except LibrusCaptchaRequiredError:
                errors["base"] = "captcha_needed"
            except LibrusAccountActionRequiredError:
                errors["base"] = "account_action_required"
            except LibrusInvalidCredentialsError:
                errors["base"] = "invalid_auth"
            except LibrusConnectionError:
                errors["base"] = "cannot_connect"
            except (LibrusUnexpectedResponseError, LibrusError):
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={**reauth_entry.data, **info["data"]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}),
            description_placeholders={"login": username},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> LibrusSynergiaOptionsFlow:
        """Return the options flow handler."""
        return LibrusSynergiaOptionsFlow()


class LibrusSynergiaOptionsFlow(OptionsFlow):
    """Handle the polling-interval option."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL_MINUTES,
                        max=MAX_SCAN_INTERVAL_MINUTES,
                        step=5,
                        unit_of_measurement="min",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
