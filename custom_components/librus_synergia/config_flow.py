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
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)

from .const import (
    CONF_ANNOUNCEMENTS_ENABLED,
    CONF_BEHAVIOUR_GRADES_ENABLED,
    CONF_COOKIES,
    CONF_DESCRIPTIVE_GRADES_ENABLED,
    CONF_FREE_DAYS_ENABLED,
    CONF_MESSAGES_ENABLED,
    CONF_QUIET_HOURS_ENABLED,
    CONF_QUIET_HOURS_END,
    CONF_QUIET_HOURS_START,
    CONF_SESSION_LOGGED_IN_AT,
    DEFAULT_ANNOUNCEMENTS_ENABLED,
    DEFAULT_BEHAVIOUR_GRADES_ENABLED,
    DEFAULT_DESCRIPTIVE_GRADES_ENABLED,
    DEFAULT_FREE_DAYS_ENABLED,
    DEFAULT_MESSAGES_ENABLED,
    DEFAULT_QUIET_HOURS_ENABLED,
    DEFAULT_QUIET_HOURS_END,
    DEFAULT_QUIET_HOURS_START,
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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user fix their login/password from the entry's own
        "Reconfigure" menu item, without deleting and re-adding the whole
        entry (which would lose the entity ids/history/dashboard
        references/automations built on top of it). Reauth (above) only
        ever triggers automatically on a session failure and only ever
        asks for the password again - this covers the "I want to fix a
        typo'd login" or "I changed my Librus password on purpose" cases,
        proactively, from the UI.

        The login field IS editable here (unlike reauth) since a typo'd
        login is exactly one of the two things this step exists to fix -
        `_abort_if_unique_id_mismatch` below is the safety net that stops
        someone from accidentally reconfiguring this entry into pointing
        at a genuinely different Librus account."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

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
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    title=info["title"],
                    data={**reconfigure_entry.data, **info["data"]},
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=reconfigure_entry.data[CONF_USERNAME]
                    ): TextSelector(TextSelectorConfig()),
                    vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> LibrusSynergiaOptionsFlow:
        """Return the options flow handler."""
        return LibrusSynergiaOptionsFlow()


class LibrusSynergiaOptionsFlow(OptionsFlow):
    """Poll interval + which optional feature groups to fetch."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL_MINUTES,
                        max=MAX_SCAN_INTERVAL_MINUTES,
                        step=5,
                        unit_of_measurement="min",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_MESSAGES_ENABLED,
                    default=options.get(CONF_MESSAGES_ENABLED, DEFAULT_MESSAGES_ENABLED),
                ): BooleanSelector(),
                vol.Required(
                    CONF_ANNOUNCEMENTS_ENABLED,
                    default=options.get(CONF_ANNOUNCEMENTS_ENABLED, DEFAULT_ANNOUNCEMENTS_ENABLED),
                ): BooleanSelector(),
                vol.Required(
                    CONF_BEHAVIOUR_GRADES_ENABLED,
                    default=options.get(
                        CONF_BEHAVIOUR_GRADES_ENABLED, DEFAULT_BEHAVIOUR_GRADES_ENABLED
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_DESCRIPTIVE_GRADES_ENABLED,
                    default=options.get(
                        CONF_DESCRIPTIVE_GRADES_ENABLED, DEFAULT_DESCRIPTIVE_GRADES_ENABLED
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_FREE_DAYS_ENABLED,
                    default=options.get(CONF_FREE_DAYS_ENABLED, DEFAULT_FREE_DAYS_ENABLED),
                ): BooleanSelector(),
                vol.Required(
                    CONF_QUIET_HOURS_ENABLED,
                    default=options.get(CONF_QUIET_HOURS_ENABLED, DEFAULT_QUIET_HOURS_ENABLED),
                ): BooleanSelector(),
                # The two fields below are only meaningful while the toggle
                # above is on - shown unconditionally regardless (options
                # flow forms have no native conditional-field visibility),
                # same tradeoff already accepted for the feature toggles.
                vol.Required(
                    CONF_QUIET_HOURS_START,
                    default=options.get(CONF_QUIET_HOURS_START, DEFAULT_QUIET_HOURS_START),
                ): TimeSelector(),
                vol.Required(
                    CONF_QUIET_HOURS_END,
                    default=options.get(CONF_QUIET_HOURS_END, DEFAULT_QUIET_HOURS_END),
                ): TimeSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
