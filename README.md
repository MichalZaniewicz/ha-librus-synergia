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

| Entity | Notes |
|---|---|
| `sensor` Overall grade average | Weighted average across every subject |
| `sensor` *Subject* average (one per subject) | Discovered automatically from your account; attributes include latest grade (with any teacher comments), proposed/final semester grades |
| `sensor` Attendance | Count of real absences (excludes "present"/"late"/"excused" marks); full per-type breakdown and total record count in attributes |
| `sensor` Lucky number | Today's "szczęśliwy numerek" |
| `sensor` Unread announcements | Count, with a `recent` attribute (subject/content preview/dates) |
| `sensor` Behaviour notices | Count, with a short recent-items attribute including the resolved category name and sentiment (positive/negative/neutral) |
| `sensor` Unread messages | Count of unread Wiadomości in your main inbox, with sender/topic/preview for the most recent ones and a `mailbox_breakdown` attribute (inbox/notes/alerts/substitutions/absences/justifications/trash unread counts). Never marks anything read - only the message list/count endpoints are used, never the per-message detail one. Shows `unavailable` (not `0`) if your school hasn't enabled the messages module |
| `sensor` School | Name, town/street, head teacher, contact details |
| `sensor` Class | Class name (e.g. "7d"), homeroom teacher, semester/school-year boundary dates |
| `sensor` Homework assignments | Count of real "zadania domowe" (distinct from the Agenda calendar's general feed below), with a `recent` attribute (topic/text/due date/teacher) |
| `sensor` Behaviour grade | Formal "ocena zachowania" (distinct from Behaviour notices above) - state is the most recent grade's short code (e.g. "wz"), with a `recent` attribute (value/category/date/comments) |
| `sensor` Descriptive grades | Count of non-numeric descriptive grades (this school has these enabled instead of point-scale grades), with a `recent` attribute (subject/value/date) |
| `calendar` Timetable | Lesson plan, including known cancellations/substitutions |
| `calendar` Agenda | Tests, trips, parent meetings and other school events, prefixed with their category (e.g. "[Sprawdzian] ...") when known |
| `calendar` Free days | The whole school year's holidays/breaks |

New grades, announcements, behaviour notices and messages also fire Home Assistant bus events (`librus_synergia_new_grade`, `librus_synergia_new_announcement`, `librus_synergia_new_note`, `librus_synergia_new_message`) for building notification automations - nothing fires on the very first sync after setup (that run only establishes the baseline). Grade/note events include the resolved subject/teacher name alongside the raw id, so an automation doesn't need its own lookup. Four ready-made [blueprints](#automation-blueprints) wrap these for you.

The poll interval (default 20 minutes) is configurable via the integration's **Configure** option.

### Automation blueprints

Four ready-to-import blueprints under
[`blueprints/automation/librus_synergia/`](blueprints/automation/librus_synergia/) wrap
the events above so you don't have to write the YAML yourself - each just asks for an
*action* (e.g. "Send a notification"):

| Blueprint | What it does |
| --- | --- |
| [New Grade Notification](blueprints/automation/librus_synergia/new_grade_notification.yaml) | Runs your action with a one-line summary whenever `librus_synergia_new_grade` fires. |
| [New Behaviour Notice Notification](blueprints/automation/librus_synergia/new_note_notification.yaml) | Runs your action with a one-line summary whenever `librus_synergia_new_note` fires. |
| [New Announcement Notification](blueprints/automation/librus_synergia/new_announcement_notification.yaml) | Runs your action with the title whenever `librus_synergia_new_announcement` fires. |
| [New Message Notification](blueprints/automation/librus_synergia/new_message_notification.yaml) | Runs your action with a one-line summary whenever `librus_synergia_new_message` fires. |

[![Open your Home Assistant instance and show the blueprint import dialog with the new-grade-notification blueprint pre-filled.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_grade_notification.yaml)
[![Open your Home Assistant instance and show the blueprint import dialog with the new-note-notification blueprint pre-filled.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_note_notification.yaml)
[![Open your Home Assistant instance and show the blueprint import dialog with the new-announcement-notification blueprint pre-filled.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_announcement_notification.yaml)
[![Open your Home Assistant instance and show the blueprint import dialog with the new-message-notification blueprint pre-filled.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMichalZaniewicz%2Fha-librus-synergia%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flibrus_synergia%2Fnew_message_notification.yaml)

Or import manually: Settings -> Automations & Scenes -> Blueprints -> Import
Blueprint, and paste a blueprint's GitHub URL.

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
