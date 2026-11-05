"""One colour per subject for the companion cards: a palette slot for each
of the student's subjects, overridden by the Subject colours option."""

from __future__ import annotations

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.librus_synergia.const import CONF_SUBJECT_COLORS, DOMAIN
from custom_components.librus_synergia.subject_colors import (
    PALETTE_SIZE,
    palette_slots,
    parse_subject_colors,
)

from .conftest import build_mock_client, setup_integration


def test_palette_slots_are_stable_and_unique() -> None:
    names = ["Matematyka", "Język polski", "Biologia", "Historia", "Fizyka"]
    slots = palette_slots(names)
    assert set(slots) == set(names)
    assert len(set(slots.values())) == len(names)
    assert all(1 <= slot <= PALETTE_SIZE for slot in slots.values())
    # The order the subjects come in doesn't matter.
    assert palette_slots(list(reversed(names))) == slots
    # More subjects than colours: slots repeat, every subject still gets one.
    many = palette_slots([f"Przedmiot {i}" for i in range(20)])
    assert len(many) == 20
    assert set(many.values()) <= set(range(1, PALETTE_SIZE + 1))


def test_parse_subject_colors() -> None:
    subjects = {100: "Matematyka", 200: "Język polski", 300: "Wychowanie fizyczne"}
    colors, unmatched = parse_subject_colors(
        "matematyka: #2563eb\nJEZYK POLSKI: rgb(219 39 119)\n300: green\n"
        "Chemia: red\nMatematyka: url(x)\nbez dwukropka\n",
        subjects,
    )
    assert colors == {
        "Matematyka": "#2563eb",
        "Język polski": "rgb(219 39 119)",
        "Wychowanie fizyczne": "green",
    }
    assert unmatched == ["Chemia: red", "Matematyka: url(x)", "bez dwukropka"]
    assert parse_subject_colors("", subjects) == ({}, [])


async def test_school_sensor_has_the_subject_colours(hass) -> None:
    day = dt_util.now().date().isoformat()
    timetable = {
        "Timetable": {
            day: [
                [{"LessonNo": "1", "HourFrom": "08:00", "HourTo": "08:45", "Subject": {"Id": "100"}}],
                [{"LessonNo": "2", "HourFrom": "09:00", "HourTo": "09:45", "Subject": {"Id": "200"}}],
            ]
        }
    }
    client = build_mock_client(
        async_get_timetable=timetable,
        async_get_subjects={
            "Subjects": [
                {"Id": 100, "Name": "Matematyka"},
                {"Id": 200, "Name": "Język polski"},
                {"Id": 900, "Name": "Religia"},
            ]
        },
    )
    entry = await setup_integration(
        hass, client, options={CONF_SUBJECT_COLORS: "Matematyka: #2563eb\nKsiężyc: blue"}
    )
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_school")
    attributes = hass.states.get(entity_id).attributes
    # Only the student's own subjects, not the school's whole list.
    assert set(attributes["subject_palette"]) == {"Matematyka", "Język polski"}
    assert attributes["subject_colors"] == {"Matematyka": "#2563eb"}
    assert attributes["subject_colors_unmatched"] == ["Księżyc: blue"]
