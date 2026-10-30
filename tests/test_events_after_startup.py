"""Bus events found before Home Assistant has started.

The first refresh runs while Home Assistant boots, before any automation has
armed its triggers: what it found - a grade added while HA was off or
restarting - was fired into the void and recorded as seen, so it was never
announced. Those events are now held - saved with the ids they announce, so
an unload, a failed setup or a stop before the start doesn't lose them - and
fired once Home Assistant has started."""

from __future__ import annotations

import ast
import copy
import logging
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.librus_synergia.const import (
    DOMAIN,
    EVENT_FORECAST_CHANGED,
    EVENT_NEW_GRADE,
    STATE_SAVE_DELAY,
)
from custom_components.librus_synergia.coordinator import state_store_key

from .conftest import build_mock_client, setup_integration

_COMPONENT = Path(__file__).parent.parent / "custom_components" / "librus_synergia"

_NOW = "2026-10-20T12:00:00+00:00"

# What the grades added below announce: the 1 (a 1-6 grade), the point grade
# and the forecast drop the 1 causes - each fired from a different place in
# the coordinator, in this order.
_ANNOUNCED = ["grade 2 (normal)", "grade 3 (point)", "forecast 4->2"]
_HELD_TYPES = [EVENT_NEW_GRADE, EVENT_NEW_GRADE, EVENT_FORECAST_CHANGED]


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Setting up `automation` copies its example blueprints into the
    config directory - a temporary one, not the test package's own."""
    return hass_tmp_config_dir


def _grade(grade_id: int, value: str, day: str = "2026-09-15") -> dict:
    return {
        "Id": grade_id,
        "Grade": value,
        "Category": {"Id": 10},
        "Subject": {"Id": 100},
        "Semester": 1,
        "AddDate": f"{day} 10:00:00",
    }


def _point(grade_id: int, points: str) -> dict:
    return {
        "Id": grade_id,
        "Grade": points,
        "GradeValue": float(points),
        "Category": {"Id": 2},
        "Subject": {"Id": 200},
        "Semester": 1,
        "AddDate": "2026-10-01 10:00:00",
    }


def _client() -> AsyncMock:
    """A 4 in Matematyka (its forecast: 4) and a point grade in Fizyka."""
    return build_mock_client(
        async_get_subjects={
            "Subjects": [{"Id": 100, "Name": "Matematyka"}, {"Id": 200, "Name": "Fizyka"}]
        },
        async_get_grades={"Grades": [_grade(1, "4")]},
        async_get_grade_categories={
            "Categories": [{"Id": 10, "Name": "Kartkówka", "CountToTheAverage": True, "Weight": 1}]
        },
        # Points on: PointGrades is then asked for every cycle.
        async_get_units={"Units": [{"GradesSettings": {"PointGradesEnabled": True}}]},
        async_get_point_grades={"Grades": [_point(1, "8")]},
        async_get_point_grade_categories={
            "Categories": [
                {"Id": 2, "Name": "Kartkówka", "Weight": 1, "CountToTheAverage": True,
                 "ValueTo": 10}
            ]
        },
    )


def _add_new_grades(client: AsyncMock) -> None:
    """While Home Assistant is off: a 1 (the forecast drops from 4 to 2)
    and a point grade."""
    client.async_get_grades.return_value = {
        "Grades": [_grade(1, "4"), _grade(2, "1", day="2026-10-19")]
    }
    client.async_get_point_grades.return_value = {"Grades": [_point(1, "8"), _point(3, "2")]}


def _record(hass: HomeAssistant) -> list[str]:
    """The new-grade and forecast-changed events, in the order they fire."""
    fired: list[str] = []

    @callback
    def record(event: Event) -> None:
        data = event.data
        if event.event_type == EVENT_FORECAST_CHANGED:
            fired.append(f"forecast {data['old']}->{data['new']}")
        else:
            fired.append(f"grade {data['id']} ({data['kind']})")

    hass.bus.async_listen(EVENT_NEW_GRADE, record)
    hass.bus.async_listen(EVENT_FORECAST_CHANGED, record)
    return fired


def _started_listeners(hass: HomeAssistant) -> int:
    return hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STARTED, 0)


async def _set_up_again(hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock) -> None:
    with patch("custom_components.librus_synergia.LibrusApiClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def _restart(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: AsyncMock,
    state: CoreState = CoreState.not_running,
) -> None:
    """Home Assistant restarting: the entry unloads (saving what it has
    seen) and is set up again in `state` - not_running is the boot, where
    async_setup_entry runs the first refresh."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.set_state(state)
    await _set_up_again(hass, entry, client)


def _started(hass: HomeAssistant) -> None:
    """The end of HomeAssistant.async_start: running, then STARTED."""
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)


def _saved_held(hass_storage: dict, entry: MockConfigEntry) -> list[str]:
    """The event types the entry's saved state still holds."""
    saved = hass_storage[state_store_key(entry.entry_id)]["data"]
    return [event["event_type"] for event in saved["held_events"]]


async def _write_delayed(hass: HomeAssistant, freezer) -> None:
    """Let the state's delayed write happen."""
    freezer.tick(timedelta(seconds=STATE_SAVE_DELAY + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


@pytest.mark.parametrize("state", [CoreState.not_running, CoreState.starting])
async def test_events_found_while_starting_fire_once_started(hass, freezer, state) -> None:
    """Before: the first refresh fired these while HA was still starting -
    to nobody - and they were never announced again."""
    freezer.move_to(_NOW)
    client = _client()
    entry = await setup_integration(hass, client)  # the first sync only seeds
    fired = _record(hass)
    _add_new_grades(client)

    await _restart(hass, entry, client, state)
    assert fired == []

    _started(hass)
    await hass.async_block_till_done()
    assert fired == _ANNOUNCED

    # Started: nothing held any more, and nothing announced twice.
    assert entry.runtime_data._held_events == []
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert fired == _ANNOUNCED


@pytest.mark.parametrize("state", [CoreState.running, CoreState.stopping])
async def test_events_fire_at_once_after_the_start(hass, freezer, state) -> None:
    """Once Home Assistant has started events fire at once, as before. So
    do they while it stops: no start is coming to fire anything held."""
    freezer.move_to(_NOW)
    client = _client()
    entry = await setup_integration(hass, client)
    fired = _record(hass)
    listeners = _started_listeners(hass)
    _add_new_grades(client)
    hass.set_state(state)

    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert fired == _ANNOUNCED
    assert _started_listeners(hass) == listeners  # nothing waits for a start


@pytest.mark.parametrize("again", [CoreState.not_running, CoreState.running])
async def test_unloaded_before_the_start_announced_by_the_next_setup(
    hass, freezer, hass_storage, caplog, again
) -> None:
    """Unloaded before Home Assistant has started - a reload or an options
    change during the boot (set up again while it boots), or the entry
    disabled then and enabled once started. Before: the unload saved the
    items as seen and dropped their events, so they were never announced.
    Now its write keeps them, and the next setup fires them, once."""
    freezer.move_to(_NOW)
    client = _client()
    entry = await setup_integration(hass, client)
    fired = _record(hass)
    listeners = _started_listeners(hass)
    _add_new_grades(client)
    await _restart(hass, entry, client)
    assert _started_listeners(hass) == listeners + 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    # The unloaded coordinator fires nothing; its final write holds them.
    assert _started_listeners(hass) == listeners
    assert _saved_held(hass_storage, entry) == _HELD_TYPES
    if again is CoreState.running:
        _started(hass)
        await hass.async_block_till_done()
        assert fired == []

    await _set_up_again(hass, entry, client)
    if again is CoreState.not_running:
        assert fired == []
        _started(hass)
        await hass.async_block_till_done()
    assert fired == _ANNOUNCED

    # Fired: the next write drops them, and nothing is announced twice -
    # not by a refresh, not after the next restart.
    await _write_delayed(hass, freezer)
    assert _saved_held(hass_storage, entry) == []
    await entry.runtime_data.async_refresh()
    await _restart(hass, entry, client)
    _started(hass)
    await hass.async_block_till_done()
    assert fired == _ANNOUNCED
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_started_while_unloading_leaves_them_to_the_next_setup(
    hass, freezer, hass_storage
) -> None:
    """STARTED comes while the entry unloads, after its final write: the
    unloading coordinator doesn't fire them (the saved state has them - the
    next setup would announce them a second time); the next setup does."""
    freezer.move_to(_NOW)
    client = _client()
    entry = await setup_integration(hass, client)
    fired = _record(hass)
    _add_new_grades(client)
    await _restart(hass, entry, client)
    coordinator = entry.runtime_data
    close_state = coordinator.async_close_state

    async def close_state_then_started() -> None:
        await close_state()
        _started(hass)

    with patch.object(coordinator, "async_close_state", close_state_then_started):
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert fired == []
    assert _saved_held(hass_storage, entry) == _HELD_TYPES

    await _set_up_again(hass, entry, client)
    assert fired == _ANNOUNCED


async def test_removed_before_the_start_announces_nothing(hass, freezer, hass_storage) -> None:
    """A deleted entry's held events go with its saved state."""
    freezer.move_to(_NOW)
    client = _client()
    entry = await setup_integration(hass, client)
    fired = _record(hass)
    listeners = _started_listeners(hass)
    _add_new_grades(client)
    await _restart(hass, entry, client)
    assert len(entry.runtime_data._held_events) == 3

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _started_listeners(hass) == listeners
    assert state_store_key(entry.entry_id) not in hass_storage

    _started(hass)
    await hass.async_block_till_done()
    assert fired == []


async def test_stopped_before_the_start_announced_after_the_next_start(
    hass, freezer, hass_storage
) -> None:
    """Home Assistant stops before it has started (restarted again while it
    starts): STARTED never comes. Before: the final write saved the items as
    seen, unannounced. Now it saves their events with them, and the next
    start fires them."""
    freezer.move_to(_NOW)
    client = _client()
    entry = await setup_integration(hass, client)
    fired = _record(hass)
    _add_new_grades(client)
    await _restart(hass, entry, client, CoreState.starting)

    hass.set_state(CoreState.stopping)
    hass.set_state(CoreState.final_write)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    assert fired == []
    stopped = copy.deepcopy(hass_storage[state_store_key(entry.entry_id)])
    assert 3 in stopped["data"]["known_items"]["point_grades"]  # seen
    assert _saved_held(hass_storage, entry) == _HELD_TYPES

    # The next start, from what the stop wrote.
    hass.set_state(CoreState.not_running)
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass_storage[state_store_key(entry.entry_id)] = stopped
    await _set_up_again(hass, entry, client)
    assert fired == []
    _started(hass)
    await hass.async_block_till_done()
    assert fired == _ANNOUNCED


@pytest.mark.parametrize("write_lands", [False, True], ids=["unwritten", "written"])
async def test_failed_setup_leaves_them_to_the_retry(
    hass, freezer, hass_storage, write_lands
) -> None:
    """The setup fails after the first refresh, during the boot (HA retries
    it at STARTED). The failed coordinator fires nothing; its pending write
    may land before the retry (then the items are saved as seen, with their
    events) or not (then they are unseen): the retry announces each once."""
    freezer.move_to(_NOW)
    client = _client()
    entry = await setup_integration(hass, client)
    fired = _record(hass)
    listeners = _started_listeners(hass)
    _add_new_grades(client)
    forward_entry_setups = hass.config_entries.async_forward_entry_setups
    failures = [ConfigEntryNotReady("not this time")]

    async def failing_once(*args, **kwargs) -> None:
        if failures:
            raise failures.pop()
        await forward_entry_setups(*args, **kwargs)

    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.set_state(CoreState.not_running)
    with (
        patch("custom_components.librus_synergia.LibrusApiClient", return_value=client),
        patch.object(hass.config_entries, "async_forward_entry_setups", failing_once),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert _started_listeners(hass) == listeners + 1  # the retry's only
        if write_lands:
            await _write_delayed(hass, freezer)
            assert _saved_held(hass_storage, entry) == _HELD_TYPES

        _started(hass)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.state is ConfigEntryState.LOADED
    assert fired == _ANNOUNCED


async def test_grade_found_at_boot_reaches_an_automation_armed_later(hass, freezer) -> None:
    """The real start: an automation set up during the boot arms its trigger
    in a startup job of HomeAssistant.async_start - after the entry's first
    refresh has found the new grades. Its notification still goes out, and
    the event entity shows the grade."""
    freezer.move_to(_NOW)
    client = _client()
    entry = await setup_integration(hass, client)
    _add_new_grades(client)
    calls = async_mock_service(hass, "test", "automation")

    # Home Assistant restarts. The boot sets up the automation (its trigger
    # not armed yet) and the entry, whose first refresh finds both grades.
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.set_state(CoreState.not_running)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                # The held events come in a burst: `single` would drop two.
                "mode": "queued",
                "triggers": [
                    {"trigger": "event", "event_type": EVENT_NEW_GRADE},
                    {"trigger": "event", "event_type": EVENT_FORECAST_CHANGED},
                ],
                "actions": [
                    {
                        "action": "test.automation",
                        "data": {
                            "event": "{{ trigger.event.event_type }}",
                            "subject": "{{ trigger.event.data.subject }}",
                        },
                    }
                ],
            }
        },
    )
    await _set_up_again(hass, entry, client)
    grade_event = er.async_get(hass).async_get_entity_id(
        "event", DOMAIN, f"{entry.entry_id}_grade_event"
    )
    assert calls == []
    assert hass.states.get(grade_event).attributes.get("event_type") is None

    await hass.async_start()  # START, startup jobs (arming), running, STARTED
    await hass.async_block_till_done()

    assert [(call.data["event"], call.data["subject"]) for call in calls] == [
        (EVENT_NEW_GRADE, "Matematyka"),
        (EVENT_NEW_GRADE, "Fizyka"),
        (EVENT_FORECAST_CHANGED, "Matematyka"),
    ]
    assert hass.states.get(grade_event).attributes["event_type"] == "new_grade"


def _bus_fires(path: Path) -> list[str]:
    """The function each `<...>.bus.async_fire` (or `fire`,
    `async_fire_internal`) in the module is in - called or not."""
    found: list[str] = []

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, child.name)
                continue
            if (
                isinstance(child, ast.Attribute)
                and child.attr in {"async_fire", "async_fire_internal", "fire"}
                and (
                    (isinstance(child.value, ast.Attribute) and child.value.attr == "bus")
                    or (isinstance(child.value, ast.Name) and child.value.id == "bus")
                )
            ):
                found.append(function)
            visit(child, function)

    visit(ast.parse(path.read_text(encoding="utf-8")), "<module>")
    return found


def test_every_bus_event_goes_through_async_fire_event() -> None:
    """Every bus event of the integration goes through the coordinator's
    async_fire_event - the one place that holds it until Home Assistant has
    started. One fired straight on the bus would be lost again when found
    at the boot: the bus fires happen there and in the flush of the held
    ones, nowhere else."""
    fires = sorted(
        f"{path.relative_to(_COMPONENT)}:{function}"
        for path in _COMPONENT.rglob("*.py")
        for function in _bus_fires(path)
    )
    assert fires == [
        "coordinator.py:_async_fire_held_events",
        "coordinator.py:async_fire_event",
    ], (
        "Fire the integration's bus events with the coordinator's "
        f"async_fire_event, not on the bus directly - found in: {fires}"
    )
