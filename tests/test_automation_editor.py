"""Triggers and conditions for the automation editor, the logbook entries
and the system health info."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_mock_service,
    get_system_health_info,
)

from custom_components.librus_synergia import condition as librus_condition
from custom_components.librus_synergia import logbook as librus_logbook
from custom_components.librus_synergia import trigger as librus_trigger
from custom_components.librus_synergia.const import (
    DOMAIN,
    EVENT_NEW_ABSENCE,
    EVENT_NEW_GRADE,
    EVENT_TIMETABLE_CHANGED,
)

from .conftest import build_mock_client, setup_integration

_COMPONENT = Path(__file__).parent.parent / "custom_components" / "librus_synergia"


def _device_id(hass, entry) -> str:
    return dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)[0].id


def _described(name: str) -> set[str]:
    """Keys of a triggers/conditions file, without the YAML anchor blocks
    (".target", ...) that Home Assistant drops too."""
    return {key for key in load_yaml_dict(str(_COMPONENT / name)) if not key.startswith(".")}


def _strings(name: str = "strings.json") -> dict:
    return json.loads((_COMPONENT / name).read_text(encoding="utf-8"))


# --- descriptions -----------------------------------------------------------


def test_every_trigger_and_condition_is_described_and_translated() -> None:
    triggers = _described("triggers.yaml")
    conditions = _described("conditions.yaml")
    assert triggers == set(librus_trigger._SPECS)
    assert conditions == set(librus_condition._SPECS)
    icons = _strings("icons.json")
    for name in ("strings.json", "translations/en.json", "translations/pl.json"):
        strings = _strings(name)
        assert set(strings["triggers"]) == triggers, name
        assert set(strings["conditions"]) == conditions, name
    assert set(icons["triggers"]) == triggers
    assert set(icons["conditions"]) == conditions


def test_every_select_option_is_translated() -> None:
    selectors = _strings()["selector"]
    for file in ("triggers.yaml", "conditions.yaml"):
        for key, description in load_yaml_dict(str(_COMPONENT / file)).items():
            for field, spec in (description or {}).get("fields", {}).items():
                select = (spec.get("selector") or {}).get("select")
                if select is None:
                    continue
                options = selectors[select["translation_key"]]["options"]
                assert set(select["options"]) == set(options), f"{file} {key}.{field}"


# --- trigger filters ----------------------------------------------------------


def test_grade_filter() -> None:
    passes = librus_trigger._SPECS["new_grade"][2]
    grade = {"kind": "normal", "subject": "Matematyka", "value": "2+"}
    assert passes(grade, {})
    assert passes(grade, {"subject": "matem, fizyka"})
    assert not passes(grade, {"subject": "fizyka"})
    assert passes(grade, {"grade_at_most": 2.5})
    assert not passes(grade, {"grade_at_most": 2})
    assert not passes(grade, {"grade_at_least": 3})
    assert passes(grade, {"kind": ["normal"]})
    assert not passes(grade, {"kind": ["proposition"]})
    assert passes({"kind": "semester_proposition", "value": "4"}, {"kind": ["proposition"]})
    # No number to compare: a threshold never matches.
    assert not passes({"kind": "text", "value": "zal"}, {"grade_at_most": 6})
    assert not passes({"kind": "normal", "value": "np"}, {"grade_at_least": 1})
    # Polish letters don't matter.
    assert passes({"subject": "Język polski", "value": "5"}, {"subject": "jezyk pol"})


def test_other_filters() -> None:
    f = {key: spec[2] for key, spec in librus_trigger._SPECS.items()}
    assert f["new_absence"]({"excused": True}, {}) is True
    assert f["new_absence"]({"excused": True}, {"only_unexcused": True}) is False
    assert f["new_note"]({"sentiment": None}, {"sentiment": ["neutral"]})
    assert not f["new_note"]({"sentiment": "positive"}, {"sentiment": ["negative"]})
    assert f["new_message"]({}, {"mailbox": ["inbox"]})
    assert not f["new_message"]({"mailbox": "alerts"}, {"mailbox": ["inbox"]})
    assert f["timetable_changed"]({"change": "room_change"}, {"change": ["room_change"]})
    assert not f["timetable_changed"]({"kind": "canceled"}, {"change": ["substitution"]})
    assert f["new_agenda_entry"]({"category": "Kartkówka"}, {"category": "sprawdzian, kartkowka"})
    assert f["new_agenda_entry"]({"category": "Inne", "content": "kartkówka z ułamków"}, {"category": "kartkówka"})
    assert not f["new_agenda_entry"]({"category": "Wycieczka"}, {"category": "sprawdzian"})
    assert f["justification_status"]({"rejected": True}, {"outcome": ["rejected"]})
    assert f["weekly_summary"]({"audience": "student"}, {"audience": "any"})
    assert not f["weekly_summary"]({"audience": "student"}, {"audience": "parent"})


def test_trigger_payload_keeps_home_assistant_keys() -> None:
    payload = librus_trigger.trigger_payload(EVENT_NEW_GRADE, {"id": 5, "subject": "Biologia"})
    assert payload == {"item_id": 5, "subject": "Biologia", "event_type": EVENT_NEW_GRADE}


# --- triggers in a real automation --------------------------------------------


async def test_new_grade_trigger_runs_automation(hass) -> None:
    entry = await setup_integration(hass, build_mock_client())
    coordinator = entry.runtime_data
    calls = async_mock_service(hass, "test", "automation")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "triggers": [
                    {
                        "trigger": "librus_synergia.new_grade",
                        "id": "low",
                        "target": {"device_id": _device_id(hass, entry)},
                        "options": {"grade_at_most": 2},
                    }
                ],
                "actions": [
                    {
                        "action": "test.automation",
                        "data": {
                            "subject": "{{ trigger.subject }}",
                            "item": "{{ trigger.item_id }}",
                            "trigger_id": "{{ trigger.id }}",
                        },
                    }
                ],
            }
        },
    )
    entry_id = entry.entry_id
    coordinator.async_fire_event(
        EVENT_NEW_GRADE, {"entry_id": entry_id, "id": 1, "subject": "Biologia", "value": "5"}
    )
    coordinator.async_fire_event(
        EVENT_NEW_GRADE, {"entry_id": "other", "id": 2, "subject": "Biologia", "value": "1"}
    )
    coordinator.async_fire_event(
        EVENT_NEW_GRADE, {"entry_id": entry_id, "id": 3, "subject": "Chemia", "value": "2-"}
    )
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data == {"subject": "Chemia", "item": 3, "trigger_id": "low"}


async def test_trigger_without_target_serves_every_student(hass) -> None:
    entry = await setup_integration(hass, build_mock_client())
    calls = async_mock_service(hass, "test", "automation")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "triggers": [
                    {
                        "trigger": "librus_synergia.timetable_changed",
                        "options": {"change": ["canceled"]},
                    }
                ],
                "actions": [{"action": "test.automation", "data": {"s": "{{ trigger.subject }}"}}],
            }
        },
    )
    entry.runtime_data.async_fire_event(
        EVENT_TIMETABLE_CHANGED,
        {"entry_id": entry.entry_id, "kind": "canceled", "change": "canceled", "subject": "WF"},
    )
    entry.runtime_data.async_fire_event(
        EVENT_TIMETABLE_CHANGED,
        {"entry_id": entry.entry_id, "kind": "substitution", "change": "substitution"},
    )
    await hass.async_block_till_done()
    assert [c.data for c in calls] == [{"s": "WF"}]


async def test_events_carry_the_device_id(hass) -> None:
    entry = await setup_integration(hass, build_mock_client())
    events = async_capture_events(hass, EVENT_NEW_ABSENCE)
    entry.runtime_data.async_fire_event(EVENT_NEW_ABSENCE, {"entry_id": entry.entry_id})
    await hass.async_block_till_done()
    assert events[0].data["device_id"] == _device_id(hass, entry)


# --- conditions -----------------------------------------------------------------


async def test_school_day_condition(hass) -> None:
    entry = await setup_integration(hass, build_mock_client())
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_school_day_tomorrow"
    )
    calls = async_mock_service(hass, "test", "automation")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "triggers": [{"trigger": "event", "event_type": "test_event"}],
                "conditions": [
                    {
                        "condition": "librus_synergia.school_day",
                        "target": {"device_id": _device_id(hass, entry)},
                        "options": {"day": "tomorrow"},
                    }
                ],
                "actions": [{"action": "test.automation"}],
            }
        },
    )
    hass.states.async_set(entity_id, "off")
    hass.bus.async_fire("test_event")
    await hass.async_block_till_done()
    assert len(calls) == 0

    hass.states.async_set(entity_id, "on")
    hass.bus.async_fire("test_event")
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_condition_checks(hass) -> None:
    entry = await setup_integration(hass, build_mock_client())
    registry = er.async_get(hass)

    def entity(platform: str, suffix: str) -> str:
        return registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{suffix}")

    lucky = entity("sensor", "lucky_number")
    hass.states.async_set(lucky, "25", {"is_yours": True, "is_today": False})
    assert not librus_condition.check(hass, "lucky_number_yours", None, {})
    hass.states.async_set(lucky, "25", {"is_yours": True, "is_today": True})
    assert librus_condition.check(hass, "lucky_number_yours", None, {})

    absences = entity("sensor", "unexcused_absences")
    hass.states.async_set(absences, "0")
    assert not librus_condition.check(hass, "absences_to_justify", None, {})
    hass.states.async_set(absences, "2")
    assert librus_condition.check(hass, "absences_to_justify", None, {"behavior": "all"})
    hass.states.async_set(absences, "unavailable")
    assert not librus_condition.check(hass, "absences_to_justify", None, {})

    # A target that leads to no Librus student matches nothing.
    assert not librus_condition.check(
        hass, "absences_to_justify", {"device_id": "nope"}, {}
    )


# --- logbook ------------------------------------------------------------------


def test_logbook_lines() -> None:
    grade = {"student": "Ola", "subject": "Matematyka", "value": "5", "category": "Sprawdzian"}
    assert librus_logbook.describe(EVENT_NEW_GRADE, grade, "pl") == {
        "name": "Ola",
        "message": "nowa ocena 5 - Matematyka (Sprawdzian)",
        "icon": "mdi:numeric-6-box-outline",
    }
    assert (
        librus_logbook.describe(EVENT_NEW_GRADE, grade, "en")["message"]
        == "new grade 5 - Matematyka (Sprawdzian)"
    )
    absence = {"student": "Ola", "date": "2026-11-02", "excused": False, "lesson_no": 3}
    assert (
        librus_logbook.describe(EVENT_NEW_ABSENCE, absence, "pl")["message"]
        == "nieobecność nieusprawiedliwiona: 02.11, lekcja 3"
    )
    change = {"date": "2026-11-03", "change": "room_change", "subject": "Fizyka", "lesson_no": 2}
    entry = librus_logbook.describe(EVENT_TIMETABLE_CHANGED, change, "en")
    assert entry["name"] == "Librus"
    assert entry["message"] == "room change: Fizyka - 2026-11-03, lesson 2"
    assert (
        librus_logbook.describe(EVENT_NEW_ABSENCE, absence, "pt")["message"]
        == "falta injustificada: 02.11, aula 3"
    )
    assert librus_logbook.describe(EVENT_NEW_GRADE, grade, "uk")["message"].startswith("нова оцінка 5")
    assert librus_logbook.describe(EVENT_NEW_GRADE, grade, "ru")["message"].startswith("новая оценка 5")
    assert librus_logbook.describe(EVENT_NEW_GRADE, grade, "de")["message"].startswith("neue Note 5")
    assert librus_logbook.describe(EVENT_NEW_GRADE, grade, "fr")["message"].startswith("nouvelle note 5")
    # Any other language: English.
    assert librus_logbook.describe(EVENT_NEW_GRADE, grade, "sv")["message"].startswith("new grade")
    # Every language has every phrase.
    words = librus_logbook._WORDS
    assert all(set(table) == set(words["en"]) for table in words.values())


def test_every_event_has_a_logbook_line() -> None:
    described: list[str] = []
    librus_logbook.async_describe_events(
        None, lambda domain, event_type, _cb: described.append(event_type)  # type: ignore[arg-type]
    )
    assert set(described) == set(librus_logbook.DESCRIBERS)
    # Every builder copes with an empty event.
    for event_type in librus_logbook.DESCRIBERS:
        for language in ("pl", "en"):
            assert librus_logbook.describe(event_type, {}, language)["message"]


# --- system health ------------------------------------------------------------


async def test_system_health(hass, aioclient_mock) -> None:
    for url in (
        "https://synergia.librus.pl/",
        "https://api.librus.pl/",
        "https://wiadomosci.librus.pl/",
    ):
        aioclient_mock.get(url, text="")
    assert await async_setup_component(hass, "system_health", {})
    entry = await setup_integration(hass, build_mock_client())
    info = await get_system_health_info(hass, DOMAIN)
    for key, value in info.items():
        if asyncio.iscoroutine(value):
            info[key] = await value
    assert info["students"] == "1 / 1"
    assert info["connection"] == entry.runtime_data.status
    assert info["last_update"] == (entry.runtime_data.last_success_at or "-")
    assert info["can_reach_synergia"] == "ok"
    assert info["can_reach_login"] == "ok"
    assert info["can_reach_messages"] == "ok"
    assert set(info) == set(_strings()["system_health"]["info"])
