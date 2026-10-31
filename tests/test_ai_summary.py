"""Tests for the weekly AI summary (ai_summary.py and its entities)."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, time
from unittest.mock import patch

import pytest

from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.librus_synergia.ai_summary import (
    TOKEN_LIMIT_HINT,
    build_context,
    build_instructions,
    build_structure,
    is_empty_week,
    last_due,
    parse_result,
)
from custom_components.librus_synergia.const import (
    CONF_AI_AUDIENCE,
    CONF_AI_LANGUAGE,
    CONF_AI_TASK_ENTITY,
    CONF_AI_TIME,
    CONF_AI_WEEKDAY,
    DOMAIN,
    EVENT_WEEKLY_SUMMARY,
)

from librus_synergia.models import DescriptiveGradeData, PlanChangeData

from .conftest import build_mock_client, make_config_entry, setup_integration

# Sunday 2026-10-04 is the run day in these tests: the window is 28.09-4.10,
# the coming week 5.10-11.10.
_TODAY = date(2026, 10, 4)

_AI_OPTIONS = {
    CONF_AI_TASK_ENTITY: "ai_task.test",
    CONF_AI_AUDIENCE: "parent",
    CONF_AI_WEEKDAY: "7",
    CONF_AI_TIME: "18:00:00",
}

_ANSWER = {
    "headline": "Dobry tydzień z geografii",
    "status": "good",
    "grades": "Szóstka z geografii.",
    "grades_status": "good",
    "attendance": "Jedna nieobecność.",
    "attendance_status": "caution",
    "behaviour": "Bez uwag.",
    "behaviour_status": "good",
    "next_week": "Sprawdzian z matematyki we wtorek.",
    "next_week_status": "ok",
    "advice": ["Usprawiedliwić nieobecność", "Powtórzyć działy 1-2"],
    "warning": "",
}


def _client():
    return build_mock_client(
        async_get_grades={
            "Grades": [
                {
                    "Id": 1,
                    "Grade": "6",
                    "Category": {"Id": 10},
                    "Subject": {"Id": 42002},
                    "Semester": 1,
                    "AddDate": "2026-10-01 10:00:00",
                },
                {
                    "Id": 2,
                    "Grade": "4",
                    "Category": {"Id": 10},
                    "Subject": {"Id": 42002},
                    "Semester": 1,
                    "AddDate": "2026-09-15 10:00:00",
                },
            ]
        },
        async_get_grade_categories={
            "Categories": [{"Id": 10, "Name": "Sprawdzian", "CountToTheAverage": True, "Weight": 3}]
        },
        async_get_subjects={
            "Subjects": [{"Id": 42002, "Name": "Geografia"}, {"Id": 42005, "Name": "Matematyka"}]
        },
        async_get_attendances={
            "Attendances": [
                {"Id": 1, "Date": "2026-09-30", "Semester": 1, "Type": {"Id": 1}},
                {"Id": 2, "Date": "2026-09-30", "Semester": 1, "Type": {"Id": 100}},
                {"Id": 3, "Date": "2026-09-10", "Semester": 1, "Type": {"Id": 3}},
            ]
        },
        async_get_attendance_types={
            "Types": [
                {"Id": 100, "Name": "Obecność", "IsPresenceKind": True},
                {"Id": 1, "Name": "Nieobecność", "IsPresenceKind": False},
                {"Id": 3, "Name": "Nieobecność uspr.", "IsPresenceKind": False},
            ]
        },
        async_get_homeworks={
            "HomeWorks": [
                {
                    "Id": 7,
                    "Content": "Działy 1-2",
                    "Date": "2026-10-06",
                    "Category": {"Id": 9368},
                    "Subject": {"Id": 42005},
                },
                {
                    "Id": 8,
                    "Content": "Za daleko",
                    "Date": "2026-10-20",
                    "Category": {"Id": 9368},
                    "Subject": {"Id": 42005},
                },
            ]
        },
        async_get_homework_categories={"Categories": [{"Id": 9368, "Name": "Sprawdzian"}]},
    )


def _register_ai_task(hass, answer=_ANSWER):
    calls: list[dict] = []

    async def _handler(call):
        calls.append(dict(call.data))
        return {"data": answer}

    hass.services.async_register(
        "ai_task", "generate_data", _handler, supports_response=SupportsResponse.ONLY
    )
    return calls


def _entity_id(hass, platform: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


async def test_build_context_covers_this_week_and_next(hass) -> None:
    entry = await setup_integration(hass, _client())
    data = entry.runtime_data.data

    context = build_context(data, _TODAY, weighted=True, include_news=False, student="Ola K")

    # Weekdays spelled out like every other date in the snapshot.
    assert context["this_week"] == {"from": "2026-09-28 Mon", "to": "2026-10-04 Sun"}
    assert context["next_week"]["from"] == "2026-10-05 Mon"
    assert [g["value"] for g in context["grades"]] == ["6"]
    assert context["grades"][0]["date"] == "2026-10-01 Thu"
    assert context["today"] == "2026-10-04 Sun"
    assert context["grades"][0]["subject"] == "Geografia"
    assert context["grades"][0]["category"] == "Sprawdzian"
    assert context["averages"] == [{"subject": "Geografia", "now": 5.0, "week_ago": 4.0}]
    attendance = context["attendance"]
    assert attendance["this_week"]["absent_unexcused"] == 1
    assert attendance["this_week"]["present"] == 1
    assert attendance["open_unexcused"] == 1
    assert [item["content"] for item in context["next_week"]["agenda"]] == ["Działy 1-2"]
    assert context["next_week"]["agenda"][0]["subject"] == "Matematyka"
    assert context["next_week"]["agenda"][0]["date"] == "2026-10-06 Tue"
    assert "school_news" not in context
    assert not is_empty_week(context)


async def test_build_context_includes_this_weeks_descriptive_grades(hass) -> None:
    entry = await setup_integration(hass, _client())
    data = dataclasses.replace(
        entry.runtime_data.data,
        descriptive_grades=[
            DescriptiveGradeData(
                id=1, subject_id=None, value="6", skill_id=55, category_id=None,
                add_date="2026-10-02 10:00:00", skill="Rytmika", date="2026-10-02",
                comments=["Brawo"],
            ),
            # Last week - not in this summary.
            DescriptiveGradeData(
                id=2, subject_id=None, value="5", skill_id=56, category_id=None,
                add_date="2026-09-20 10:00:00", skill="Śpiew", date="2026-09-20",
            ),
        ],
    )

    context = build_context(data, _TODAY, weighted=True, include_news=False, student=None)

    descriptive = [g for g in context["grades"] if g.get("kind") == "descriptive"]
    assert descriptive == [
        {"date": "2026-10-02 Fri", "value": "6", "category": "Rytmika", "comment": "Brawo", "kind": "descriptive"}
    ]


async def test_build_context_has_this_weeks_cancelled_and_substituted_lessons(hass) -> None:
    entry = await setup_integration(hass, _client())
    base = entry.runtime_data.data

    def change(change_id: int, day: str, cancelled: bool) -> PlanChangeData:
        return PlanChangeData(
            id=change_id, original_date=day, original_lesson_no=2, original_subject_id=42002,
            original_teacher_id=None, original_classroom=None, cancelled=cancelled, shifted=False,
            date=None if cancelled else day, lesson_no=None if cancelled else 2,
            teacher_id=None,
        )

    data = dataclasses.replace(
        base,
        plan_changes=[
            change(1, "2026-09-15", True),  # earlier in the year - totals only
            change(2, "2026-09-30", True),
            change(3, "2026-10-02", False),
        ],
    )

    attendance = build_context(data, _TODAY, weighted=True, include_news=False, student=None)["attendance"]

    subject = base.subjects.get(42002)
    week = attendance["cancelled_or_substituted_this_week"]
    assert [(c["date"], c["change"]) for c in week] == [
        ("2026-09-30 Wed", "cancelled"),
        ("2026-10-02 Fri", "substitute"),
    ]
    assert all(c.get("subject") == subject for c in week)
    assert attendance["school_year_cancelled"] == 2
    assert attendance["school_year_substituted"] == 1


async def test_build_context_has_revision_topics_and_missed_lessons(hass) -> None:
    client = _client()
    client.async_get_lessons.return_value = {"Lessons": [{"Id": 501, "Subject": {"Id": 42005}}]}
    client.async_get_realizations.return_value = {
        "Realizations": [
            {"Id": "t1", "Lesson": {"Id": 501}, "LessonNo": 2, "Date": "2026-09-29", "Topic": "Ułamki"},
            {"Id": "t2", "Lesson": {"Id": 501}, "LessonNo": 3, "Date": "2026-09-30", "Topic": "Procenty"},
        ]
    }
    client.async_get_attendances.return_value = {
        "Attendances": [
            {"Id": 1, "Date": "2026-09-30", "LessonNo": "3", "Semester": 1, "Type": {"Id": 1}},
        ]
    }
    client.async_get_school_files.return_value = {
        "Data": [{"id": "f1", "displayName": "Regulamin wycieczek", "addedOnDate": "2026-10-02 10:00:00"}]
    }
    entry = await setup_integration(hass, client)

    context = build_context(entry.runtime_data.data, _TODAY, weighted=True, include_news=False, student=None)

    (test,) = context["next_week"]["tests_to_revise"]
    assert test["subject"] == "Matematyka"
    assert test["topics"] == ["Ułamki", "Procenty (missed)"]
    assert [t["topic"] for t in context["attendance"]["missed_lessons_topics"]] == ["Procenty"]
    assert context["new_school_documents"] == ["Regulamin wycieczek"]


async def test_empty_week_is_detected(hass) -> None:
    entry = await setup_integration(hass, build_mock_client())
    context = build_context(
        entry.runtime_data.data, _TODAY, weighted=True, include_news=True, student=None
    )
    assert is_empty_week(context)


def test_instructions_follow_the_audience() -> None:
    context = {"student": "Ola Kowalska"}
    parent = build_instructions(context, _TODAY, "pl", "parent", None)
    student = build_instructions(context, _TODAY, "pl", "student", "egzamin w tym roku")
    assert "PARENT of Ola" in parent
    assert "directly to Ola" in student
    assert "Polish" in student
    assert "egzamin w tym roku" in student


def test_structure_and_parse_respect_the_news_option() -> None:
    assert "school_news" not in build_structure(False)
    assert "school_news_status" in build_structure(True)
    answer = {**_ANSWER, "school_news": "Zebranie w środę.", "school_news_status": "ok"}
    assert "school_news" not in parse_result(answer, False)["sections"]
    parsed = parse_result(answer, True)
    assert parsed["sections"]["school_news"] == {"status": "ok", "text": "Zebranie w środę."}
    assert parsed["sections"]["attendance"]["status"] == "caution"
    assert parsed["warning"] is None
    # Plain-text answer from a core without structured output.
    text = parse_result("Krótko: dobry tydzień. Reszta tekstu.", False)
    assert text["headline"] == "Krótko: dobry tydzień"
    assert text["summary"].startswith("Krótko")


def test_last_due() -> None:
    tz = dt_util.get_time_zone("Europe/Warsaw")
    at = time(18, 0)
    # Sunday 18:30 -> today 18:00; Sunday 17:00 -> a week earlier.
    assert last_due(datetime(2026, 10, 4, 18, 30, tzinfo=tz), 7, at).date() == date(2026, 10, 4)
    assert last_due(datetime(2026, 10, 4, 17, 0, tzinfo=tz), 7, at).date() == date(2026, 9, 27)
    # Wednesday -> the previous Sunday.
    assert last_due(datetime(2026, 10, 7, 9, 0, tzinfo=tz), 7, at).date() == date(2026, 10, 4)


async def test_no_entities_without_ai_task(hass) -> None:
    entry = await setup_integration(hass, _client())
    assert entry.runtime_data.weekly_summary is None
    assert _entity_id(hass, "sensor", f"{entry.entry_id}_weekly_summary") is None
    assert _entity_id(hass, "button", f"{entry.entry_id}_weekly_summary_generate") is None
    assert _entity_id(hass, "switch", f"{entry.entry_id}_weekly_summary_enabled") is None


async def test_button_generates_summary_and_fires_event(hass) -> None:
    calls = _register_ai_task(hass)
    events = []
    hass.bus.async_listen(EVENT_WEEKLY_SUMMARY, events.append)
    entry = await setup_integration(hass, _client(), options=_AI_OPTIONS)

    sensor_id = _entity_id(hass, "sensor", f"{entry.entry_id}_weekly_summary")
    button_id = _entity_id(hass, "button", f"{entry.entry_id}_weekly_summary_generate")
    switch_id = _entity_id(hass, "switch", f"{entry.entry_id}_weekly_summary_enabled")
    assert sensor_id and button_id and switch_id
    assert hass.states.get(switch_id).state == "on"

    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await hass.async_block_till_done()

    assert len(calls) == 1
    assert calls[0]["entity_id"] == "ai_task.test"
    assert "grades_status" in calls[0]["structure"]
    assert "school_news" not in calls[0]["structure"]
    assert "PARENT of Ola" in calls[0]["instructions"]

    state = hass.states.get(sensor_id)
    assert state.state == "Dobry tydzień z geografii"
    assert state.attributes["status"] == "good"
    assert list(state.attributes["sections"]) == ["grades", "attendance", "behaviour", "next_week"]
    assert state.attributes["advice"] == ["Usprawiedliwić nieobecność", "Powtórzyć działy 1-2"]
    assert state.attributes["error"] is None

    assert len(events) == 1
    assert events[0].data["manual"] is True
    assert events[0].data["student"] == "Ola Kowalska"
    assert events[0].data["labels"]["sections"]["grades"]


async def test_language_option_overrides_the_home_assistant_language(hass) -> None:
    """Automatic follows HA's server language; the option forces another one."""
    assert hass.config.language == "en"
    calls = _register_ai_task(hass)
    events = []
    hass.bus.async_listen(EVENT_WEEKLY_SUMMARY, events.append)
    entry = await setup_integration(
        hass, _client(), options={**_AI_OPTIONS, CONF_AI_LANGUAGE: "pl"}
    )
    button_id = _entity_id(hass, "button", f"{entry.entry_id}_weekly_summary_generate")
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await hass.async_block_till_done()

    assert "Write every field in Polish" in calls[0]["instructions"]
    assert events[0].data["labels"]["sections"]["grades"] == "Oceny"

    summary = entry.runtime_data.weekly_summary
    summary._language = "auto"
    assert summary.language == "en"


async def test_scheduled_run_fires_once_at_the_chosen_time(hass, freezer) -> None:
    # Tests run in US/Pacific: Sunday 2026-10-04 17:59 local.
    freezer.move_to("2026-10-05T00:59:00+00:00")
    calls = _register_ai_task(hass)
    entry = await setup_integration(hass, _client(), options=_AI_OPTIONS)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == []

    freezer.move_to("2026-10-05T01:00:00+00:00")
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(calls) == 1

    # Later coordinator updates the same evening do not run it again.
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(calls) == 1


async def test_switch_off_pauses_the_scheduled_run(hass, freezer) -> None:
    freezer.move_to("2026-10-05T00:59:00+00:00")
    calls = _register_ai_task(hass)
    entry = await setup_integration(hass, _client(), options=_AI_OPTIONS)
    switch_id = _entity_id(hass, "switch", f"{entry.entry_id}_weekly_summary_enabled")

    await hass.services.async_call("switch", "turn_off", {"entity_id": switch_id}, blocking=True)
    freezer.move_to("2026-10-05T01:00:00+00:00")
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == []
    sensor_id = _entity_id(hass, "sensor", f"{entry.entry_id}_weekly_summary")
    assert hass.states.get(sensor_id).attributes["paused"] is True


async def test_scheduled_run_skips_an_empty_week(hass, freezer) -> None:
    freezer.move_to("2026-10-05T00:59:00+00:00")
    calls = _register_ai_task(hass)
    await setup_integration(hass, build_mock_client(), options=_AI_OPTIONS)

    freezer.move_to("2026-10-05T01:00:00+00:00")
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == []


async def test_provider_failure_is_reported_on_the_sensor(hass) -> None:
    async def _failing(call):
        raise RuntimeError("quota exceeded")

    hass.services.async_register(
        "ai_task", "generate_data", _failing, supports_response=SupportsResponse.ONLY
    )
    entry = await setup_integration(hass, _client(), options=_AI_OPTIONS)
    button_id = _entity_id(hass, "button", f"{entry.entry_id}_weekly_summary_generate")

    try:
        await hass.services.async_call(
            "button", "press", {"entity_id": button_id}, blocking=True
        )
    except Exception:  # noqa: BLE001 - surfaced as an error toast in the UI
        pass
    sensor_id = _entity_id(hass, "sensor", f"{entry.entry_id}_weekly_summary")
    assert "quota exceeded" in hass.states.get(sensor_id).attributes["error"]
    assert TOKEN_LIMIT_HINT not in hass.states.get(sensor_id).attributes["error"]


async def test_output_token_limit_error_says_what_to_change(hass) -> None:
    async def _cut_off(call):
        raise HomeAssistantError(
            "Claude reached the output token limit before completing the response."
        )

    hass.services.async_register(
        "ai_task", "generate_data", _cut_off, supports_response=SupportsResponse.ONLY
    )
    entry = await setup_integration(hass, _client(), options=_AI_OPTIONS)
    button_id = _entity_id(hass, "button", f"{entry.entry_id}_weekly_summary_generate")

    with pytest.raises(HomeAssistantError, match="maximum tokens"):
        await hass.services.async_call(
            "button", "press", {"entity_id": button_id}, blocking=True
        )
    sensor_id = _entity_id(hass, "sensor", f"{entry.entry_id}_weekly_summary")
    error = hass.states.get(sensor_id).attributes["error"]
    assert error.startswith("Claude reached the output token limit")
    assert TOKEN_LIMIT_HINT in error


async def test_parent_and_student_get_their_own_text(hass) -> None:
    calls: list[dict] = []

    async def _handler(call):
        calls.append(dict(call.data))
        headline = "Do rodzica" if "PARENT of Ola" in call.data["instructions"] else "Do Oli"
        return {"data": {**_ANSWER, "headline": headline}}

    hass.services.async_register(
        "ai_task", "generate_data", _handler, supports_response=SupportsResponse.ONLY
    )
    events = []
    hass.bus.async_listen(EVENT_WEEKLY_SUMMARY, events.append)
    entry = await setup_integration(
        hass, _client(), options={**_AI_OPTIONS, CONF_AI_AUDIENCE: "both"}
    )
    button_id = _entity_id(hass, "button", f"{entry.entry_id}_weekly_summary_generate")
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await hass.async_block_till_done()

    assert len(calls) == 2
    assert [(e.data["audience"], e.data["headline"]) for e in events] == [
        ("parent", "Do rodzica"),
        ("student", "Do Oli"),
    ]
    state = hass.states.get(_entity_id(hass, "sensor", f"{entry.entry_id}_weekly_summary"))
    assert state.state == "Do rodzica"
    assert state.attributes["audience"] == "parent"
    versions = state.attributes["versions"]
    assert versions["student"]["headline"] == "Do Oli"
    assert versions["student"]["advice"] == _ANSWER["advice"]
    assert list(versions) == ["parent", "student"]


async def test_one_audience_has_no_versions_and_old_store_still_loads(hass, hass_storage) -> None:
    entry = make_config_entry(options={**_AI_OPTIONS, CONF_AI_AUDIENCE: "student"})
    entry.add_to_hass(hass)
    hass_storage[f"{DOMAIN}.weekly_summary.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.weekly_summary.{entry.entry_id}",
        # Saved before there could be two texts: a single "result".
        "data": {"result": {"headline": "Stary tekst", "audience": "student"}},
    }
    with patch("custom_components.librus_synergia.LibrusApiClient", return_value=_client()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get(_entity_id(hass, "sensor", f"{entry.entry_id}_weekly_summary"))
    assert state.state == "Stary tekst"
    assert "versions" not in state.attributes
