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

**Contents:** [Why this exists](#why-this-exists) · [Credential model](#credential-model-read-this-before-installing) · [Installation](#installation) · [Weekly AI summary](#weekly-ai-summary) · [Cards](#custom-lovelace-cards) · [Entities](#entities) · [Blueprints](#automation-blueprints) · [Services](#services) · [Known limitations](#known-limitations--unverified-details) · [Related projects](#related-projects)

## Why this exists

An integration for Librus already exists ([`LukMaverick/LibrusSynergiaHA`](https://github.com/LukMaverick/LibrusSynergiaHA), built on [`RustySnek/librus-apix`](https://github.com/RustySnek/librus-apix)), but it logs into the legacy HTML Synergia portal, which can demand a reCAPTCHA a human has to solve - unworkable for something meant to sync unattended in the background.

This integration instead uses the login flow Librus's own website/app uses for its private API gateway - reverse-engineered from [`emsi/librus_pyapi`](https://github.com/emsi/librus_pyapi) (MIT), which was itself updated to track a Librus authentication change on 2026-03-28. Confirmed live (2026-09-05) to complete without a captcha challenge for a normal login. An older password-grant login (the one documented by the open-source [`szkolny-eu/szkolny-android`](https://github.com/szkolny-eu/szkolny-android) app) no longer works: Librus now answers it with `unsupported_grant_type`.

The Librus client itself lives in a separate library, [**librus-synergia**](https://github.com/MichalZaniewicz/librus-synergia) (`pip install librus-synergia`). You can use it outside Home Assistant, and it comes with an [unofficial Librus API reference](https://michalzaniewicz.github.io/librus-synergia/).

**This is an unofficial integration using a private API and may violate Librus's Terms of Service. Use your own account at your own risk.**

<p align="center">
  <img src="docs/credentials-banner.svg" alt="Credential model">
</p>

## Credential model (read this before installing)

Unlike some cloud-polling integrations, **your Librus password is stored** in Home Assistant's config storage, alongside the session. This is a deliberate tradeoff, not an oversight: the session cookie this flow obtains is only valid for about a day and there is no separate refresh grant, so silent, unattended daily renewal isn't possible without it. Your password never leaves your Home Assistant instance.

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

Once a week, an AI model of your choice writes a summary of the school week,
split into sections:

| Section | What it covers |
|---|---|
| **Grades** | Every grade from the week (subject, category, weight, teacher's comment), what went well and what didn't, and how the averages moved compared with a week earlier |
| **Attendance** | Absences and lates, which subjects were missed, what still needs to be excused |
| **Behaviour** | Notes from the week, the behaviour grade, streaks |
| **Next week** | Tests and other agenda entries, homework due, cancelled lessons and substitutions, free days |
| **From the school** (optional) | The important points of the week's messages and announcements: meetings, trips, payments, deadlines |

On top: a one-line headline and an overall status (*good* / *OK* / *needs
attention*). At the bottom: 2-4 concrete to-dos for the coming week, and a
warning only when something really needs attention. Each section has its own
status too, so a dashboard card or a notification can show at a glance what
to look at.

![Weekly AI summary card](https://raw.githubusercontent.com/MichalZaniewicz/ha-librus-synergia-cards/main/docs/screenshots/librus-ai-summary-card-dark.png)

*Shown in the [Weekly AI summary card](https://github.com/MichalZaniewicz/ha-librus-synergia-cards) (example text).*

### How to set it up

You need Home Assistant **2025.8 or newer** (it uses the built-in
[AI Task](https://www.home-assistant.io/integrations/ai_task/) feature).
There is no API key in this integration: the summary goes through whatever AI
provider you already use in Home Assistant.

1. **Add an AI provider to Home Assistant**, if you don't have one yet:
   *Settings → Devices & services → Add integration*, then pick e.g.
   **Google Generative AI** (Gemini), **OpenAI**, **Anthropic** or
   **Ollama** (runs locally, nothing leaves your network). Follow its own
   setup (usually an API key from the provider). It creates an **AI Task**
   entity such as `ai_task.google_ai_task`.
2. **Turn the summary on:** *Settings → Devices & services → Librus Synergia →
   Configure → Weekly AI summary*, and fill in:

   | Field | What to choose |
   |---|---|
   | **AI model** | The AI Task entity from step 1. Leave it empty to turn the feature off |
   | **Written to** | **Parent**: third person, with what you can do. **Student**: written to your child directly, encouraging |
   | **Day** and **Time** | When the weekly summary is written (default Sunday 18:00). It covers the 7 days up to that day and previews the 7 days after |
   | **Include messages and announcements** | Off by default. On adds the *From the school* section, but then private messages and school announcements are sent to the AI provider |
   | **Your notes for the AI** | Optional context, e.g. *"Eighth-grade exam this year, chemistry is the weaker subject"* |

   Save. Three new entities appear on the student's device:
   - **Weekly summary** sensor: the headline is its state; sections, to-dos and warning are attributes.
   - **Generate weekly summary** button.
   - **Automatic weekly summary** switch.
3. **Write the first one now:** press **Generate weekly summary**. It takes
   a few seconds; then the **Weekly summary** sensor shows the headline.
4. **Show it on a dashboard** with the **Weekly AI summary** card from
   [Librus Synergia Cards](https://github.com/MichalZaniewicz/ha-librus-synergia-cards)
   (`type: custom:librus-ai-summary-card`, no other configuration needed). It
   has tabs for the sections and its own **Generate now** button.
5. **Get it on your phone** with the
   [Weekly AI Summary Report](#automation-blueprints) blueprint. It sends a
   short report (headline, section statuses, to-dos) or the full text. You
   choose the sections, and can limit it to weeks that need attention.

With more than one child, set it up on each child's entry. Each one gets its
own summary, and the blueprint's title says whose it is.

### What gets sent, and when

- Only data the integration already has: no extra Librus requests. The AI
  provider receives the student's name and class, the week's grades, the
  averages, the week's attendance and behaviour notes, how many absences
  are still unexcused, and the coming week's agenda, homework due and
  timetable changes. Messages and announcements are included only if you
  turned that on. Use a local model (Ollama) if nothing should leave your
  network.
- **One request a week**, plus any time you press the button. A run missed
  because Home Assistant was off is made up within 2 days. A week with no
  lessons and nothing coming up (holidays) is skipped, so it costs nothing.
- The **Automatic weekly summary** switch pauses the schedule; the button
  still works.
- The result is stored, so a restart does not pay for the same summary twice.
  A new summary fires the `librus_synergia_weekly_summary` event.

### If something doesn't work

- **"The AI Task integration is not available"** when opening *Weekly AI
  summary*: update Home Assistant to 2025.8+ and add an AI provider first
  (step 1).
- **The sensor stays empty after pressing the button:** look at its `error`
  attribute. It holds the provider's message, e.g. an exhausted quota or a
  wrong API key.
- The text is written by an AI model. It is told to stick to the data it
  gets, but it can still make mistakes, so check anything important in
  Librus itself.

<p align="center">
  <img src="docs/cards-banner.svg" alt="Custom Lovelace cards">
</p>

## Custom Lovelace cards

Want a dashboard without wiring these sensors into generic entity cards by hand?
**[Librus Synergia Cards](https://github.com/MichalZaniewicz/ha-librus-synergia-cards)**
is a companion HACS repo with 56 purpose-built cards: grades and trends, attendance,
timetable, agenda, messages, announcements, a "Today" overview, and a few playful ones.
Each card finds your child's device on its own (no YAML for one student), follows your
Home Assistant theme and language (English, Polish).

![Librus Synergia Cards preview](https://raw.githubusercontent.com/MichalZaniewicz/ha-librus-synergia-cards/main/docs/screenshots/cards-overview-dark.png)

<p align="center">
  <img src="docs/entities-banner.svg" alt="Entities">
</p>

## Entities

| Type | Entity | Notes |
|---|---|---|
| `sensor` | Overall grade average | Average across every subject - weighted by default, or the plain arithmetic mean if you pick that under **Configure**; attributes carry both (`average_weighted`, `average_arithmetic`) plus `average_semester_1`/`average_semester_2` |
| `sensor` | *Subject* average (one per subject) | Discovered automatically from your account; attributes include the subject name, full grade log, latest grade (with any teacher comments), proposed/final semester grades and per-semester / arithmetic averages |
| `sensor` | Attendance | Count of real absences (excludes "present"/"late"/"excused" marks); full per-type breakdown, total record count, an independently-computed `percentage` (works even if your school hides this), and `by_semester`/`by_weekday`/`by_subject` breakdowns in attributes |
| `sensor` | Unexcused absences | Just the count of absences that still need a justification (the Attendance sensor's state blends excused + unexcused); `recent_dates` in attributes |
| `sensor` | Lowest subject attendance | Attendance percentage of the subject where it is lowest - the one to watch for the 50% rule, below which a student can be left unclassified. `subject` names it, `subjects` lists every subject (`total`/`present`/`absent`/`percentage`), `at_risk` the ones already under 50% |
| `sensor` | Next lesson | Subject name of the next lesson that will actually take place (cancelled slots skipped); attributes carry `start`/`end`, `minutes_until`, teacher, classroom, period number and substitution flag |
| `sensor` | Current lesson | Subject name of the lesson happening right now (`unknown` during breaks / outside school hours); attributes include `minutes_left` and the same period detail |
| `sensor` | Next exam | Date of the next graded assessment on the Agenda (`device_class: date`); attributes carry `days_until`, subject, category and an `upcoming` list |
| `sensor` | Lucky number | The most recently published "szczęśliwy numerek" - Librus can publish the *next* school day's number a day ahead, so check the `is_today`/`day` attributes rather than assuming the state is always for today |
| `sensor` | Unread announcements | Count, with a `recent` attribute (subject/content preview/dates) |
| `sensor` | Behaviour notices | Count, with a short recent-items attribute including the resolved category name and sentiment (positive/negative/neutral) |
| `sensor` | Unread messages | Count of unread Wiadomości in your main inbox, with sender/topic/preview for the most recent ones (including each message's `id`/`mailbox`, for the [`get_message` service](#services)) and a `mailbox_breakdown` attribute (inbox/notes/alerts/substitutions/absences/justifications/trash unread counts). Also carries full preview content (not just a count) for the two secondary mailboxes worth actually reading - `substitutions_recent` and `alerts_recent`. The routine poll never marks anything read - only the message list/count endpoints are used for that. Shows `unavailable` (not `0`) if your school hasn't enabled the messages module |
| `sensor` | School | Name, town/street, head teacher, contact details, and a `bell_schedule` attribute (period number -> start/end time, derived from the timetable) |
| `sensor` | Class | Class name (e.g. "7d"), homeroom teacher, semester/school-year boundary dates |
| `sensor` | Homework assignments | Count of real "zadania domowe" (distinct from the Agenda calendar's general feed below), with a `recent` attribute (id/topic/text/due date/teacher, and the subject - worked out from the teacher, since Librus doesn't say; empty when that teacher teaches more than one subject) |
| `sensor` | Behaviour grade | Formal "ocena zachowania" (distinct from Behaviour notices above) - state is the most recent grade's short code (e.g. "wz"), with a `recent` attribute (value/category/date/comments) |
| `sensor` | Descriptive grades | Count of non-numeric descriptive grades (this school has these enabled instead of point-scale grades), with a `recent` attribute (subject/value/date) |
| `sensor` | Attendance streak | Days since the last real absence - falls back to days since the school year started for a perfect-attendance student, rather than `unknown` |
| `sensor` | Good behaviour streak | Days since the last negative behaviour note, same fallback as Attendance streak |
| `sensor` | Good grades streak | Consecutive most-recent numeric grades of 4 or better - a non-numeric mark (`bz`/`np`/...) doesn't break it, only an actual low grade does |
| `sensor` | Rank | A cosmetic Bronze/Silver/Gold/Diamond tier derived from your overall average (`device_class: enum`) - `points_to_next_tier` in attributes shows how much more average you need for the next one up |
| `sensor` | Weekly summary | Only with the [weekly AI summary](#weekly-ai-summary) set up. The AI's headline for the week; `status`, `sections` (grades/attendance/behaviour/next_week, plus school_news if enabled - each `{status, text}`), `advice`, `warning`, `week_from`/`week_to`, `next_run` and `error` in attributes. Comes with a **Generate weekly summary** button and an **Automatic weekly summary** switch |
| `calendar` | Timetable | Lesson plan, including known cancellations/substitutions |
| `calendar` | Agenda | Tests, trips, parent meetings and other school events, prefixed with their category (e.g. "[Sprawdzian] ...") when known |
| `calendar` | Free days | The whole school year's holidays/breaks |

New grades, announcements, behaviour notices, messages, Agenda entries, homework assignments and absences fire Home Assistant bus events (`librus_synergia_new_grade`, `librus_synergia_new_announcement`, `librus_synergia_new_note`, `librus_synergia_new_message`, `librus_synergia_new_homework` (Agenda), `librus_synergia_new_homework_assignment` (real homework: topic, text, due date, teacher, subject), `librus_synergia_new_absence`), and a cancelled/substitution lesson fires `librus_synergia_timetable_changed` - all for building notification automations. `librus_synergia_achievement_unlocked` fires for a handful of objective, data-derived gamification milestones (first six, good-grade streaks of 5/10/20, and 7/30/90-day streaks without an absence or a negative note) - deliberately not an invented points system, every one of these is a plain, honest fact anyone could verify by hand. Nothing fires on the very first sync after setup (that run only establishes the baseline). Grade/note/homework/timetable events include the resolved subject/teacher/category name alongside the raw id, so an automation doesn't need its own lookup. `librus_synergia_new_grade` also carries the grade's details: `teacher`, `category`, `weight`, `counts_to_average`, `comments` (list), `date`, `semester` and `kind` (`normal` / `semester_proposition` / `semester` / `final_proposition` / `final`). Ready-made [blueprints](#automation-blueprints) wrap these for you.

The integration's **Configure** menu has two parts. *Settings* sets the poll interval (default 20 minutes), whether the average sensors show the weighted or the arithmetic average, and whether to fetch private messages at all - turn *Fetch private messages* off if your school doesn't use Wiadomości or you don't want those extra requests (the Unread messages sensor then reports `unavailable`). *Weekly AI summary* sets up the [weekly AI summary](#weekly-ai-summary).

<p align="center">
  <img src="docs/blueprints-banner.svg" alt="Automation blueprints">
</p>

### Automation blueprints

Ready-to-import blueprints under
[`blueprints/automation/librus_synergia/`](blueprints/automation/librus_synergia/) wrap
the events above so you don't have to write the YAML yourself - each just asks for an
*action* (e.g. "Send a notification"):

<details>
<summary><b>Show all 20 blueprints</b></summary>

| Blueprint | What it does | |
| --- | --- | --- |
| [New Grade Notification](blueprints/automation/librus_synergia/new_grade_notification.yaml) | Runs your action with a summary whenever `librus_synergia_new_grade` fires - optionally with a second line showing the category, weight, date and teacher's comment. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_grade_notification.yaml) |
| [New Behaviour Notice Notification](blueprints/automation/librus_synergia/new_note_notification.yaml) | Runs your action with a one-line summary whenever `librus_synergia_new_note` fires. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_note_notification.yaml) |
| [New Announcement Notification](blueprints/automation/librus_synergia/new_announcement_notification.yaml) | Runs your action with the title whenever `librus_synergia_new_announcement` fires. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_announcement_notification.yaml) |
| [New Message Notification](blueprints/automation/librus_synergia/new_message_notification.yaml) | Runs your action with a one-line summary whenever `librus_synergia_new_message` fires. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_message_notification.yaml) |
| [New Homework Assignment Notification](blueprints/automation/librus_synergia/new_homework_assignment_notification.yaml) | Runs your action whenever a new real homework assignment ("zadanie domowe") appears - topic, due date, teacher, subject and, optionally, the instructions. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_homework_assignment_notification.yaml) |
| [Lesson Change Notification](blueprints/automation/librus_synergia/lesson_change_notification.yaml) | Runs your action with a one-line summary whenever `librus_synergia_timetable_changed` fires (a lesson newly cancelled or moved to a substitution). | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Flesson_change_notification.yaml) |
| [New Agenda Entry Notification](blueprints/automation/librus_synergia/new_homework_notification.yaml) | Runs your action whenever `librus_synergia_new_homework` fires - optionally filtered to one category (e.g. only "Sprawdzian"). | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_homework_notification.yaml) |
| [Unexcused Absence Notification](blueprints/automation/librus_synergia/unexcused_absence_notification.yaml) | Runs your action whenever `librus_synergia_new_absence` fires for an absence that isn't excused yet. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Funexcused_absence_notification.yaml) |
| [Absences To Justify Reminder](blueprints/automation/librus_synergia/absence_justification_reminder.yaml) | At a set time on the days you pick, runs your action with a `{{ reminder_text }}` (count + dates) for as long as there are unexcused absences left - silent once everything is excused. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fabsence_justification_reminder.yaml) |
| [Low Subject Attendance](blueprints/automation/librus_synergia/low_subject_attendance_notification.yaml) | Runs your action when the Lowest subject attendance sensor crosses below a percentage you set (default 60%) - a heads-up before any subject gets near the 50% mark. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Flow_subject_attendance_notification.yaml) |
| [Test Tomorrow Reminder](blueprints/automation/librus_synergia/exam_tomorrow_notification.yaml) | At a set time each day, runs your action with an `{{ exam_summary }}` only when a test or quiz is on the Agenda for the next day - silent otherwise. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fexam_tomorrow_notification.yaml) |
| [Your Lucky Number](blueprints/automation/librus_synergia/lucky_number_yours_notification.yaml) | Runs your action when the published lucky number is your child's class-register number (set it under **Configure**). | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Flucky_number_yours_notification.yaml) |
| [Morning Briefing](blueprints/automation/librus_synergia/morning_briefing_notification.yaml) | At a set time on school days, builds a `{{ briefing_text }}` (first lesson + room, today's lucky number, any test within 3 days) and runs your action - speak it, or notify. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fmorning_briefing_notification.yaml) |
| [Low Grade Alert](blueprints/automation/librus_synergia/low_grade_notification.yaml) | Runs your action only for a new grade at or below a threshold you set (default 2) - non-numeric marks are ignored. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Flow_grade_notification.yaml) |
| [Subject Average Dropped](blueprints/automation/librus_synergia/subject_average_drop_notification.yaml) | Runs your action when a subject-average sensor crosses below a value you set (default 3.5) - once on the way down, again only after it recovers. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fsubject_average_drop_notification.yaml) |
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
student; omit it to refresh every configured student. Uses the coordinator's
debounced refresh, so it's safe to call often.

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
| `message_id` | The message's `id`, from the Unread messages sensor's `recent`/`substitutions_recent`/`alerts_recent` attribute. |
| `mailbox` | Which mailbox the message is in - defaults to `inbox`; use `substitutions` or `alerts` for a message from those attributes. |

Returns `id`, `mailbox`, `sender`, `topic`, `content` (full text),
`send_date`, `read_date`, `has_attachment`, and `attachments` (a list of
`id`/`filename` - the file itself still can't be downloaded through this
integration, see [Known limitations](#known-limitations--unverified-details)
below, but at least you'll know what to look for in the real Librus app).

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
`date`, `semester`, `comments`, newest first) and `count`.

## Known limitations / unverified details

Almost everything this integration reads has been checked against a real account. The open points below are data the test account simply hasn't had yet. For some of them, the field names come from other open-source Librus clients' documentation of the API (see [Acknowledgments](#acknowledgments)); no code was copied from those projects.

**Waiting for real data:**
- **Final semester and year grades** (as opposed to the *proposed* ones, which are confirmed) are left out of averages and streaks. None exist yet, because the first semester ends on 2027-01-31.
- **Behaviour grade** and **descriptive grades** sensors are in place, but their endpoints have been empty on every check so far.

**Deliberately not supported:**
- **Downloading message attachments.** `get_message` returns each attachment's file name, but the download itself only works through Librus's older XML protocol, which uses a separate session.
- **Point grades, text grades and virtual classes.** The client can fetch them, but no entity uses them: point grades are disabled at the test school, and the others had nothing to show.

**Never tested on purpose:**
- **A wrong password.** To avoid tripping Librus's abuse protection on a real family's account, it was never tried. "Invalid credentials" is inferred from the shape of the login response.

For the full, endpoint-by-endpoint picture, see the [unofficial Librus API notes](https://michalzaniewicz.github.io/librus-synergia/) in the librus-synergia library. If you see something that contradicts them, please open an issue with what you saw (redact personal data first).

## Related projects

- **[Librus Synergia Cards](https://github.com/MichalZaniewicz/ha-librus-synergia-cards)**: 56 Lovelace cards for this integration.
- **[librus-synergia](https://github.com/MichalZaniewicz/librus-synergia)**: the Python library this integration is built on (`pip install librus-synergia`). Use it in your own scripts, or from the command line.
- **[Unofficial Librus API notes](https://michalzaniewicz.github.io/librus-synergia/)**: the login flow and every endpoint's response shape.

## Acknowledgments

- [`emsi/librus_pyapi`](https://github.com/emsi/librus_pyapi) (MIT): the current login flow is based on this project's implementation.
- [`szkolny-eu/szkolny-android`](https://github.com/szkolny-eu/szkolny-android) (GPL-3.0): an independent, open-source e-register app. Its source helped identify Librus's endpoint and field names. No code was taken from it.
- [`RustySnek/librus-apix`](https://github.com/RustySnek/librus-apix): referenced for grade-value conventions.

## License

MIT - see [LICENSE](LICENSE).
