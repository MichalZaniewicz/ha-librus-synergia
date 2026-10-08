# Librus Synergia (unofficial) for Home Assistant

<p align="center">
  <img src="docs/hero-banner.svg" alt="Librus Synergia">
</p>

<p align="center">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/MichalZaniewicz/ha-librus-synergia"></a>
  <a href="https://github.com/MichalZaniewicz/ha-librus-synergia/releases"><img alt="Release" src="https://img.shields.io/github/v/release/MichalZaniewicz/ha-librus-synergia"></a>
</p>

A HACS-installable Home Assistant integration for [Librus Synergia](https://synergia.librus.pl/) - the Polish school e-register - pulling grades, attendance, behaviour notices, timetable, agenda, announcements, messages, and school/class info in as sensors and calendars.

> [!TIP]
> ⭐ **Enjoying this integration?** Every star is real motivation to keep building new features :)
>
> ☕ Want to say thanks another way? You can [buy me a coffee](https://buymeacoffee.com/zanula).

<!-- The badge lives OUTSIDE the alert on purpose: Home Assistant/HACS rewrites a GitHub alert
into <ha-alert> and drops every child whose textContent is empty, which silently removes any
<img> placed inside it. -->

[![Star this repo](https://img.shields.io/github/stars/MichalZaniewicz/ha-librus-synergia?style=for-the-badge&logo=github&label=STAR%20THIS%20REPO&labelColor=555555&color=ffc107)](https://github.com/MichalZaniewicz/ha-librus-synergia) [![Buy me a coffee](https://img.shields.io/badge/BUY%20ME%20A%20COFFEE-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/zanula)

<p align="center">
  <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=MichalZaniewicz&repository=ha-librus-synergia&category=integration"><img alt="Open your Home Assistant instance and open a repository inside the Home Assistant Community Store." src="https://my.home-assistant.io/badges/hacs_repository.svg"></a>
</p>

**Contents:** [Why this exists](#why-this-exists) · [Credential model](#credential-model-read-this-before-installing) · [Installation](#installation) · [Weekly AI summary](#weekly-ai-summary) · [Ask Assist](#ask-assist-about-school) · [Cards](#custom-lovelace-cards) · [Entities](#entities) · [Blueprints](#automation-blueprints) · [Services](#services) · [Known limitations](#known-limitations--unverified-details) · [Related projects](#related-projects)

## Why this exists

An integration for Librus already exists ([`LukMaverick/LibrusSynergiaHA`](https://github.com/LukMaverick/LibrusSynergiaHA), built on [`RustySnek/librus-apix`](https://github.com/RustySnek/librus-apix)), but it logs into the legacy HTML Synergia portal, which can demand a reCAPTCHA a human has to solve - unworkable for something meant to sync unattended in the background.

This integration instead uses the login flow Librus's own website/app uses for its private API gateway - reverse-engineered from [`emsi/librus_pyapi`](https://github.com/emsi/librus_pyapi) (MIT), which was itself updated to track a Librus authentication change on 2026-03-28. Confirmed live (2026-09-05) to complete without a captcha challenge for a normal login. An older password-grant login (the one documented by the open-source [`szkolny-eu/szkolny-android`](https://github.com/szkolny-eu/szkolny-android) app) no longer works: Librus now answers it with `unsupported_grant_type`.

The Librus client itself lives in a separate library, [**librus-synergia**](https://github.com/MichalZaniewicz/librus-synergia) (`pip install librus-synergia`). You can use it outside Home Assistant, and it comes with an [unofficial Librus API reference](https://michalzaniewicz.github.io/librus-synergia/).

**This is an unofficial integration using a private API and may violate Librus's Terms of Service. Use your own account at your own risk.**

<p align="center">
  <img src="docs/credentials-banner.svg" alt="Credential model">
</p>

## Credential model (read this before installing)

Unlike some cloud-polling integrations, **your Librus password is stored** in Home Assistant's config storage, alongside the session. This is a deliberate tradeoff, not an oversight: the integration keeps its session alive through Librus's own session refresh (every couple of hours), but once a session has lapsed - Home Assistant was off for a while, Librus restarted its sessions - the only way back in is a fresh login, and doing that unattended needs the password. Your password never leaves your Home Assistant instance.

A long-lived device-recognition cookie is also persisted and re-sent on every login - this is believed to be why a normal login skips any captcha/2FA challenge, so treat it as load-bearing, not just a convenience.

If you change your Librus password or mistype the login, use the integration's **Reconfigure** option (⋮ menu on the entry, in Settings → Devices & services) rather than deleting and re-adding it - that keeps your entity ids, dashboards and automations intact. Reconfigure refuses to repoint an entry at a genuinely different Librus account; add a new integration entry instead if you want to add another student.

<p align="center">
  <img src="docs/installation-banner.svg" alt="Installation">
</p>

## Installation

1. HACS → ⋮ → **Custom repositories** → add this repo as category **Integration** (or use the badge above).
2. Install **Librus Synergia (unofficial)**, restart Home Assistant.
3. Settings → Devices & services → **Add integration** → search "Librus Synergia".
4. Enter your Librus **login** (e.g. `1234567u` - a direct student/account login, not a Librus Portal e-mail) and password.

Each child/student is a separate login and a separate integration entry.

<p align="center">
  <img src="docs/ai-summary-banner.svg" alt="Weekly AI summary">
</p>

## Weekly AI summary

Once a week, an AI model of your choice (Gemini, OpenAI, Claude, or a local
Ollama) writes a summary of the school week: grades, attendance, behaviour,
the week ahead and, if you want, the important points from the school's
messages. A headline, a status per section, and 2-4 to-dos for the coming
week. It goes through Home Assistant's own AI Task (2025.8+), with no API key
in this integration and no extra Librus requests.

![Weekly AI summary card](https://raw.githubusercontent.com/MichalZaniewicz/ha-librus-synergia-cards/main/docs/screenshots/librus-ai-summary-card-dark.png)

**Setup in short:** add an AI provider to Home Assistant, then *Librus
Synergia → Configure → Weekly AI summary* and pick its AI Task entity.
**[How to set it up, what gets sent, troubleshooting](https://github.com/MichalZaniewicz/ha-librus-synergia/wiki/Weekly-AI-summary)**

<p align="center">
  <img src="docs/assist-banner.svg" alt="Ask Assist about school">
</p>

## Ask Assist about school

Ask your Assist conversation agent about school in plain words, by voice or in
the chat: *"What does Ola have tomorrow?"*, *"When is the next maths test?"*,
*"How many absences still need an excuse?"*. Seven read-only tools, answered
from data the integration already has.

**Setup in short:** your AI integration → the conversation agent's
**Configure** → under *Control Home Assistant* tick **Librus Synergia**.
**[Setup, the tools, what gets sent](https://github.com/MichalZaniewicz/ha-librus-synergia/wiki/Ask-Assist)**

<p align="center">
  <img src="docs/cards-banner.svg" alt="Custom Lovelace cards">
</p>

## Custom Lovelace cards

Want a dashboard without wiring these sensors into generic entity cards by hand?
**[Librus Synergia Cards](https://github.com/MichalZaniewicz/ha-librus-synergia-cards)**
is a companion HACS repo with 61 purpose-built cards: grades and trends, attendance,
timetable, agenda, messages, announcements, a "Today" overview, and a few playful ones.
Each card finds your child's device on its own (no YAML for one student), follows your
Home Assistant theme and language (English, Polish).

![Librus Synergia Cards preview](https://raw.githubusercontent.com/MichalZaniewicz/ha-librus-synergia-cards/main/docs/screenshots/cards-overview-dark.png)

<p align="center">
  <img src="docs/entities-banner.svg" alt="Entities">
</p>

## Entities

Each child gets a device with these entities:

- **Grades** - the overall and per-subject averages (weighted or arithmetic), a report-card forecast with a *Grade at risk* alarm, and text, point and descriptive grades.
- **Attendance** - absences (excused and still to excuse), the justifications you sent, and attendance per subject.
- **Timetable and the school day** - the next and current lesson, when school starts and ends, *School day today/tomorrow*, *At school*, and calendars with the timetable (substitutions and room changes marked) and days off.
- **Homework and tests** - the next test, homework assignments (also as a to-do list), the Agenda calendar, lesson topics and school trips.
- **Behaviour** - behaviour notes and the behaviour grade.
- **Messages and the school** - unread messages and announcements, school documents, the lucky number (and whether it's your child's), school and class details.
- **Fun** - attendance, behaviour and good-grade streaks, and a rank.
- **Events** - one event entity per kind of news (new grade, new absence, lesson change, ...) for automations built in the UI.
- **Diagnostics** - connection status and the last successful update.

**[Every entity with its attributes](https://github.com/MichalZaniewicz/ha-librus-synergia/wiki/Entities)** is on the wiki.

New grades, notes, absences, messages, announcements, Agenda entries, homework, school trips and documents fire Home Assistant bus events, and so do a changed lesson, a changed or removed Agenda entry, a decided absence justification and a changed grade forecast. Every event says which child it's about, and nothing fires for what was already there when you set the integration up. **[All events and their data](https://github.com/MichalZaniewicz/ha-librus-synergia/wiki/Events)**. Ready-made [blueprints](#automation-blueprints) wrap them for you.

The integration's **Configure** menu has two parts. *Settings* sets the poll interval (default 20 minutes), whether the average sensors show the weighted or the arithmetic average, the **grade forecast thresholds** (minimum average for a 2, 3, 4, 5 and 6; default `1.75, 2.75, 3.75, 4.75, 5.50`), and whether to fetch private messages at all - turn *Fetch private messages* off if your school doesn't use Wiadomości or you don't want those extra requests (the Unread messages sensor then reports `unavailable`). Two more switches there: **Smart polling** keeps the poll interval on school days between 06:00 and 22:00 but fetches at most hourly on days without lessons and at most every 3 hours at night (the `librus_synergia.refresh` action fetches straight away), and **Hide subjects without grades** creates a subject's average sensor only once it has its first grade. Four switches turn off parts you don't use - announcements, behaviour grade, descriptive grades and the free-days calendar (the sensor then reports `unavailable`; the calendar isn't created) - and **Quiet hours** (off by default, 23:00-06:00 unless changed) skips fetching overnight entirely; while they're on, even the refresh action waits. The poll interval can be 10-180 minutes, and you can override the class register number there. *Weekly AI summary* sets up the [weekly AI summary](#weekly-ai-summary).

**When Librus is down.** The integration keeps the last good response of every part of Librus (in Home Assistant's own storage). If one part fails - grades, the timetable, subject names - its last good copy is shown instead of an empty sensor. If Librus doesn't answer at all, every entity keeps showing the last data (the *Connection status* sensor says `stale`) for up to 3 days; after two failed refreshes in a row the next attempts are spaced out (twice the poll interval, then four times, up to 2 hours), so an outage isn't met with a login attempt every cycle - the `librus_synergia.refresh` action still tries straight away. If Home Assistant starts while Librus is down, the data is rebuilt from the saved responses instead of the integration failing to load. Long lists in attributes (grade logs, `recent` lists, breakdowns) aren't written to the recorder's history, which keeps the database small; templates and cards read them as before.

**Grade averages over the whole year.** The integration also writes the history of the overall and per-subject averages, rebuilt from the grades' dates, into Home Assistant's long-term statistics - so a chart covers the school year from its first grade, not just the time since you installed the integration. Add a **Statistics graph** card and pick e.g. *"Ola Kowalska - średnia Matematyka"* (statistic type *Mean*). It's rewritten whenever the grades change; it needs the Recorder, which is on by default.

<p align="center">
  <img src="docs/blueprints-banner.svg" alt="Automation blueprints">
</p>

### Automation blueprints

Ready-to-import blueprints under
[`blueprints/automation/librus_synergia/`](blueprints/automation/librus_synergia/) wrap
the events above so you don't have to write the YAML yourself - each just asks for an
*action* (e.g. "Send a notification"):

<details>
<summary><b>Show all 28 blueprints</b></summary>

| Blueprint | What it does | |
| --- | --- | --- |
| [New Grade Notification](blueprints/automation/librus_synergia/new_grade_notification.yaml) | Runs your action with a summary whenever `librus_synergia_new_grade` fires - optionally with a second line showing the category, weight, date and teacher's comment. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_grade_notification.yaml) |
| [New Behaviour Notice Notification](blueprints/automation/librus_synergia/new_note_notification.yaml) | Runs your action with a one-line summary whenever `librus_synergia_new_note` fires. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_note_notification.yaml) |
| [New Announcement Notification](blueprints/automation/librus_synergia/new_announcement_notification.yaml) | Runs your action with the title whenever `librus_synergia_new_announcement` fires. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_announcement_notification.yaml) |
| [New Message Notification](blueprints/automation/librus_synergia/new_message_notification.yaml) | Runs your action with a one-line summary whenever `librus_synergia_new_message` fires. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_message_notification.yaml) |
| [New Homework Assignment Notification](blueprints/automation/librus_synergia/new_homework_assignment_notification.yaml) | Runs your action whenever a new real homework assignment ("zadanie domowe") appears - topic, due date, teacher, subject and, optionally, the instructions. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_homework_assignment_notification.yaml) |
| [Lesson Change Notification](blueprints/automation/librus_synergia/lesson_change_notification.yaml) | Runs your action with a one-line summary whenever `librus_synergia_timetable_changed` fires (a lesson newly cancelled, substituted, moved to another room or to another time) - the message says what changed, e.g. "Zmiana sali: Matematyka (2026-10-08, 09:50) - sala 12 → 21" or "Zastępstwo: Biologia - za Chemia, prowadzi Anna Nowak". | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Flesson_change_notification.yaml) |
| [New Agenda Entry Notification](blueprints/automation/librus_synergia/new_homework_notification.yaml) | Runs your action whenever `librus_synergia_new_homework` fires - optionally filtered to one category (e.g. only "Sprawdzian"). | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_homework_notification.yaml) |
| [Agenda Entry Changed or Removed](blueprints/automation/librus_synergia/agenda_change_notification.yaml) | Runs your action when `librus_synergia_agenda_changed` fires - an upcoming test moved to another day, a new time or description, or an entry removed from Librus - optionally only for some categories. Comes with a ready-made message ("Ola: [Sprawdzian] Matematyka - termin 12.10 → 14.10"). | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fagenda_change_notification.yaml) |
| [Unexcused Absence Notification](blueprints/automation/librus_synergia/unexcused_absence_notification.yaml) | Runs your action whenever `librus_synergia_new_absence` fires for an absence that isn't excused yet. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Funexcused_absence_notification.yaml) |
| [Absences To Justify Reminder](blueprints/automation/librus_synergia/absence_justification_reminder.yaml) | At a set time on the days you pick, runs your action with a `{{ reminder_text }}` (count + dates) for as long as there are unexcused absences left - silent once everything is excused, and days you've already sent a justification for are left out. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fabsence_justification_reminder.yaml) |
| [Absence Justification Decided](blueprints/automation/librus_synergia/justification_status_notification.yaml) | Runs your action when `librus_synergia_justification_status` fires - the school accepted or rejected a justification you sent (optionally only rejections). Ready-made message: "Ola: usprawiedliwienie za 14.09 - 16.09 przyjęte (5 nieobecn.)." | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fjustification_status_notification.yaml) |
| [School Trip Tomorrow](blueprints/automation/librus_synergia/school_trip_reminder.yaml) | At a set time, the day before a school trip, runs your action with the destination, transport and route ("Ola: jutro wycieczka - Muzeum Narodowe (autokar)."). | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fschool_trip_reminder.yaml) |
| [New School Document](blueprints/automation/librus_synergia/new_school_document_notification.yaml) | Runs your action when the school shares a new document with parents ("Ola: szkoła udostępniła dokument „Regulamin wycieczek”"). | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_school_document_notification.yaml) |
| [Low Subject Attendance](blueprints/automation/librus_synergia/low_subject_attendance_notification.yaml) | Runs your action when the Lowest subject attendance sensor crosses below a percentage you set (default 60%) - a heads-up before any subject gets near the 50% mark. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Flow_subject_attendance_notification.yaml) |
| [Test Tomorrow Reminder](blueprints/automation/librus_synergia/exam_tomorrow_notification.yaml) | At a set time each day, runs your action with an `{{ exam_summary }}` only when a test or quiz is on the Agenda for the next day - silent otherwise. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fexam_tomorrow_notification.yaml) |
| [Your Lucky Number](blueprints/automation/librus_synergia/lucky_number_yours_notification.yaml) | Runs your action when the published lucky number is your child's class-register number (read from Librus; override under **Configure**). | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Flucky_number_yours_notification.yaml) |
| [Morning Briefing](blueprints/automation/librus_synergia/morning_briefing_notification.yaml) | At a set time on school days, builds a `{{ briefing_text }}` (first lesson + room, today's lucky number, any test within 3 days) and runs your action - speak it, or notify. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fmorning_briefing_notification.yaml) |
| [School Wake-Up](blueprints/automation/librus_synergia/school_wake_up.yaml) | Runs your action a set number of minutes before the first lesson of each school day (alarm, music, lights, blinds). Follows the timetable: a day starting with lesson 2 wakes later, days off stay silent. `{{ wake_summary }}` is a ready-made message. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fschool_wake_up.yaml) |
| [School Pick-Up Reminder](blueprints/automation/librus_synergia/school_pickup_reminder.yaml) | Runs your action a set number of minutes before the last lesson ends - time to leave for pick-up. `{{ pickup_summary }}` is a ready-made message. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fschool_pickup_reminder.yaml) |
| [Copy School Events to a Calendar](blueprints/automation/librus_synergia/school_calendar_sync.yaml) | Copies tests, quizzes, trips, meetings and days off into a calendar of your choice (e.g. a family Google Calendar) - daily for the coming days and right after a new Agenda entry. Pick which Agenda categories to copy; entries already there are skipped. Home Assistant can only add events, so a moved test leaves its old date to delete by hand. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fschool_calendar_sync.yaml) |
| [Low Grade Alert](blueprints/automation/librus_synergia/low_grade_notification.yaml) | Runs your action only for a new grade at or below a threshold you set (default 2) - non-numeric marks are ignored. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Flow_grade_notification.yaml) |
| [Subject Average Dropped Below Threshold](blueprints/automation/librus_synergia/subject_average_drop_notification.yaml) | Runs your action when a subject-average sensor crosses below a value you set (default 3.5) - once on the way down, again only after it recovers. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fsubject_average_drop_notification.yaml) |
| [Grade Forecast Changed](blueprints/automation/librus_synergia/grade_forecast_notification.yaml) | Runs your action when a subject's forecast report-card grade moves (`librus_synergia_forecast_changed`) - by default only when it drops - with a `{{ forecast_summary }}` that says what it takes to win it back ("one 6 lifts it again"). | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fgrade_forecast_notification.yaml) |
| [Weekly AI Summary Report](blueprints/automation/librus_synergia/weekly_ai_summary_notification.yaml) | Sends the weekly AI summary when it is written (`librus_synergia_weekly_summary`) - short (headline, section statuses, to-dos) or full text, pick the sections, optionally only for weeks that need attention. Needs the weekly AI summary set up under **Configure**. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fweekly_ai_summary_notification.yaml) |
| [Weekly Digest](blueprints/automation/librus_synergia/weekly_digest_notification.yaml) | Every Sunday at a set time, builds a `{{ digest_text }}` (tests due in the next 7 days, optionally homework due and any unexcused absence count) and runs your action. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fweekly_digest_notification.yaml) |
| [Homework Due Tomorrow](blueprints/automation/librus_synergia/homework_due_tomorrow_notification.yaml) | At a set time each day, runs your action with a `{{ homework_summary }}` only when a real Homework assignment ("zadanie domowe") is due the next day - silent otherwise. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fhomework_due_tomorrow_notification.yaml) |
| [Time to Leave](blueprints/automation/librus_synergia/time_to_leave_notification.yaml) | Runs your action once, right when it's time to leave for the next lesson (its start time minus your travel time), checked every minute for accuracy regardless of your poll interval. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Ftime_to_leave_notification.yaml) |
| [Achievement Unlocked](blueprints/automation/librus_synergia/achievement_unlocked_notification.yaml) | Runs your action whenever `librus_synergia_achievement_unlocked` fires - a gamification milestone (first six, grade streaks, absence/behaviour-free streaks). Fires at most once per achievement, ever. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fachievement_unlocked_notification.yaml) |

</details>

Or import manually: Settings -> Automations & Scenes -> Blueprints -> Import
Blueprint, and paste a blueprint's GitHub URL.

<p align="center">
  <img src="docs/services-banner.svg" alt="Services">
</p>

## Services

### `librus_synergia.refresh`

Forces an immediate data refresh instead of waiting for the next poll - handy
right before a morning-briefing automation. Optional `device_id` targets one
student; omit it to refresh every configured student. Skips the smart-polling
throttle and the outage backoff, but not quiet hours.

### `librus_synergia.get_message`

Fetches ONE message's full, untruncated content (the Unread messages sensor's
`recent` attribute only ever carries a short preview - Librus itself truncates
that field). **This marks the message as read on Librus's servers, exactly
like opening it in the Librus app or website** - confirmed live: a message's
`readDate` flips from empty to a real timestamp the moment this is called.
Only call it for a message someone has actually chosen to open (e.g. the
[Librus Synergia Cards](https://github.com/MichalZaniewicz/ha-librus-synergia-cards)
Wiadomości card does this when you click a message) - never on a schedule or
from an automation that isn't a direct response to someone opening it.

| Field | Description |
|---|---|
| `device_id` | The Librus Synergia device (student) to fetch from. |
| `message_id` | The message's `id`, from the Unread messages sensor's `recent`/`substitutions_recent`/`alerts_recent`/`justifications_recent` attribute. |
| `mailbox` | Which mailbox the message is in - defaults to `inbox`; pass the `mailbox` value from the same attribute entry (e.g. `substitutions`, `alerts`, `justifications`). |

Returns `id`, `mailbox`, `sender`, `topic`, `content` (full text),
`send_date`, `read_date`, `has_attachment`, and `attachments` (a list of
`id`/`filename`). To download a file, open
`/api/librus_synergia/attachment/<device id>/<message id>/<attachment id>` while
logged in to Home Assistant - it passes the file straight from Librus to the
browser without saving it in Home Assistant or opening the message in Librus.
The companion Messages card does this when a file name is tapped.

### `librus_synergia.get_grades`

Returns every grade for a student in one response, optionally filtered to
one subject via `subject_id` - the single-call equivalent of reading each
subject average sensor's own `grades` attribute separately. Reads straight
from already-fetched data, no extra Librus request, so it's safe to call
from a routine automation (unlike `get_message`).

| Field | Description |
|---|---|
| `device_id` | The Librus Synergia device (student) to fetch grades for. |
| `subject_id` | Optional - limit to one subject, using a subject average sensor's `subject_id` attribute. |

Returns `grades` (a list of `subject`, `subject_id`, `value`, `category`,
`date`, `semester`, `comments`, `teacher`, `improves`, `improved`, newest
first) and `count`, plus `text_grades`, and `point_grades` with
`points_percentage` when the student has any.

## Known limitations / unverified details

Almost everything this integration reads has been checked against a real account. The open points below are data the test account simply hasn't had yet. For some of them, the field names come from other open-source Librus clients' documentation of the API (see [Acknowledgments](#acknowledgments)); no code was copied from those projects.

**Waiting for real data:**
- **Semester and year grades, proposed and final:** the fields are in Librus's grade data and these grades are left out of averages and streaks, but none have been issued yet. The first semester ends on 2027-01-31.
- **Descriptive grades:** the sensor is in place, but the endpoint has been empty on every check so far.
- **Point grades:** the sensor and attributes are in place, but the field names come from another client's reading of the API; no point grade has been seen on a real account (the test school has them off).
- **A rejected absence justification:** handled, but only accepted ones have been seen live.
- **Behaviour grade on a points scale:** the classic scale (wz, bdb, db, popr, ndp, ng) is confirmed on a real account; the points variant some schools use has not been seen yet.
- **Grade corrections ("poprawy"):** the link from a correction to the grade it replaces comes from another client's reading of the API; no correction has appeared on the test account yet.

**Deliberately not supported:**
- **Virtual classes.** The client can fetch them, but nothing references a virtual-class id, so no entity uses them.

**Never tested on purpose:**
- **A wrong password.** To avoid tripping Librus's abuse protection on a real family's account, it was never tried. "Invalid credentials" is inferred from the shape of the login response.

For the full, endpoint-by-endpoint picture, see the [unofficial Librus API notes](https://michalzaniewicz.github.io/librus-synergia/) in the librus-synergia library. If you see something that contradicts them, please open an issue with what you saw (redact personal data first).

## Related projects

- **[Librus Synergia Cards](https://github.com/MichalZaniewicz/ha-librus-synergia-cards)**: 61 Lovelace cards for this integration.
- **[librus-synergia](https://github.com/MichalZaniewicz/librus-synergia)**: the Python library this integration is built on (`pip install librus-synergia`). Use it in your own scripts, or from the command line.
- **[Unofficial Librus API notes](https://michalzaniewicz.github.io/librus-synergia/)**: the login flow and every endpoint's response shape.

## Acknowledgments

- [`emsi/librus_pyapi`](https://github.com/emsi/librus_pyapi) (MIT): the current login flow is based on this project's implementation.
- [`szkolny-eu/szkolny-android`](https://github.com/szkolny-eu/szkolny-android) (GPL-3.0): an independent, open-source e-register app. Its source helped identify Librus's endpoint and field names. No code was taken from it.
- [`RustySnek/librus-apix`](https://github.com/RustySnek/librus-apix): referenced for grade-value conventions.

## License

MIT - see [LICENSE](LICENSE).
