# Librus Synergia (unofficial) for Home Assistant

A HACS-installable Home Assistant integration for [Librus Synergia](https://synergia.librus.pl/) - the Polish school e-register - pulling grades, attendance, behaviour notices, timetable, agenda, announcements, messages, and school/class info in as sensors and calendars.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=MichalZaniewicz&repository=ha-librus-synergia&category=integration)

> [!TIP]
> ⭐ **Enjoying this integration?** Every star is real motivation to keep building new features :)

<!-- The badge lives OUTSIDE the alert on purpose: Home Assistant/HACS rewrites a GitHub alert
into <ha-alert> and drops every child whose textContent is empty, which silently removes any
<img> placed inside it. -->

[![Star this repo](https://img.shields.io/github/stars/MichalZaniewicz/ha-librus-synergia?style=for-the-badge&logo=github&label=STAR%20THIS%20REPO&labelColor=555555&color=ffc107)](https://github.com/MichalZaniewicz/ha-librus-synergia)

## Why this exists

An integration for Librus already exists ([`LukMaverick/LibrusSynergiaHA`](https://github.com/LukMaverick/LibrusSynergiaHA), built on [`RustySnek/librus-apix`](https://github.com/RustySnek/librus-apix)), but it logs into the legacy HTML Synergia portal, which can demand a reCAPTCHA a human has to solve - unworkable for something meant to sync unattended in the background.

This integration instead uses the login flow Librus's own website/app uses for its private API gateway - reverse-engineered from [`emsi/librus_pyapi`](https://github.com/emsi/librus_pyapi) (MIT), which was itself updated to track a Librus authentication change on 2026-03-28. Confirmed live (2026-09-05) to complete without a captcha challenge for a normal login. An earlier, now-dead password-grant flow (documented by the open-sourced [`szkolny-eu/szkolny-android`](https://github.com/szkolny-eu/szkolny-android), GPL-3.0) informed the initial data-endpoint research but no longer works (`unsupported_grant_type`).

**This is an unofficial integration using a private API and may violate Librus's Terms of Service. Use your own account at your own risk.**

## Credential model (read this before installing)

Unlike some cloud-polling integrations, **your Librus password is stored** in Home Assistant's config storage, alongside the session. This is a deliberate tradeoff, not an oversight: the session cookie this flow obtains is only valid for about a day and there is no separate refresh grant, so silent, unattended daily renewal isn't possible without it. Your password never leaves your Home Assistant instance.

A long-lived device-recognition cookie is also persisted and re-sent on every login - this is believed to be why a normal login skips any captcha/2FA challenge, so treat it as load-bearing, not just a convenience.

## Installation

1. HACS → ⋮ → **Custom repositories** → add this repo as category **Integration** (or use the badge above).
2. Install **Librus Synergia (unofficial)**, restart Home Assistant.
3. Settings → Devices & services → **Add integration** → search "Librus Synergia".
4. Enter your Librus **login** (e.g. `1234567u` - a direct student/account login, not a Librus Portal e-mail) and password.

Each child/student is a separate login and a separate integration entry.

## Entities

| Type | Entity | Notes |
|---|---|---|
| `sensor` | Overall grade average | Weighted average across every subject; attributes add `average_arithmetic` and `average_semester_1`/`average_semester_2` |
| `sensor` | *Subject* average (one per subject) | Discovered automatically from your account; attributes include the subject name, full grade log, latest grade (with any teacher comments), proposed/final semester grades and per-semester / arithmetic averages |
| `sensor` | Attendance | Count of real absences (excludes "present"/"late"/"excused" marks); full per-type breakdown, total record count, an independently-computed `percentage` (works even if your school hides this), and a `by_semester` breakdown (count/percentage per semester) in attributes |
| `sensor` | Unexcused absences | Just the count of absences that still need a justification (the Attendance sensor's state blends excused + unexcused); `recent_dates` in attributes |
| `sensor` | Next lesson | Subject name of the next lesson that will actually take place (cancelled slots skipped); attributes carry `start`/`end`, `minutes_until`, teacher, classroom, period number and substitution flag |
| `sensor` | Current lesson | Subject name of the lesson happening right now (`unknown` during breaks / outside school hours); attributes include `minutes_left` and the same period detail |
| `sensor` | Next exam | Date of the next graded assessment on the Agenda (`device_class: date`); attributes carry `days_until`, subject, category and an `upcoming` list |
| `sensor` | Lucky number | The most recently published "szczęśliwy numerek" - Librus can publish the *next* school day's number a day ahead, so check the `is_today`/`day` attributes rather than assuming the state is always for today |
| `sensor` | Unread announcements | Count, with a `recent` attribute (subject/content preview/dates) |
| `sensor` | Behaviour notices | Count, with a short recent-items attribute including the resolved category name and sentiment (positive/negative/neutral) |
| `sensor` | Unread messages | Count of unread Wiadomości in your main inbox, with sender/topic/preview for the most recent ones (including each message's `id`/`mailbox`, for the [`get_message` service](#services)) and a `mailbox_breakdown` attribute (inbox/notes/alerts/substitutions/absences/justifications/trash unread counts). Also carries full preview content (not just a count) for the two secondary mailboxes worth actually reading - `substitutions_recent` and `alerts_recent`. The routine poll never marks anything read - only the message list/count endpoints are used for that. Shows `unavailable` (not `0`) if your school hasn't enabled the messages module |
| `sensor` | School | Name, town/street, head teacher, contact details, and a `bell_schedule` attribute (period number -> start/end time, derived from the timetable) |
| `sensor` | Class | Class name (e.g. "7d"), homeroom teacher, semester/school-year boundary dates |
| `sensor` | Homework assignments | Count of real "zadania domowe" (distinct from the Agenda calendar's general feed below), with a `recent` attribute (topic/text/due date/teacher) |
| `sensor` | Behaviour grade | Formal "ocena zachowania" (distinct from Behaviour notices above) - state is the most recent grade's short code (e.g. "wz"), with a `recent` attribute (value/category/date/comments) |
| `sensor` | Descriptive grades | Count of non-numeric descriptive grades (this school has these enabled instead of point-scale grades), with a `recent` attribute (subject/value/date) |
| `calendar` | Timetable | Lesson plan, including known cancellations/substitutions |
| `calendar` | Agenda | Tests, trips, parent meetings and other school events, prefixed with their category (e.g. "[Sprawdzian] ...") when known |
| `calendar` | Free days | The whole school year's holidays/breaks |

New grades, announcements, behaviour notices, messages, Agenda entries and absences fire Home Assistant bus events (`librus_synergia_new_grade`, `librus_synergia_new_announcement`, `librus_synergia_new_note`, `librus_synergia_new_message`, `librus_synergia_new_homework`, `librus_synergia_new_absence`), and a cancelled/substitution lesson fires `librus_synergia_timetable_changed` - all for building notification automations. Nothing fires on the very first sync after setup (that run only establishes the baseline). Grade/note/homework/timetable events include the resolved subject/teacher/category name alongside the raw id, so an automation doesn't need its own lookup. Ready-made [blueprints](#automation-blueprints) wrap these for you.

The poll interval (default 20 minutes) is configurable via the integration's **Configure** option.

### Automation blueprints

Ready-to-import blueprints under
[`blueprints/automation/librus_synergia/`](blueprints/automation/librus_synergia/) wrap
the events above so you don't have to write the YAML yourself - each just asks for an
*action* (e.g. "Send a notification"):

| Blueprint | What it does | |
| --- | --- | --- |
| [New Grade Notification](blueprints/automation/librus_synergia/new_grade_notification.yaml) | Runs your action with a one-line summary whenever `librus_synergia_new_grade` fires. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_grade_notification.yaml) |
| [New Behaviour Notice Notification](blueprints/automation/librus_synergia/new_note_notification.yaml) | Runs your action with a one-line summary whenever `librus_synergia_new_note` fires. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_note_notification.yaml) |
| [New Announcement Notification](blueprints/automation/librus_synergia/new_announcement_notification.yaml) | Runs your action with the title whenever `librus_synergia_new_announcement` fires. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_announcement_notification.yaml) |
| [New Message Notification](blueprints/automation/librus_synergia/new_message_notification.yaml) | Runs your action with a one-line summary whenever `librus_synergia_new_message` fires. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_message_notification.yaml) |
| [Lesson Change Notification](blueprints/automation/librus_synergia/lesson_change_notification.yaml) | Runs your action with a one-line summary whenever `librus_synergia_timetable_changed` fires (a lesson newly cancelled or moved to a substitution). | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Flesson_change_notification.yaml) |
| [New Agenda Entry Notification](blueprints/automation/librus_synergia/new_homework_notification.yaml) | Runs your action whenever `librus_synergia_new_homework` fires - optionally filtered to one category (e.g. only "Sprawdzian"). | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_homework_notification.yaml) |
| [Unexcused Absence Notification](blueprints/automation/librus_synergia/unexcused_absence_notification.yaml) | Runs your action whenever `librus_synergia_new_absence` fires for an absence that isn't excused yet. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Funexcused_absence_notification.yaml) |

Or import manually: Settings -> Automations & Scenes -> Blueprints -> Import
Blueprint, and paste a blueprint's GitHub URL.

## Services

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
`send_date`, `read_date`, `has_attachment`.

## Custom Lovelace cards

Want a dashboard without wiring these sensors into generic entity cards by hand?
**[Librus Synergia Cards](https://github.com/MichalZaniewicz/ha-librus-synergia-cards)**
is a companion HACS repo with 41 purpose-built cards - grade averages, a full
grade log (per-subject or across every subject), a grade trend chart, a grade
distribution histogram, a grade profile radar and a grades-by-category donut,
attendance (plus a percentage/semester breakdown, a year-at-a-glance heatmap,
and an absences-by-weekday chart), behaviour notices and the formal behaviour
grade, messages, substitutions & alerts, announcements, homework assignments,
a combined "what's new" activity feed, today's timetable, a week-at-a-glance
grid, a lesson-time-by-subject donut, the agenda, free days, a "Today"
overview, a weekly summary, the lucky number, a playful trading-card style
student summary, an absence-free streak counter, and compact single-row
tiles for several of the above. Each card auto-detects
your child's device (zero YAML for the common case of one student), themes with
your Home Assistant theme automatically, and follows your HA language (English,
Polish).

![Librus Synergia Cards preview](https://raw.githubusercontent.com/MichalZaniewicz/ha-librus-synergia-cards/main/docs/screenshots/cards-overview-dark.png)

## Known limitations / unverified details

A few details couldn't be confirmed against a real account with data yet (an empty gradebook and no behaviour notices/grades at the time of writing). Most field names below come straight from reading szkolny-eu/szkolny-android's own reference parser (the same GPL-3.0 source this whole integration is modeled on), not guesswork - but none of it has been checked against a real *populated* response yet:

- **Grade value parsing**: the `+0.5`/`-0.25` numeric modifier convention (`5+`, `4-`, ...) is a common third-party inference, not something Librus documents - the live `Grades/Types` reference endpoint confirms every *non-numeric* mark Librus actually uses (`bz`, `np`, `nk`, `uł`, `nł`, `zl`, `nz`, `zw`, `uc`, `nu`, bare `+`/`-`) is correctly excluded from the average, but the exact numeric value a `+`/`-` modifier should produce is still unverified (no grades existed on the test account, first week of the school year).
- **Homework assignments, Behaviour grade and Descriptive grades sensors** are all wired up and shipping, but none have ever shown real data - the account's `HomeWorkAssignments`/`BehaviourGrades/Points`/`DescriptiveGrades` endpoints have been empty every time they've been checked.
- **`Grades/Comments`**: fixed to correctly treat this as a separate endpoint from `/Grades` (a grade's own `Comments` field is a list of ids to resolve against it, not embedded text) - still unconfirmed against a real commented grade either way.
- `VirtualClasses` (split/group classes, e.g. language subgroups), `PointGrades` (confirmed *disabled* for this account's school via the `Units` endpoint) and `TextGrades` (enablement unknown) all have client methods available but aren't wired into any entity - nothing to build a parser against, or nothing that would ever populate for this account.
- Whether the `HomeWorks` (agenda) endpoint accepts a date-range query, or only ever returns a fixed window, is unconfirmed - the Agenda calendar works either way, just without server-side range filtering if not.
- A genuinely wrong password was deliberately never tested against a real account (to avoid tripping any credential-attempt-counting abuse heuristic), so the "invalid credentials" detection is a reasonable inference from the login response shape, not a confirmed observation.

If you hit one of these, please open an issue with what you saw (redact personal data).

## Acknowledgments

- [`emsi/librus_pyapi`](https://github.com/emsi/librus_pyapi) (MIT) - the current login flow this integration uses is closely modeled on this project's implementation.
- [`szkolny-eu/szkolny-android`](https://github.com/szkolny-eu/szkolny-android) (GPL-3.0) - Librus's open-sourced former Android client; informed the data-endpoint research even though its own login flow no longer works.
- [`RustySnek/librus-apix`](https://github.com/RustySnek/librus-apix) - referenced for grade-value parsing conventions.

## License

MIT - see [LICENSE](LICENSE).
