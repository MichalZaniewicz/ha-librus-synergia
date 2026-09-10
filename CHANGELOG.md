# Changelog

## 0.5.0

A big round of "surface more from data already fetched" - six new sensors,
three new bus events, four new blueprints, an options toggle and a
`refresh` service. Everything client-side over the existing poll data, so
no extra load on Librus. All of it also feeds a matching batch of
companion cards.

### Added
- **`librus_synergia.refresh` service** - force an immediate data refresh
  (e.g. right before a morning-briefing automation). Optional `device_id`;
  omit to refresh all students.
- **Low Grade Alert blueprint** - runs your action only for a new grade
  at or below a threshold (default 2), ignoring a trailing +/- and any
  non-numeric mark.
- **Morning Briefing blueprint** - at a set time on school days, builds a
  one-line briefing (first lesson + room, today's lucky number, any test
  within 3 days) from the Next lesson / Lucky number / Next exam sensors
  and runs your action (speak it on a media player, or notify).
- **"Fetch private messages" option** - a toggle in the integration's
  Configure dialog. Off = the coordinator skips the whole Wiadomości
  subsystem (its separate session bootstrap + the per-cycle unread-count
  / list calls) and the Unread messages sensor reports `unavailable`,
  for schools without the module or anyone wanting fewer requests.
- **`id` on each Homework assignments `recent` entry** - so a companion
  card can track per-item state (e.g. a done/undone checklist).
- **Unexcused absences sensor** (`sensor.*_unexcused_absences`) - just
  the count of real absences that still need a justification (the
  Attendance sensor blends excused and unexcused into its state).
  `recent_dates` in attributes lists the days involved. Pairs with a new
  **`librus_synergia_new_absence`** event (fires for any newly-seen
  absence record, with an `excused` flag) and an **Unexcused Absence
  Notification** blueprint (only pings for still-open ones).
- **Next exam sensor** (`sensor.*_next_exam`) - date of the soonest
  future Agenda entry whose category looks like a graded assessment
  ("Sprawdzian", "praca klasowa", "kartkówka", "egzamin", "diagnoza" -
  matched on the category name, deliberately conservative). State is a
  date (`device_class: date`); attributes carry `days_until`, subject,
  category, description and an `upcoming` list.
- **Per-semester and arithmetic grade averages** - the Overall grade
  average and each subject average sensor now expose
  `average_arithmetic` (plain mean of the same counted grades) and
  `average_semester_1` / `average_semester_2` (weighted, scoped to that
  semester) as attributes. The state is unchanged - still the weighted
  all-time average.
- **`librus_synergia_new_homework` event** - fires for a new Agenda
  ("HomeWorks") entry (tests, trips, events), carrying the resolved
  subject and category name so an automation can filter e.g.
  `category == "Sprawdzian"`. New **New Agenda Entry Notification**
  blueprint wraps it, with an optional category filter.
- **Next lesson / Current lesson sensors** - state is the subject name;
  attributes carry the start/end time, `minutes_until` / `minutes_left`,
  teacher, classroom, period number and whether it's a substitution.
  Cancelled slots are skipped. All client-side over the existing
  current+next-week timetable - no extra API calls - so a "leaving for
  school" TTS or a countdown card doesn't have to re-derive it from the
  Timetable calendar.
- **`bell_schedule` attribute on the School sensor** - period number ->
  start/end time, derived from the times that actually appear in the
  student's timetable (most common pair per period wins, so an odd
  shortened day can't redefine the normal bell times).
- **`librus_synergia_timetable_changed` event** - fires when a lesson on
  today or a later date newly turns up cancelled or as a substitution vs.
  the previous poll. Seeded silently on the first sync, same as the other
  `*_new_*` events, and signature-keyed on date+period+kind so a known
  disruption isn't re-announced every cycle. Carries the resolved subject
  name, date, period number, kind and start time. A new **Lesson Change
  Notification** blueprint wraps it.

## 0.4.21

Built for the new companion "Absences by weekday" chart card.

### Added
- **`by_weekday` attribute on the Attendance sensor** - excused/unexcused/
  late record counts grouped by ISO weekday (1=Monday..7=Sunday), for a
  "which day of the week is this happening on" chart. Distinct from
  `by_date`: that attribute holds one blended status per calendar date and
  folds "late" into plain "good"; this one counts every matching record
  per weekday across all three categories. "Late" ("Spóźnienie") is a
  presence-kind type just like plain "Obecność" and has no dedicated API
  flag either - identified the same best-effort way as excused absences,
  by matching "późn" in the type's own name.

## 0.4.20

Built for the new companion "Attendance heatmap" card (a GitHub-
contributions-style calendar of the school year so far).

### Added
- **`by_date` attribute on the Attendance sensor** - one status
  ("good"/"warn"/"bad") per calendar date, not per record. A single day
  can carry several period-level attendance records; when it does, the
  day takes its WORST status (one unexcused-absence period outweighs an
  otherwise-present day), reusing the same status classification the
  companion cards already compute client-side.

## 0.4.19

**Real bug found live**: an excused absence ("Nieobecność uspr.") kept
showing identically to a still-open unexcused one in every summary/tile
view - only the full Attendance card's own per-type legend distinguished
them at all, and even there both rendered in the same "bad" red color
(Librus has no separate "is this excused" API flag - `IsPresenceKind`
only says present/not, so both correctly count as "not present").

The Attendance sensor now exposes `excused_count` and `unexcused_count`
alongside the existing blended total, splitting on the type name
containing "uspr." (skrót od "usprawiedliwiona") - the best signal
available without a real API flag, same best-effort class as this
codebase's other name-based heuristics. The sensor's own state is
unchanged (still the blended total - both kinds of absence genuinely mean
the student wasn't there).

## 0.4.18

User request: "czy usprawiedliwienia można jakoś pobrać i pokazać?" (can
justifications be fetched and shown?), asked right after submitting a
real absence excuse still awaiting acceptance.

**"Usprawiedliwienia" (justifications) now get full content, not just an
unread count.** Same architecture already proven for "Zastępstwa"
(substitutions) and "Alerty" (alerts) in 0.4.13 - all these mailboxes are
sibling keys in the same unread-count response and share the identical
`{mailbox}/messages` list endpoint, so this extends a pattern already
live-verified rather than guessing at a new one. Exposed as a new
`justifications_recent` attribute on the Unread messages sensor,
following the exact `substitutions_recent`/`alerts_recent` shape.

UNVERIFIED: whether a submitted justification's accept/reject status is
actually visible in this mailbox's message content, or only the school's
own free-text response - the user's real pending justification will
confirm this on the next real update.

## 0.4.17

Two real bugs, both found live by the user.

**"ogłoszeń nie można odczytywać?" (announcements can't be read)**: the
Unread announcements sensor's `recent` attribute truncated `content` to
200 characters for no real reason - unlike the Wiadomości mailboxes,
Librus does NOT truncate this endpoint's content server-side, so this was
this integration's own doing. Now exposes the full content (plus an `id`
field, for the companion card's click-to-expand). No extra API call and
no read-marking side effect either - this data was already being fetched
in full every cycle.

**"co to za bug z treścią wiadomości?" (message content bug)**: a card's
expanded full-message view showed the literal `<Message><Content>` `
<![CDATA[` prefix leaking into the display. The single-message endpoint's
`Message` field, once base64-decoded, isn't plain text - it's a tiny XML
wrapper around the real content in a CDATA section. `decode_message_content`
now strips this wrapper (falls back gracefully if the closing `]]>` is
ever missing). The list endpoint's `content` field is unaffected -
confirmed plain text, no wrapper.

## 0.4.16

**Real bug, found live**: the Attendance sensor's `breakdown` attribute
only ever exposed a name → count map, with no indication of which names
count as a presence. The companion cards guessed from the name text
(`/obecno/i`), which also matches *inside* "Nieobecność" (absence) since
it literally contains "obecność" as a substring - "Obecność" (present)
and "Nieobecność" (absent) rendered as the SAME color on the Attendance
card. Now exposes a `presence_by_type` map (name → bool, sourced from the
school's own `AttendanceTypes[].IsPresenceKind` - the same authoritative
source the sensor's own state/percentage already use) so consumers don't
have to guess from Polish text at all.

Code-review pass (no new user-visible features) closing three real
resilience gaps plus one data-normalization hardening:

- **`_async_fetch_core_payloads` no longer fails all-or-nothing.** All 16
  core+supplementary endpoints used to sit in ONE `asyncio.gather()` -
  exactly the same failure class already fixed once for the Wiadomości
  mailboxes in 0.4.14 (`asyncio.gather()` discards every already-succeeded
  result the moment any ONE awaitable raises). A single flaky/newer
  endpoint (e.g. `DescriptiveGrades`, `ParentTeacherConferences`) could
  wipe grades/attendance/timetable/notices for the whole cycle. Split into
  two tiers: the original core sensors stay in one gather (still fatal on
  failure, still drives the forced-relogin-and-retry-once recovery), the
  newer supplementary endpoints are now fetched with
  `return_exceptions=True` so one failing only degrades that one entity to
  empty, never the rest.
- **Bare-JSON-array normalization is no longer endpoint-blind.** 0.4.14
  fixed a real bug (a secondary Wiadomości mailbox returning a bare array)
  by having `_async_read_json` wrap ANY bare array under a hardcoded
  `"data"` key - correct for Wiadomości, but silently wrong for any other
  endpoint that might someday do the same (e.g. `Grades/Comments`, which
  uses a `"Comments"` key). Now opt-in per call site
  (`array_envelope_key=...`) - only the one endpoint that has actually been
  observed doing this gets normalized; anything else still fails loudly.
- **Timetable week cache now expires.** `LibrusTimetableCalendar` caches
  on-demand-fetched weeks (for date ranges outside the coordinator's own
  current+next-week poll window) forever for the entity's lifetime -
  correct once, then silently stale if a substitution changed later. Now
  refetches after 24h, and falls back to the stale cached data (rather than
  nothing) if a refresh attempt fails.

Companion cards (`ha-librus-synergia-cards` 0.2.3):
- **Cards no longer rescan the entity registry on every unrelated state
  change.** Home Assistant hands every card a new `hass` object on ANY
  state change anywhere in the instance, and every card's device/entity
  resolution ran a full linear scan of `hass.entities` on every single
  render as a result. `LibrusBaseCard._resolveEntities()` now memoizes its
  result against the specific `hass.entities` reference it was computed
  from - that reference only actually changes when the entity registry
  itself changes (a rename, a reload, a device added/removed), not on
  every state update.
- **"What's new" feed's sort fixed for same-day items.** Grades/notes/
  announcements carry a bare `YYYY-MM-DD` date, messages a full
  `YYYY-MM-DDTHH:MM:SS` timestamp - sorting the two directly always sorted
  a same-day bare date as "later" than any specific time that day (a grade
  added at 07:00 could show up above a message from 20:00 the same day).
  Bare dates are now padded to midnight before comparing, so same-day items
  interleave in a defined, sensible order.
- **Attendance card: "Obecność" and "Nieobecność" no longer share a color**
  (see the backend `presence_by_type` fix above - the card now reads that
  instead of guessing from the name).
- **Announcements/Behaviour notices tile cards now say what the number
  means.** Both used to show a bare count with no label at all (`1`, `0`)
  - now read `1 announcements`/`0 behaviour notices` (translated), matching
  the Attendance/Messages tiles, which already did this correctly.

## 0.4.15

**Real bug, found live**: the Lucky number sensor always presented its
value as "today's" number without ever checking Librus's own
`LuckyNumberDay` field against the actual current date. Confirmed live
(cross-checked against the real Librus app on a Sunday) that Librus can
publish the *next* school day's number a day ahead - the sensor was
showing that value as if it were for today regardless.

The state is still the most recently published number (unchanged), but
the sensor now also exposes:
- `day` - the actual date (YYYY-MM-DD) the number applies to.
- `is_today` - whether that date is really today.

Companion cards updated to use this: the Lucky number card now shows
"For {date}" instead of "Today" when the number isn't actually for
today, and the Today summary card omits the lucky-number tile entirely
in that case (its whole premise is "what matters today").

## 0.4.14

**Real regression from 0.4.13, found live within hours of shipping**: the
Unread messages sensor's `mailbox_breakdown`/`recent` went from real data
to completely empty (`{}`/`[]`) - not just the new `substitutions_recent`/
`alerts_recent` failing, but the previously-solid inbox unread-count and
message list too.

Root cause: one of the "substitutions"/"alerts" mailboxes' list endpoint
returns a bare JSON array instead of the `{"data": [...]}` envelope every
other endpoint in this client uses, raising `LibrusUnexpectedResponseError`.
0.4.13 fetched all four calls (unread-count, inbox, substitutions, alerts)
in a single `asyncio.gather()` - which fails as a whole the moment any ONE
of its awaitables raises, so this one shape mismatch silently wiped out
the otherwise-working inbox data too.

Fixed at both levels:
- `_async_read_json` now normalizes a bare JSON array into `{"data": [...]}`
  instead of raising - substitutions/alerts should now actually populate,
  not just fail gracefully.
- Belt-and-suspenders: the coordinator now fetches inbox/unread-count and
  substitutions/alerts as two separate, independently-failing steps, so a
  problem with the bonus mailboxes can never take the core inbox data
  down with it again.

## 0.4.13

Two feature-parity additions inspired by a comparison against
`dani3l0/librusik` (a third-party Librus web client):

- **Attendance sensor**: new `percentage` (independently computed - works
  even if your school disables Librus's own average display) and
  `by_semester` (per-semester count + percentage) attributes.
  `AttendanceData.semester` was already parsed but never actually used
  until now.
- **Unread messages sensor**: full content (not just an unread count) for
  the two secondary mailboxes most worth actually reading -
  `substitutions_recent` and `alerts_recent`, same shape as the existing
  `recent` (inbox) attribute, each message tagged with its own `mailbox`.
  The `get_message` service gained a matching optional `mailbox` field
  (defaults to `inbox`) so a card can fetch full content for one of these
  too, not just an inbox message.

## 0.4.12

**Real bug, found live**: any calendar query ending exactly at local
midnight (which is what "today", "this week", and every other card in
this project's own companion cards repo actually requests) wrongly
included the ENTIRE next day too. Confirmed live on a real Sunday: asking
the Timetable calendar for "today" (no lessons - the school week hadn't
started) returned Monday's full 6-lesson day instead of nothing.

Root cause: `calendar.py`'s `async_get_events` filtered by comparing bare
calendar dates (`event.start.date()` vs. `end_date.date()`), but a
half-open `[start, end)` window's `end` at exactly midnight belongs to
the *next* calendar date - so the date-only filter treated that whole
next day as included. Fixed in all three calendars (Timetable, Agenda,
Free days) - the Timetable calendar now compares lessons' actual
start/end datetimes against the real window instead of reducing either
side to a bare date; Agenda and Free days use a new `_inclusive_end_date`
helper that correctly resolves the last date actually inside the window.

## 0.4.11

New `librus_synergia.get_message` service: fetches ONE message's full,
untruncated content (the Unread messages sensor's `recent` attribute only
ever carries Librus's own truncated preview). **Confirmed live that this
marks the message read on Librus's servers**, exactly like opening it in
the Librus app - so it is deliberately a service, never wired into the
coordinator's routine polling, and only ever meant to run on someone's own
explicit action (e.g. clicking a message in a dashboard card). The Unread
messages sensor's `recent` attribute now also carries each message's `id`,
needed to call the new service. See the README's new "Services" section.

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
