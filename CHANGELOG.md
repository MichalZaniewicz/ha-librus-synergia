# Changelog

## 0.4.10

Two real bugs found live while building the new "Szkoła" HA dashboard with
every companion card installed:

- **Wiadomości card showing an unbroken base64 blob (horizontal scroll)**:
  Librus's message-list endpoint truncates the base64-encoded `content`
  field to a fixed byte length, which can land mid a multi-byte UTF-8
  character (e.g. a Polish "ą"/"ę"/"ń"). Decoding that then raised
  `UnicodeDecodeError`, and the integration fell all the way back to the
  raw, still-base64-encoded string - rendered by the card as one long
  unbroken blob. Now decodes the readable prefix instead (drops only the
  incomplete trailing bytes), matching how every other truncated message
  already reads.
- **Week-timetable card showing "Coś poszło nie tak"**: `LibrusTimetable
  Calendar.async_get_events` (used by any dashboard asking for a date
  range outside the coordinator's own current+next-week cache) called the
  API client directly, with none of the coordinator's forced-relogin-and-
  retry-once recovery for a session that expired mid-cycle. A live session
  expiry crashed the whole `/api/calendars/<entity>` request with an
  unhandled 500. The coordinator now exposes a reusable
  `async_fetch_timetable_week` that applies the same one-retry recovery
  outside the normal poll cycle too; if that retry also fails, the
  calendar degrades to "no lessons known for that week" instead of
  crashing the request.

## 0.4.9

- Fixed the automation blueprints table: all four "Import Blueprint" badges
  looked identical and sat in an unlabeled row below the table, with no
  visual way to tell which button imported which blueprint. Each badge now
  sits inline in its own row, next to that blueprint's name and
  description.
- Split the Entities table's `sensor`/`calendar` type tag into its own
  column instead of prefixing the entity name - the mixed tag+name text
  was wrapping awkwardly on narrower screens.
- Documented the companion **[Librus Synergia Cards](https://github.com/MichalZaniewicz/ha-librus-synergia-cards)**
  repo (22 cards, shipped this session) in the README - see the "Custom
  Lovelace cards" section.

## 0.4.8

Two more attributes needed by the new `ha-librus-synergia-cards` repo's
card designs:

- The *Subject average* sensor now exposes a full `grades` attribute (the
  complete per-grade log for that subject - value/category/date/comments,
  newest first), not just the latest one - needed for a "grade log" style
  card, per-subject or across every subject at once.
- The *Attendance* sensor now exposes `last_absence_date`, for an
  absence-free-streak style card.

## 0.4.7

- The *Subject average* sensor now exposes a plain `subject` attribute
  (the resolved subject name, e.g. "Matematyka") - needed by the new
  companion `ha-librus-synergia-cards` repo to group per-subject data,
  since the only place the name previously appeared was baked into
  `friendly_name` via a per-language translation string, which is fragile
  to parse back apart in JavaScript.

## 0.4.6

- Fixed an oversight from v0.4.5: the *Subject average* sensor's latest-
  grade info never actually exposed the resolved `Grades/Comments` text,
  even though the correlation fix that round computed it correctly. New
  `latest_grade_comments` attribute.

## 0.4.5

Closed most of the remaining feature-parity gaps using exact field names
read from szkolny-eu/szkolny-android's own reference parser (not guessed) -
even without any real populated example on the test account yet.

- **Resolved a long-standing unverified detail**: `Notes[].Positive` is now
  confirmed - `0` = negative, `1` = positive, anything else = neutral. The
  Behaviour notices sensor's `recent` attribute now shows this as
  `sentiment` instead of the bare number.
- **Fixed a real bug**: grade comments were assumed to be embedded inside
  each `/Grades` item - they're actually a separate `Grades/Comments`
  endpoint, with each grade's own `Comments` field holding ids into it.
  Fixed the correlation (handles both a bare-id list and an object-id
  list defensively).
- **New:** Homework assignments sensor - real "zadania domowe", distinct
  from the Agenda calendar's general feed.
- **New:** Behaviour grade sensor - the formal "ocena zachowania", distinct
  from the free-text Behaviour notices ("uwagi") sensor.
- **New:** Descriptive grades sensor - this school has descriptive grades
  enabled (confirmed via the `Units` endpoint) instead of point-scale
  grades.
- Parent-teacher conferences (`ParentTeacherConferences`) are now merged
  into the Agenda calendar as a defensive extra - live-verified redundant
  with the general agenda feed for this account, but costs nothing to add.
- None of the three new sensors have ever shown real data - their
  endpoints have been empty every time this account has been checked.
  `PointGrades` (confirmed disabled for this school) and `TextGrades`
  (enablement unknown) remain deliberately unwired.

## 0.4.4

Deeper pass over szkolny-eu/szkolny-android's raw endpoint list (every
`LibrusApi*.kt` file, not just the higher-level feature-flag list used for
the 0.4.0 round), live-probed against the real account.

- **Behaviour notices** sensor's `recent` attribute now resolves each
  note's category id to its real name (e.g. "Praca na lekcji") via the
  `Notes/Categories` endpoint - confirmed live with real data.
- New client methods for endpoints confirmed real+reachable but empty on
  the test account, not yet wired into any entity (same treatment as
  `VirtualClasses`/`ParentTeacherConferences` before them):
  `BehaviourGrades/Points` (+ Categories) - a formal "ocena zachowania"
  behaviour grade, distinct from Notes/"uwagi"; `PointGrades`,
  `DescriptiveGrades`, `TextGrades` - alternate grading systems (this
  account's school has descriptive grades enabled, not point grades, per
  the new `Units` endpoint); `Grades/Comments`.
- **Flagged, not yet fixed**: `Grades/Comments` turned out to be a
  *separate* endpoint from `/Grades`, casting real doubt on this
  integration's assumption that grade comments arrive nested inside each
  `/Grades` item. Unconfirmed either way - no real grade with a comment
  exists yet to check against.

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
