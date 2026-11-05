"""Your own subject names (Configure → Subject names, issue #29): swapped
where the data is built, so every entity, attribute and card shows them."""

from __future__ import annotations

from homeassistant.helpers import entity_registry as er

from custom_components.librus_synergia.const import (
    CONF_SUBJECT_COLORS,
    CONF_SUBJECT_NAMES,
    DOMAIN,
)
from custom_components.librus_synergia.subject_names import parse_subject_names, rename_subjects

from .conftest import build_mock_client, setup_integration


def test_parse_subject_names() -> None:
    subjects = {100: "Edukacja matematyczna", 200: "Język angielski", 300: "Religia"}
    names, unmatched = parse_subject_names(
        "edukacja MATEMATYCZNA: Maths\nJezyk angielski:   English  \n300: RE\n"
        "Chemia: Chemistry\nReligia:\nbez dwukropka\n",
        subjects,
    )
    assert names == {100: "Maths", 200: "English", 300: "RE"}
    assert unmatched == ["Chemia: Chemistry", "Religia:", "bez dwukropka"]
    assert rename_subjects(subjects, names) == {100: "Maths", 200: "English", 300: "RE"}
    assert parse_subject_names(None, subjects) == ({}, [])


def _entity_id(hass, entry, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")


async def test_renamed_subject_shows_everywhere(hass) -> None:
    client = build_mock_client(
        async_get_subjects={"Subjects": [{"Id": 42005, "Name": "Matematyka"}, {"Id": 42006, "Name": "Fizyka"}]},
        async_get_grades={
            "Grades": [
                {"Id": 1, "Grade": "5", "Category": {"Id": 10}, "Subject": {"Id": 42005},
                 "Semester": 1, "AddDate": "2026-09-01"},
                {"Id": 2, "Grade": "4", "Category": {"Id": 10}, "Subject": {"Id": 42006},
                 "Semester": 1, "AddDate": "2026-09-02"},
            ]
        },
        async_get_grade_categories={
            "Categories": [{"Id": 10, "Name": "sprawdzian", "CountToTheAverage": True, "Weight": 1}]
        },
    )
    entry = await setup_integration(
        hass,
        client,
        options={
            CONF_SUBJECT_NAMES: "Matematyka: Maths\nBiologia: Biology",
            # The colour may name the subject by its Librus name or yours.
            CONF_SUBJECT_COLORS: "Matematyka: #2563eb\nPhysics: red",
        },
    )

    maths = hass.states.get(_entity_id(hass, entry, "subject_42005_average"))
    assert maths.attributes["subject"] == "Maths"
    physics = hass.states.get(_entity_id(hass, entry, "subject_42006_average"))
    assert physics.attributes["subject"] == "Fizyka"  # not renamed

    school = hass.states.get(_entity_id(hass, entry, "school")).attributes
    assert school["subject_colors"] == {"Maths": "#2563eb"}
    assert school["subject_colors_unmatched"] == ["Physics: red"]
    assert "Maths" in school["subject_palette"]
    assert school["subject_names_unmatched"] == ["Biologia: Biology"]
    assert entry.runtime_data.data.subjects[42005] == "Maths"
    assert entry.runtime_data.original_subjects[42005] == "Matematyka"
