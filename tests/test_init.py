"""Regression tests for a real multi-student bug: two config entries (two
children) must never share a network session, since this client's auth
lives entirely in the session's cookie jar keyed only by domain - sharing
`async_get_clientsession(hass)` meant one entry's login could silently
"win" the jar for another entry's next request, swapping data between
siblings. See LibrusApiClient's docstring for the full story.

Note: adding a SECOND config entry to `hass` before the `librus_synergia`
component has been set up at all makes Home Assistant's own domain-level
bootstrap load BOTH entries as part of the first `async_setup(entry_id)`
call (not just the one requested) - so these tests add both entries, call
`async_setup` once, and inspect each entry's own `runtime_data` afterward
rather than assuming anything about call order between the two entries'
own `async_setup_entry` invocations (which may run concurrently).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from .conftest import build_mock_client, make_config_entry


def _client_factory_recording_session(session, username, **kwargs):
    """Stand-in for `LibrusApiClient(session, username, ...)` that records
    which session object it was built with, so a test can later check
    `entry.runtime_data.client.session` without caring about call order."""
    client = build_mock_client()
    client.session = session
    client.username = username
    return client


async def test_each_entry_gets_its_own_dedicated_session(hass) -> None:
    """Two entries loaded together must each end up with a distinct
    session from `async_create_clientsession` - never the same shared,
    hass-wide session."""
    entry_a = make_config_entry(username="1111111a")
    entry_b = make_config_entry(username="2222222b")
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)

    with (
        patch(
            "custom_components.librus_synergia.async_create_clientsession",
            side_effect=lambda hass: AsyncMock(),
        ) as mock_create,
        patch(
            "custom_components.librus_synergia.LibrusApiClient",
            side_effect=_client_factory_recording_session,
        ),
    ):
        assert await hass.config_entries.async_setup(entry_a.entry_id)
        await hass.async_block_till_done()

    assert entry_a.runtime_data is not None
    assert entry_b.runtime_data is not None
    assert mock_create.call_count == 2
    # The real invariant the old shared-session bug violated: each entry's
    # own client must hold a DIFFERENT session object.
    assert entry_a.runtime_data.client.session is not entry_b.runtime_data.client.session


async def test_unload_closes_only_that_entrys_own_client_session(hass) -> None:
    """Unloading one entry must close its own client's dedicated session,
    and must not touch a still-loaded sibling entry's client - otherwise
    every reload leaks one session, and closing the wrong one would break
    a still-active sibling entry."""
    entry_a = make_config_entry(username="1111111a")
    entry_b = make_config_entry(username="2222222b")
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)

    with (
        patch(
            "custom_components.librus_synergia.async_create_clientsession",
            side_effect=lambda hass: AsyncMock(),
        ),
        patch(
            "custom_components.librus_synergia.LibrusApiClient",
            side_effect=_client_factory_recording_session,
        ),
    ):
        assert await hass.config_entries.async_setup(entry_a.entry_id)
        await hass.async_block_till_done()

        client_a = entry_a.runtime_data.client
        client_b = entry_b.runtime_data.client
        assert client_a is not client_b

        assert await hass.config_entries.async_unload(entry_a.entry_id)
        await hass.async_block_till_done()

    client_a.async_close.assert_awaited_once()
    client_b.async_close.assert_not_called()
