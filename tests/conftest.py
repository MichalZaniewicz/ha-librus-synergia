"""Shared pytest fixtures/helpers for the Librus Synergia test suite."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch

import pytest
import pytest_socket
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.librus_synergia.const import DOMAIN
from custom_components.librus_synergia.librus_api.client import LibrusSessionData

# pytest-homeassistant-custom-component's own fixture setup calls
# `pytest_socket.disable_socket(allow_unix_socket=True)` directly (plugins.py,
# not something `-p no:socket` can intercept, since it's a plain function
# call, not a hook). Windows' asyncio ProactorEventLoop needs a REAL
# socket.socketpair() for its internal self-pipe, and on Windows that's
# emulated with an AF_INET loopback pair, not AF_UNIX - so
# `allow_unix_socket=True` doesn't cover it and event-loop creation itself
# gets blocked, even though nothing here does real network I/O (Linux's
# SelectorEventLoop uses os.pipe() instead and never hits this - so this is
# a local Windows-dev-only problem, not something CI, which runs on
# ubuntu-latest, needs). `pytest_socket.disable_socket` is looked up on the
# module at call time (plugins.py does `import pytest_socket` then
# `pytest_socket.disable_socket(...)`, not a `from`-import), so replacing it
# here - before that fixture ever runs - takes effect regardless of import
# order.
if sys.platform == "win32":
    pytest_socket.disable_socket = lambda *a, **k: None

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """`pytest-homeassistant-custom-component` requires this fixture to be
    requested before `hass.config_entries` will find and load anything
    under `custom_components/` - autouse so individual tests don't each
    need to remember it."""
    yield


ME_PAYLOAD = {
    "Me": {
        "Account": {"Id": 3461991, "FirstName": "Michał", "LastName": "Zaniewicz"},
        "User": {"FirstName": "Kacper", "LastName": "Zaniewicz"},
    }
}


def build_mock_client(**overrides) -> AsyncMock:
    """A `LibrusApiClient` double with sensible empty-account defaults for
    every endpoint - override individual methods' `return_value` (or
    `side_effect`, for error-path tests) via keyword args."""
    client = AsyncMock()
    client.username = "1234567u"
    client.is_session_valid.return_value = True
    client.async_login.return_value = LibrusSessionData(
        cookies=[{"name": "oauth_token", "value": "x", "domain": "synergia.librus.pl"}],
        logged_in_at=1000.0,
    )
    client.async_ensure_session_valid.return_value = None
    client.async_get_me.return_value = ME_PAYLOAD
    client.async_get_grades.return_value = {"Grades": []}
    client.async_get_grade_categories.return_value = {"Categories": []}
    client.async_get_notes.return_value = {"Notes": []}
    client.async_get_attendances.return_value = {"Attendances": []}
    client.async_get_attendance_types.return_value = {"Types": []}
    client.async_get_timetable.return_value = {"Timetable": {}}
    client.async_get_homeworks.return_value = {"HomeWorks": []}
    client.async_get_school_notices.return_value = {"SchoolNotices": []}
    client.async_get_lucky_number.return_value = {
        "LuckyNumber": {"LuckyNumber": 7, "LuckyNumberDay": "2026-09-05"}
    }
    client.async_get_subjects.return_value = {"Subjects": []}
    client.async_get_teachers.return_value = {"Users": []}
    client.async_get_classrooms.return_value = {"Classrooms": []}
    client.async_get_schools.return_value = {"School": {"Name": "Test School"}}
    client.async_get_classes.return_value = {"Class": {"Number": 7, "Symbol": "d"}}
    client.async_get_virtual_classes.return_value = {"VirtualClasses": []}
    client.async_get_school_free_days.return_value = {"SchoolFreeDays": []}
    client.async_get_class_free_days.return_value = {"ClassFreeDays": []}
    client.async_get_homework_categories.return_value = {"Categories": []}
    client.async_get_parent_teacher_conferences.return_value = {
        "ParentTeacherConferences": []
    }
    client.async_get_grade_types.return_value = {"Types": []}
    client.async_get_note_categories.return_value = {"Categories": []}
    client.async_get_behaviour_grade_points.return_value = {"Grades": []}
    client.async_get_behaviour_grade_point_categories.return_value = {"Categories": []}
    client.async_get_behaviour_grade_point_comments.return_value = {"Comments": []}
    client.async_get_grade_comments.return_value = {"Comments": []}
    client.async_get_units.return_value = {"Units": []}
    client.async_get_point_grades.return_value = {"Grades": []}
    client.async_get_descriptive_grades.return_value = {"Grades": []}
    client.async_get_text_grades.return_value = {"Grades": []}
    client.async_get_homework_assignments.return_value = {"HomeWorkAssignments": []}
    client.async_bootstrap_messages.return_value = False
    for key, value in overrides.items():
        setattr(getattr(client, key), "return_value", value)
    return client


def make_config_entry(*, options: dict | None = None, **data) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "1234567u", CONF_PASSWORD: "pw", **data},
        options=options or {},
    )


async def setup_integration(
    hass, client: AsyncMock, *, options: dict | None = None
) -> MockConfigEntry:
    """Add a config entry and run the real `async_setup_entry`, with the
    module-level `LibrusApiClient` constructor patched to return `client`."""
    entry = make_config_entry(options=options)
    entry.add_to_hass(hass)
    with patch("custom_components.librus_synergia.LibrusApiClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry
