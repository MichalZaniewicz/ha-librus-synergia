# Changelog

## 0.4.3

- **Unread announcements** sensor's `recent` attribute now carries a content
  preview and start/end/creation dates for each announcement, not just a
  bare subject list (`titles`) - matching the Behaviour notices and Unread
  messages sensors' existing pattern. No extra API calls - this data was
  already fetched and parsed, just not surfaced.

## 0.4.2

- **Fixed:** a session that expired mid-cycle (Librus rejecting a data
  request with HTTP 401/403) no longer immediately asks you to reauthenticate.
  Librus's real session lifetime can apparently run shorter than this
  integration's own conservative ~20h estimate - the coordinator now forces
  one silent re-login with the already-stored password and retries before
  ever surfacing Home Assistant's "reauthenticate" prompt. Reauth is only
  shown if that forced re-login itself fails (genuinely wrong password,
  captcha, or an account action required on Librus's own site) - something
  that really does need your attention. Confirmed live: this previously
  showed as "Autoryzacja wygasła" ("Authorization expired") in Home
  Assistant's repairs list even though the stored password was still
  correct.

## 0.4.1

- The unread-messages sensor's `mailbox_breakdown` attribute now shows the
  per-mailbox unread count (inbox/notes/alerts/substitutions/absences/
  justifications/trash) - the Wiadomości unread-count call already returns
  all of these, so this is exposed at zero extra API cost.

## 0.4.0

Pulled the complete feature list from szkolny.eu's own open-sourced app
(`LibrusFeatures.kt`) and live-probed every plausible gap against the real
account to close as many of them as possible without guessing at unseen
response shapes.

- **New:** School sensor (name, address, head teacher, contact details).
- **New:** Class sensor (class name, homeroom teacher, semester/school-year
  boundary dates).
- **New:** Free days calendar - the whole school year's holidays/breaks
  (`SchoolFreeDays` + `ClassFreeDays`).
- Agenda calendar events are now prefixed with their category when known
  (e.g. "[Sprawdzian] Matematyka: ...", via `HomeWorks/Categories`).
- Confirmed live and closed: the `Grades/Types` reference endpoint verifies
  every non-numeric grade mark Librus uses is correctly excluded from
  averages (see README's "Known limitations").
- `VirtualClasses` and `ParentTeacherConferences` client methods added but
  not wired into any entity yet - both confirmed real, both empty on the
  test account so far.

## 0.3.0

- **New:** an original inline brand icon (a generic notebook design, not
  Librus's own logo) - the integration page no longer shows HA's generic
  fallback placeholder.
- **New:** four ready-to-import automation blueprints (new grade, new
  behaviour notice, new announcement, new message notifications) under
  `blueprints/automation/librus_synergia/`.
- `librus_synergia_new_grade` and `librus_synergia_new_note` events now
  include the resolved subject/teacher name alongside the raw id, so an
  automation doesn't need its own lookup.

## 0.2.0

- **New:** Wiadomości (private messages) support - an unread-inbox-count
  sensor with sender/topic/preview for recent messages, and a
  `librus_synergia_new_message` event. Reading it never marks anything read
  in real Librus - only the message list/count endpoints are ever called,
  never the per-message detail one.
- **Changed:** the Attendance sensor now counts real absences instead of
  every attendance record (which was mostly just ordinary "present" marks) -
  the full breakdown, including presence marks, is still in its attributes.
- Fixed the device/entry title showing the login owner's name instead of the
  student's, for accounts where those differ (e.g. a parent-managed login).
- Fixed HACS/hassfest validation issues from the initial release (an invalid
  manifest key, missing repo topics).

## 0.1.0

- Initial release. Login via the same cookie/session flow Librus's own
  website uses (reverse-engineered from `emsi/librus_pyapi`), avoiding the
  reCAPTCHA-prone flow other Librus integrations use. Grades, attendance,
  behaviour notices, timetable, agenda, announcements and lucky-number
  sensors/calendar entities.
