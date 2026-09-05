"""Exception hierarchy for the Librus API client.

Shaped so a caller's `except LibrusAuthError` cannot accidentally catch
`LibrusUnexpectedResponseError` for a case that isn't really about
credentials - the former means "ask the user to log in again", the latter
means "something about the response shape surprised us".

Note on failure-mode coverage: the happy path (successful login through the
2026-03-28-era Authorization-endpoint flow) is confirmed live. A genuinely
WRONG password was deliberately never tested against a real account (to
avoid tripping any credential-attempt-counting abuse heuristic on someone's
real Librus account), so `LibrusInvalidCredentialsError`'s trigger condition
in `client.py` (a login response with no `goTo` field) is a reasonable
inference, not a confirmed observation - flagged for whoever next hits a
real bad-password case to verify.
"""

from __future__ import annotations


class LibrusError(Exception):
    """Base error for anything the Librus API client raises."""


class LibrusConnectionError(LibrusError):
    """Network/timeout/DNS failure, or an unreachable Librus host. Transient."""


class LibrusServerMaintenanceError(LibrusConnectionError):
    """Librus returned HTTP 503 (maintenance). Transient."""


class LibrusAuthError(LibrusError):
    """Base for errors that mean the session needs the user's attention via
    Home Assistant's reauth flow."""


class LibrusInvalidCredentialsError(LibrusAuthError):
    """The login/password was rejected (login response had no `goTo`
    redirect target and no captcha marker was seen)."""


class LibrusCaptchaRequiredError(LibrusAuthError):
    """A captcha marker was seen somewhere in the login response. Not
    observed in our own live testing, but szkolny-android's Portal login
    path (which this flow structurally resembles - a login form POST, not a
    bare token exchange) is known to be captcha-capable, so this is
    checked for defensively. There is no automated way to solve it, so it
    must not be presented to the user as "wrong password"."""


class LibrusAccountActionRequiredError(LibrusAuthError):
    """The account needs the user to take an action on Librus's own site
    (accept rules, change password, complete 2FA some other way) before this
    integration can sign in again."""


class LibrusUnexpectedResponseError(LibrusError):
    """The response didn't have the shape expected (missing/renamed JSON
    keys, non-JSON body, wrong HTTP status, redirect chain that never
    terminated)."""
