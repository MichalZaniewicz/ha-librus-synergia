"""Your own subject names (Configure → Subject names, issue #29): swapped
where the data is built, so every entity, attribute and card shows them."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.librus_synergia.const import (
    CONF_GRADE_CATEGORY_NAMES,
    CONF_SUBJECT_COLORS,
    CONF_SUBJECT_NAMES,
    DOMAIN,
)
from custom_components.librus_synergia.subject_names import (
    category_key,
    parse_category_names,
    parse_subject_names,
    rename_subjects,
)

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


def test_parse_category_names() -> None:
    names, unmatched = parse_category_names(
        "KARTKOWKA: Quiz\naktywność: Activity\nSprawdzian:\nwycieczka: Trip\n",
        ["kartkówka", "Aktywność", "sprawdzian"],
    )
    assert names == {category_key("kartkówka"): "Quiz", category_key("aktywność"): "Activity"}
    assert unmatched == ["Sprawdzian:", "wycieczka: Trip"]


async def test_renamed_grade_category_shows_in_the_grades(hass) -> None:
    client = build_mock_client(
        async_get_subjects={"Subjects": [{"Id": 42005, "Name": "Matematyka"}]},
        async_get_grades={
            "Grades": [
                {"Id": 1, "Grade": "5", "Category": {"Id": 10}, "Subject": {"Id": 42005},
                 "Semester": 1, "AddDate": "2026-09-01"},
                {"Id": 2, "Grade": "3", "Category": {"Id": 11}, "Subject": {"Id": 42005},
                 "Semester": 1, "AddDate": "2026-09-02"},
            ]
        },
        async_get_grade_categories={
            "Categories": [
                {"Id": 10, "Name": "kartkówka", "CountToTheAverage": True, "Weight": 1},
                {"Id": 11, "Name": "aktywność", "CountToTheAverage": True, "Weight": 1},
            ]
        },
    )
    entry = await setup_integration(
        hass, client, options={CONF_GRADE_CATEGORY_NAMES: "Kartkowka: Quiz\nwycieczka: Trip"}
    )

    grades = hass.states.get(_entity_id(hass, entry, "subject_42005_average")).attributes["grades"]
    assert {g["value"]: g["category"] for g in grades} == {"5": "Quiz", "3": "aktywność"}
    school = hass.states.get(_entity_id(hass, entry, "school")).attributes
    assert school["grade_category_names_unmatched"] == ["wycieczka: Trip"]
    # Only the name changes: the weight and counting stay the school's.
    assert entry.runtime_data.data.grade_categories[10].weight == 1


async def test_names_step_lists_the_accounts_subjects_and_categories(hass) -> None:
    """Configure -> Subject and category names: every subject and grade
    category of the account with a field for your name, saved as lines."""
    client = build_mock_client(
        async_get_subjects={
            "Subjects": [
                {"Id": 42005, "Name": "Matematyka"},
                {"Id": 42006, "Name": "Fizyka"},
                {"Id": 42999, "Name": "Astronomia"},  # the school's, not the student's
            ]
        },
        async_get_grades={
            "Grades": [
                {"Id": 1, "Grade": "5", "Category": {"Id": 10}, "Subject": {"Id": 42005},
                 "Semester": 1, "AddDate": "2026-09-01"},
                {"Id": 2, "Grade": "4", "Category": {"Id": 10}, "Subject": {"Id": 42006},
                 "Semester": 1, "AddDate": "2026-09-02"},
            ]
        },
        async_get_grade_categories={
            "Categories": [
                {"Id": 10, "Name": "kartkówka", "CountToTheAverage": True, "Weight": 1},
                {"Id": 11, "Name": "projekt", "CountToTheAverage": True, "Weight": 1},
            ]
        },
    )
    entry = await setup_integration(hass, client, options={CONF_SUBJECT_NAMES: "Fizyka: Physics"})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "names"})
    assert result["type"] == "form"
    schema = result["data_schema"].schema
    subjects = next(v for k, v in schema.items() if str(k) == "subjects").schema.schema
    categories = next(v for k, v in schema.items() if str(k) == "categories").schema.schema
    assert [str(k) for k in subjects] == ["Fizyka", "Matematyka"]
    assert [str(k) for k in categories] == ["kartkówka"]  # only the ones in use
    fizyka = next(k for k in subjects if str(k) == "Fizyka")
    assert fizyka.description == {"suggested_value": "Physics"}

    # Saving the form also reloads the entry: keep the mocked client.
    with patch("custom_components.librus_synergia.LibrusApiClient", return_value=client):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"subjects": {"Matematyka": " Maths ", "Fizyka": ""}, "categories": {"kartkówka": "Quiz"}},
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert entry.options[CONF_SUBJECT_NAMES] == "Matematyka: Maths"
    assert entry.options[CONF_GRADE_CATEGORY_NAMES] == "kartkówka: Quiz"


async def test_saving_settings_keeps_the_names(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": "1234567u", "password": "x"},
        options={CONF_SUBJECT_NAMES: "Matematyka: Maths", CONF_GRADE_CATEGORY_NAMES: "kartkówka: Quiz"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert entry.options[CONF_SUBJECT_NAMES] == "Matematyka: Maths"
    assert entry.options[CONF_GRADE_CATEGORY_NAMES] == "kartkówka: Quiz"
