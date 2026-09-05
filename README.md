# Librus Synergia (unofficial) for Home Assistant

A HACS-installable Home Assistant integration for [Librus Synergia](https://synergia.librus.pl/) - the Polish school e-register - pulling grades, attendance, behaviour notices, timetable, agenda and announcements in as sensors and calendars.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=MichalZaniewicz&repository=ha-librus-synergia&category=integration)

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
| `sensor` *Subject* average (one per subject) | Discovered automatically from your account; attributes include latest grade, proposed/final semester grades |
| `sensor` Attendance | Count, with a per-type breakdown attribute |
| `sensor` Lucky number | Today's "szczęśliwy numerek" |
| `sensor` Unread announcements | Count, with recent titles |
| `sensor` Behaviour notices | Count, with a short recent-items attribute |
| `calendar` Timetable | Lesson plan, including known cancellations/substitutions |
| `calendar` Agenda | Tests, trips, parent meetings and other school events |

New grades, announcements and behaviour notices also fire Home Assistant bus events (`librus_synergia_new_grade`, `librus_synergia_new_announcement`, `librus_synergia_new_note`) for building notification automations - nothing fires on the very first sync after setup (that run only establishes the baseline).

The poll interval (default 20 minutes) is configurable via the integration's **Configure** option.

## Known limitations / unverified details

A few details couldn't be confirmed against a real account with data yet (an empty gradebook and no behaviour notices at the time of writing) and are flagged in code comments where they matter:

- **Grade value parsing** (`5+`, `4-`, `bz`, ...) uses the common Polish-gradebook `+0.5`/`-0.25` convention but hasn't been checked against real non-numeric grade marks yet.
- **`Notes[].Positive`**'s exact 0/1/2 enum (which value means positive/neutral/negative) is unconfirmed.
- Whether the `HomeWorks` (agenda) endpoint accepts a date-range query, or only ever returns a fixed window, is unconfirmed - the Agenda calendar works either way, just without server-side range filtering if not.
- A genuinely wrong password was deliberately never tested against a real account (to avoid tripping any credential-attempt-counting abuse heuristic), so the "invalid credentials" detection is a reasonable inference from the login response shape, not a confirmed observation.

If you hit one of these, please open an issue with what you saw (redact personal data).

## Acknowledgments

- [`emsi/librus_pyapi`](https://github.com/emsi/librus_pyapi) (MIT) - the current login flow this integration uses is closely modeled on this project's implementation.
- [`szkolny-eu/szkolny-android`](https://github.com/szkolny-eu/szkolny-android) (GPL-3.0) - Librus's open-sourced former Android client; informed the data-endpoint research even though its own login flow no longer works.
- [`RustySnek/librus-apix`](https://github.com/RustySnek/librus-apix) - referenced for grade-value parsing conventions.

## License

MIT - see [LICENSE](LICENSE).
