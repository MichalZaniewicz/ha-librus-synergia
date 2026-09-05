"""Manual smoke test for the Librus API client.

NOT part of the Home Assistant integration - dev-only, lives outside
custom_components/ so HA never loads it. Run against a real Librus account to
verify the wire protocol (reverse-engineered from `emsi/librus_pyapi`, the
currently-working flow after Librus's 2026-03-28 auth change - see
librus_api/const.py's module docstring) and to close the remaining empirical
gaps: what Notes[].Positive enumerates, how grade values like "5+"/"4-"/"bz"
should be parsed, whether HomeWorkAssignments is distinct/usable, and the
real Wiadomości (messages) response shapes.

Usage (PowerShell):
    $env:LIBRUS_USERNAME = "..."
    $env:LIBRUS_PASSWORD = "..."
    .venv\\Scripts\\python.exe scripts\\manual_smoke_test.py

Usage (bash):
    LIBRUS_USERNAME=... LIBRUS_PASSWORD=... .venv/Scripts/python.exe scripts/manual_smoke_test.py

Credentials are read from the environment only - never hardcode them here,
never commit a .env file with real values, and this script never writes
anything to disk itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Import the client package directly from the integration source tree -
# this script only needs aiohttp, not a full Home Assistant install.
# Appended (not inserted at 0): that directory also contains a calendar.py
# (the HA calendar platform), which would otherwise shadow the stdlib
# `calendar` module that aiohttp's own dependency chain imports.
sys.path.append(
    str(Path(__file__).resolve().parent.parent / "custom_components" / "librus_synergia")
)

import aiohttp  # noqa: E402

from librus_api import LibrusApiClient, LibrusError  # noqa: E402


def _print_section(title: str) -> None:
    print(f"\n{'=' * 10} {title} {'=' * 10}")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False)[:4000])


async def main() -> int:
    username = os.environ.get("LIBRUS_USERNAME")
    password = os.environ.get("LIBRUS_PASSWORD")
    if not username or not password:
        print("Set LIBRUS_USERNAME and LIBRUS_PASSWORD environment variables first.")
        return 1

    async with aiohttp.ClientSession() as session:
        client = LibrusApiClient(session, username)

        _print_section("Login (portalRodzina -> Authorization form -> redirect chain)")
        try:
            session_data = await client.async_login(password)
        except LibrusError as err:
            print(f"LOGIN FAILED: {type(err).__name__}: {err}")
            return 1
        print(f"OK - {len(session_data.cookies)} cookies persisted, logged_in_at={session_data.logged_in_at}")

        today = date.today()
        week_start = today - timedelta(days=today.weekday())

        endpoints: list[tuple[str, "asyncio.Future"]] = [
            ("Me", client.async_get_me()),
            ("Grades", client.async_get_grades()),
            ("Grades/Categories", client.async_get_grade_categories()),
            ("Notes", client.async_get_notes()),
            ("Attendances", client.async_get_attendances()),
            ("AttendanceTypes", client.async_get_attendance_types()),
            ("Timetables (this week)", client.async_get_timetable(week_start)),
            ("HomeWorks", client.async_get_homeworks()),
            ("HomeWorkAssignments (UNVERIFIED)", client.async_get_homework_assignments()),
            ("SchoolNotices", client.async_get_school_notices()),
            ("LuckyNumbers", client.async_get_lucky_number()),
            ("Subjects", client.async_get_subjects()),
            ("Teachers/Users", client.async_get_teachers()),
            ("Classrooms", client.async_get_classrooms()),
        ]

        for label, coro in endpoints:
            _print_section(label)
            try:
                payload = await coro
            except LibrusError as err:
                print(f"FAILED: {type(err).__name__}: {err}")
                continue
            _print_json(payload)

        _print_section("Messages bootstrap (Wiadomości)")
        try:
            has_access = await client.async_bootstrap_messages()
            print(f"Access: {has_access}")
        except LibrusError as err:
            print(f"FAILED: {type(err).__name__}: {err}")
            has_access = False

        if has_access:
            for label, coro in [
                ("Unread messages count", client.async_get_unread_messages_count()),
                ("Messages (inbox, limit 10)", client.async_get_messages(limit=10)),
            ]:
                _print_section(label)
                try:
                    payload = await coro
                except LibrusError as err:
                    print(f"FAILED: {type(err).__name__}: {err}")
                    continue
                _print_json(payload)

        _print_section("Done")
        print(
            "Check above: Notes[].Positive values (which is positive/neutral/"
            "negative), a few Grades[].Grade values (symbols like 5+/4-/bz), "
            "whether HomeWorks/HomeWorkAssignments look distinct from each "
            "other, and the Messages response field names. Update "
            "librus_api/const.py, librus_api/models.py and coordinator.py's "
            "parsers if anything here differs from what they currently "
            "assume."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
