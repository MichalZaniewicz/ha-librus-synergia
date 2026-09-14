"""Regression tests for a real multi-student bug: two config entries (two
children) must never share a network session, since this client's auth
lives entirely in the session's cookie jar keyed only by domain - sharing
`async_get_clientsession(hass)` meant one entry's login could silently
"win" the jar for another entry's next request, swapping data between
siblings. See LibrusApiClient's docstring for the full story."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from .conftest import build_mock_client, make_config_entry


async def test_each_entry_gets_its_own_dedicated_session(hass) -> None:
    """Two entries set up together must each get a fresh, distinct session
    from `async_create_clientsession` - never the shared, hass-wide
    `async_get_clientsession`."""
    sessions = [AsyncMock(name=f"session{i}") for i in range(2)]
    entry_a = make_config_entry(username="1111111a")
    entry_b = make_config_entry(username="2222222b")
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)

    with (
        patch(
            "custom_components.librus_synergia.async_create_clientsession",
            side_effect=sessions,
        ) as mock_create,
        patch(
            "custom_components.librus_synergia.LibrusApiClient",
            side_effect=[build_mock_client(), build_mock_client()],
        ) as mock_client_ctor,
    ):
        assert await hass.config_entries.async_setup(entry_a.entry_id)
        assert await hass.config_entries.async_setup(entry_b.entry_id)
        await hass.async_block_till_done()

    # One dedicated session created per entry, not one shared session reused
    # for both - and each entry's client was actually built with its OWN
    # session, not the other entry's.
    assert mock_create.call_count == 2
    assert mock_client_ctor.call_args_list[0].args[0] is sessions[0]
    assert mock_client_ctor.call_args_list[1].args[0] is sessions[1]


async def test_unload_closes_only_that_entrys_own_client_session(hass) -> None:
    """Unloading one entry must close its own client's dedicated session
    (`LibrusApiClient.async_close`), and must not touch a still-loaded
    sibling entry's client - otherwise every reload leaks one session, and
    closing the wrong one would break a still-active sibling entry."""
    client_a = build_mock_client()
    client_b = build_mock_client()
    entry_a = make_config_entry(username="1111111a")
    entry_b = make_config_entry(username="2222222b")
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)

    with (
        patch(
            "custom_components.librus_synergia.async_create_clientsession",
            side_effect=[AsyncMock(), AsyncMock()],
        ),
        patch(
            "custom_components.librus_synergia.LibrusApiClient",
            side_effect=[client_a, client_b],
        ),
    ):
        assert await hass.config_entries.async_setup(entry_a.entry_id)
        assert await hass.config_entries.async_setup(entry_b.entry_id)
        await hass.async_block_till_done()

        assert await hass.config_entries.async_unload(entry_a.entry_id)
        await hass.async_block_till_done()

    client_a.async_close.assert_awaited_once()
    client_b.async_close.assert_not_called()
