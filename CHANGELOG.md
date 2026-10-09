# Changelog

## 0.12.2-beta.3

### Fixed
- **Homework attachments download now.** Checked with a real file: the
  download goes through a sandbox.librus.pl waiting page, which
  librus-synergia 0.3.9 handled the wrong way (0.3.10 fixes it).
- A failed attachment download or timetable week now logs why.
- No more deprecation warning about `DeviceEntry.config_entries` (it would
  stop working in Home Assistant 2027.10).

### Changed
- Requires librus-synergia 0.3.10.

## 0.12.2-beta.2

### Added
- **Sent messages and the archive.** The *Unread messages* sensor has
  `outbox_recent` (messages you sent, each with a `receiver`) and
  `archive_recent` (past school years, read once a day). `get_message` opens
  them with `mailbox: outbox` / `archive/inbox`.
  `missing_mailboxes` lists the mailboxes the account doesn't have (Librus
  answers 404), so the Messages card can hide them.
- **Homework attachments.** *Homework assignments* `recent[].attachments`
  lists the files a teacher attached (`id`, `filename`), and
  `/api/librus_synergia/homework_attachment/<device id>/<attachment id>`
  passes one straight from Librus to the browser. Not tried live yet - no
  teacher has attached a file on the test account.
- **Assist:** the attendance tool says what to catch up on after the latest
  absence (missed lessons with topics, homework given meanwhile); the
  messages tool lists sent messages; homework comes with the names of its
  files (also in the weekly AI summary).

### Fixed
- A mailbox the account doesn't have (Librus answers 404, seen for alerts and
  substitutions) no longer marks messages as degraded, and one failing
  mailbox no longer empties the others.

### Changed
- Requires librus-synergia 0.3.9.

## 0.12.2-beta.1

### Added
- **What to catch up after an absence.** The *Lesson topics* sensor has a
  `catch_up` attribute for the latest absence (within three weeks): the
  period (`from`, `to`, `days`), the day the student was back (`back_on`,
  `back_today`), the lessons missed with their topics and the homework given
  meanwhile. New blueprint **What to Catch Up After an Absence** sends it on
  the day back at school (30 blueprints).

### Fixed
- New-item events: when a part of Librus is missing on the very first
  refresh (Wiadomości not answering, homework, trips, documents or text
  grades failing), or when messages or announcements are switched on later,
  or a timetable is published later, the items that show up then are
  recorded silently instead of arriving as a batch of "new" notifications.

## 0.12.1

Everything from 0.12.1-beta.1.

### Added
- **What to revise for a test.** The *Next exam* sensor lists, for the next
  test and for each one in `upcoming`, the topics taught in that subject since
  the previous test in the same subject (or since the start of the school
  year): `topics` (date, lesson number, topic, `absent`), `topics_since`,
  `missed_topics`, `more_topics`. New blueprint **What to Revise Before a
  Test** sends them a few days ahead (29 blueprints).
- **Changes to the usual timetable.** A new sensor compares this week and
  next with the standing weekly plan Librus keeps (`TimetableEntries`): the
  state is how many lesson slots from today on differ, `changes` lists them
  (cancelled, missing, extra, another subject, another room, a weekday
  without lessons). The standing plan is read once a day.
- **Ask Assist knows more.** A new tool, `librus_get_lesson_topics` (what was
  taught, which lessons the student missed), and the existing tools now
  cover the topics to revise for each test, school trips, differences from
  the usual plan, text and descriptive grades, the justifications sent with
  the school's decision, and the school's documents (eight tools).
- **The weekly AI summary sees more:** the topics of the lessons missed this
  week, the topics to revise for next week's tests, school trips, new school
  documents, waiting or rejected justifications, and text grades.
- Absence justifications: each entry in `recent` carries `decision`
  (`accepted` / `rejected` / `pending`).

### Changed
- Requires `librus-synergia` 0.3.7.

### Fixed
- Topics to revise: a topic the teacher entered for several lessons is listed
  once, with every date in `dates` and the count in `lessons` (it showed up
  once per lesson).

## 0.12.0

Everything from the 0.11.1 betas.

### Added
- **Grade forecast.** A *Grade forecast* sensor: what each subject's
  average gives on the report card (thresholds under Configure, default
  1.75 / 2.75 / 3.75 / 4.75 / 5.50), how many 6s lift a grade and how many
  1s drop it, subjects heading for a 1, subjects whose forecast fell in the
  last two weeks, and the forecast report-card average. First semester's
  grades until the semester ends, then the whole year. Subject average
  sensors carry the same forecast in attributes. Binary sensor **Grade at
  risk** (on while a subject points to a 1), event
  `librus_synergia_forecast_changed` with an event entity, and the blueprint
  **Grade Forecast Changed**. Ask Assist and the weekly AI summary see the
  forecast too.
- **Lesson topics.** Sensor *Lesson topics* (today's and the last 14 days'
  topics with subject and lesson number; lessons the student missed are
  marked `absent`); past lessons in the Timetable calendar get a
  "Temat: ..." line.
- **Text grades.** Grades a teacher enters as text never showed up anywhere -
  they live in a separate place in Librus. Subject average sensors now carry
  them (`text_grades`, on one line), `get_grades` returns them, and a new one
  fires `librus_synergia_new_grade` with `kind: text`.
- **Point grades** for schools grading in points or percent (e.g. 0-100):
  a *Point grades* sensor (share of points earned, weighted by category,
  per subject in attributes), `points_percentage`/`point_grades` on the
  subject average sensors and `point_grades` in `get_grades`. Created only
  where the school uses them; the requests are skipped where the school
  configuration says they are off. A number outside the 1-6 scale in the
  regular grades (e.g. "85") no longer counts towards the averages.
- **School trips.** Sensor *Next school trip* (date, destination, route,
  transport, coordinator), event `librus_synergia_new_school_trip` + event
  entity, and the blueprint **School Trip Tomorrow**.
- **School documents.** Sensor *School documents* (with links), event
  `librus_synergia_new_school_document` + event entity, and the blueprint
  **New School Document**.
- **Message attachments.** A logged-in endpoint
  (`/api/librus_synergia/attachment/<device>/<message>/<attachment>`) passes a
  Wiadomości attachment straight from Librus to the browser, without saving
  it in Home Assistant and without opening the message in Librus. The
  companion Messages card uses it when a file name is tapped.
- **Absence justifications.** Sensor *Absence justifications* (how many wait
  for the school's decision, with the list and statuses), event
  `librus_synergia_justification_status` when one is accepted or rejected,
  event entity *Absence justification decided* and the blueprint **Absence
  Justification Decided**. The Unexcused absences sensor gains
  `awaiting_justification` and `justification_sent`, and the Absences To
  Justify Reminder skips days a justification was already sent for.
- **Agenda entry changed or removed.** Event `librus_synergia_agenda_changed`
  when an upcoming Agenda entry changes (`kind: changed`: date, time,
  description, subject or category, with `changed_fields` and the old
  values in `previous`) or disappears from Librus (`kind: removed`). Event
  entity *Agenda entry changed* (`changed` / `removed`) and the blueprint
  **Agenda Entry Changed or Removed**. Entries dated before today are
  ignored; the first sync only records what's there.
- **What a substitution changes.** Librus marks room changes and moved
  lessons as substitutions too, with the original lesson attached. The
  Timetable calendar now titles them "(zmiana sali)" / "(przeniesiona)"
  and adds the details to the description ("Zastępstwo za: Chemia, Jan
  Kowal", "Zmiana sali: 12 → 21", "Przeniesiona z: 29.09, lekcja 3"). The
  same goes into `librus_synergia_timetable_changed` (`change`, `teacher`,
  `original_subject`, `original_teacher`, `classroom`, `original_classroom`,
  ...) and the Next/Current lesson attributes; the Lesson Change blueprint's
  message says what changed.
- **Works through Librus outages.** The last good response of every part of
  Librus is saved. A part that fails (grades, timetable, subject names, ...)
  shows its last good copy instead of going empty; if Librus doesn't answer
  at all, the last data stays up for up to 3 days instead of every entity
  going unavailable, and when Home Assistant starts during an outage the
  data is rebuilt from the saved responses instead of the integration
  failing to load.
- **Retry backoff.** After two failed refreshes in a row the next attempts
  are spaced out (2x, 4x the poll interval, up to 2 hours) - no login
  attempt every cycle during an outage. The refresh action ignores it.
- Diagnostic sensors **Connection status** (`ok` / `degraded` / `stale` /
  `error`, with the last error, failures and next attempt) and **Last
  successful update**. Diagnostics gained a `connection` section.
- Blueprint **Copy School Events to a Calendar**: tests, quizzes, trips,
  meetings and days off copied into a calendar of your choice (daily and on
  a new Agenda entry), filtered by Agenda category, without duplicates.
  28 blueprints in total.
- Homework assignments carry their category name (`category`).

### Changed
- **Fewer logins.** The session is renewed through Librus's own refresh
  (`refreshToken`) once it's two hours old, instead of a password login every
  day.
- The class register number is read from Librus's JSON (the student's own
  user record), with the web page only as a fallback.
- What has already been announced is saved, so a grade, note, absence or
  message that arrives while Home Assistant is off still fires its event
  after the restart (it used to be swallowed as part of the silent first
  poll).
- Long attribute lists (grade logs, `recent` lists, attendance breakdowns,
  bell schedule, ...) are no longer written to the recorder's history -
  a smaller database; the live attributes are unchanged.
- Requires `librus-synergia` 0.3.6.

## 0.11.0

### Added
- **Ask Assist about school.** A "Librus Synergia" tool set (LLM API) that
  a conversation agent turns on next to *Assist*: seven read-only tools for
  the timetable, grades and averages, what's coming up, attendance,
  behaviour, school/class info and messages. Answers come from data already
  fetched, so questions add no Librus requests.
- **Event entities** for new grades, behaviour notes (positive / negative /
  neutral), absences (excused / unexcused), timetable changes (cancelled /
  substitution), homework, agenda entries, announcements, messages and
  achievements - automations from the UI, entries in the logbook. The bus
  events are unchanged.
- **School day sensors.** Binary sensors *School day today*, *School day
  tomorrow* and *At school*; timestamp sensors *School start* and *School
  end* for automations with an offset. Re-checked every minute.
- Blueprints **School Wake-Up** and **School Pick-Up Reminder** (22 now).
- **Homework to-do list** (`todo` entity) with due dates; ticks are stored
  in Home Assistant.
- **Grade-average history in long-term statistics**: overall and
  per-subject averages for every day since the first grade.
- **Class register number read from Librus** (Synergia's *Informacje*
  page): the Lucky number sensor's `is_yours` works without setup, a number
  entered under **Configure** still wins (`student_number_source`). The
  Class sensor carries `student_number` too.
- **Grade corrections ("poprawy")**: `improves` / `improved` in the subject
  `grades` attribute and `get_grades`, `improves` in the new-grade event.
- Options **Smart polling** (off by default: the poll interval on school
  days 06:00-22:00, at most hourly on days without lessons, at most every
  3 hours at night) and **Hide subjects without grades**.

### Fixed
- Behaviour grade `recent[].comments` kept Librus's padding spaces.

### Changed
- Requires librus-synergia 0.3.2.

## 0.10.0

### Added
- **Weekly AI summary.** Once a week (day and time of your choice, default
  Sunday 18:00) an AI model writes a summary of the school week: grades and
  how the averages moved, attendance, behaviour, and what is coming next
  week (tests, homework due, timetable changes, free days), plus 2-4 to-dos
  and a warning only when something needs attention. It runs through Home
  Assistant's AI Task, so there is no API key here - set up any AI provider
  (Google Gemini, OpenAI, Anthropic, a local Ollama...) and pick it under
  **Configure -> Weekly AI summary**. Choose who it is written to: the
  parent or the student. Private messages and announcements are only sent
  to the AI if you turn that on (off by default). Only data the
  integration already has is used - no extra Librus requests. A week with
  no lessons and nothing coming up is skipped, so holidays cost nothing.
  Every date sent to the AI carries its weekday (the first live run showed
  the model getting weekdays wrong otherwise). Step-by-step setup in the
  README's *Weekly AI summary* chapter.
  - New entities, only while the feature is set up: **Weekly summary**
    sensor (headline as the state, sections/advice/warning in attributes),
    **Generate weekly summary** button and **Automatic weekly summary**
    switch.
  - New `librus_synergia_weekly_summary` event and **Weekly AI Summary
    Report** blueprint (short or full report, choose sections, only for
    weeks that need attention if you like).

### Fixed
- **Behaviour grade was blank for schools using the classic scale.** A real
  monthly "bdb" showed up as an empty Behaviour grade sensor: Librus sends
  that grade only as an id (1 wz ... 6 ng), which wasn't read. The sensor
  now shows "bdb" (or the points, for a points-based school), with the full
  name ("bardzo dobre") and the teacher's comment ("Ocena zachowania
  miesiąc za IX/26") in the `name`/`comment` attributes; every `recent`
  entry gets `grade` and `name`.

### Changed
- **Configure** now opens a menu: *Settings* (everything that was there
  before) and *Weekly AI summary*.
- Requires `librus-synergia` 0.3.1 (installed automatically).

## 0.9.2

### Added
- **Notification for new homework assignments.** A new
  `librus_synergia_new_homework_assignment` event fires for each new real
  homework assignment ("zadanie domowe", the Homework assignments feed)
  with its topic, text, due date, teacher and subject. Until now only the
  Agenda (tests, trips) had an event. New **New Homework Assignment
  Notification** blueprint.
- **Subject for homework assignments.** Librus doesn't say which subject an
  assignment belongs to, so it's now worked out from the teacher's lessons
  in the timetable - in the Homework assignments sensor's `recent`
  attribute and in the new event. Left empty when the teacher teaches more
  than one subject.
- **Teacher in grades.** The new-grade event, the subject sensors' `grades`
  attribute and the `get_grades` service now include the teacher who added
  the grade. The **New Grade Notification** blueprint shows it in its
  details line (re-import the blueprint to get it).
- **Test Tomorrow Reminder** blueprint: an evening reminder when a test or
  quiz is on the Agenda for the next day.
- **Your Lucky Number** blueprint: a notification when the published lucky
  number is your child's own number (set under **Configure**).

### Changed
- Requires `librus-synergia` 0.3.0 (Home Assistant installs it on its own).

## 0.9.1

### Fixed
- **Parent meetings no longer show up twice in the Agenda calendar.** The
  same meeting comes from both the agenda feed and the parent-teacher
  conferences endpoint (same date and time, different wording); the
  second copy is now skipped. Seen for the first time on live data.
- **Lowest subject attendance ignores subjects with almost no records.**
  Some teachers don't take attendance in Librus at all, so a subject could
  have just 2 records (both absences) after a month and pin the sensor at
  0%. A subject now needs at least 5 records to count toward the state,
  `subject` and `at_risk`; it still appears in `subjects`. New
  `min_records` attribute.
- **Longer homework text.** The Homework assignments sensor's `recent`
  attribute kept only the first 200 characters of each assignment, which
  cut longer instructions mid-sentence. It now keeps up to 1000.

## 0.9.0

Includes everything from the `0.8.1-beta.1` testing release.

### Added
- **Lowest subject attendance sensor.** Attendance percentage of the
  subject where it is lowest, with every subject's own figure in the
  `subjects` attribute and the ones already under 50% in `at_risk` - the
  mark below which a student can be left unclassified. Computed from
  attendance data the integration already fetches, with no extra Librus
  requests.
- **Low Subject Attendance** blueprint: runs your action when that sensor
  drops below a percentage you set (default 60%).
- **Absences To Justify Reminder** blueprint: at a set time on the days
  you pick, reminds you (count + dates) for as long as there are unexcused
  absences left.
- **Average mode option.** Under **Configure** you can now choose whether
  the Overall/Subject average sensors (and Rank) report the weighted
  average (default, unchanged) or the plain arithmetic one. Both are
  always available as attributes (`average_weighted`,
  `average_arithmetic`); the per-semester attributes follow the selected
  mode.
- **Grade details in the new-grade event and blueprint** ([#12](https://github.com/MichalZaniewicz/ha-librus-synergia/issues/12)).
  `librus_synergia_new_grade` now also carries `category`, `weight`,
  `counts_to_average`, `comments`, `date`, `semester` and `kind` (normal
  grade, or a semester/final grade or its proposition). All of it comes
  from data the integration already fetches, with no extra Librus requests.
  The **New Grade Notification** blueprint has a new *Include details*
  option (on by default) that adds a second line, e.g.
  "Sprawdzian · waga 3 · 28.09" plus the teacher's comment, and names
  semester/final grades and their propositions as such. Re-import the
  blueprint to get the new option.

### Changed
- New-item detection (the `librus_synergia_new_*` and
  `librus_synergia_timetable_changed` events) now uses the `ChangeTracker`
  from the `librus-synergia` library (now `0.2.0`) instead of the
  integration's own copy of that logic. Events and their fields are
  unchanged.

## 0.8.0

Promoted from the `0.7.9-beta.1` and `0.8.0-beta.1` testing line.

### Changed
- **The Librus client now comes from the standalone
  [`librus-synergia`](https://pypi.org/project/librus-synergia/) library**
  ([repo](https://github.com/MichalZaniewicz/librus-synergia)) instead of a
  copy bundled inside the integration. It's the same code, extracted so
  other projects can use it, together with an
  [unofficial Librus API reference](https://michalzaniewicz.github.io/librus-synergia/).
  Home Assistant installs it automatically. Behaviour is unchanged, and
  entities, automations and blueprints keep working as before.

### Fixed
- **The long-lived `DeviceCookie` is now actually saved.** Librus sets it
  under the `/OAuth` path, and saving the session only looked at cookies
  for the site root, so it was always left out. The integration relies on
  that cookie to mark Home Assistant as a known device and keep logins
  free of captcha/2FA. It's picked up on the next login, with nothing to
  do on your side.

## 0.7.8

Promoted from the `0.7.8-beta.1`-`0.7.8-beta.2` testing line, plus PR #9.

### Added
- **Kindergarten (przedszkole) timetable** (issue #5, based on PR #8 by
  @Lucaspog). Kindergarten accounts get HTTP 403 from the regular
  `Timetables` endpoint; their timetable lives in a separate
  `/gateway/ms/kindergartens/...` API keyed by the child's own identifier.
  When `Timetables` is forbidden, the integration now looks for that
  identifier and, once found, fills the timetable calendar and
  current/next lesson sensors from the kindergarten API. Activity names
  ("Edukacja przedszkolna", "Religia"...), classrooms, teachers and the
  kindergarten group (shown as the class) are resolved too.
  - Regular student accounts are unaffected and make **no** extra
    requests. The lookup only runs after a `Timetables` 403, at most once
    a day if it finds nothing (e.g. a school that simply hasn't published
    its timetable yet, issue #4), and a failure in any of its requests
    can never fail the update or trigger re-authentication.
  - Diagnostics gained a `kindergarten` section (detected / where the
    identifier came from - never the identifier itself).
- Calendar lesson events list every teacher when a block has several.

### Fixed
- Kindergarten classrooms show their full name ("sala 1") instead of the
  bare symbol ("1"); a purely numeric room with no name gets a "sala"
  prefix, anything else ("s. 1", "12a") is kept as Librus returns it
  (PR #9 by @Lucaspog).

## 0.7.7

### Added
- **Absences broken down by subject** (user-requested feature: "widać
  który przedmiot jest najczęściej opuszczany") - the Attendance sensor
  gained a `by_subject` attribute, same excused/unexcused/late split
  already used by the existing `by_weekday` attribute, keyed by subject
  name instead of weekday. Resolved via a new `Lessons` reference
  endpoint (global lesson_id -> Subject/Teacher/Class lookup, distinct
  from `Timetables` - which only covers a rolling 2-week window and never
  exposes a lesson's own `Id`) - `Attendances[].Lesson.Id` correlates
  against it, confirmed live (2026-09-23) against 10 real records before
  writing any integration code. Fetched in the same 24h-cached
  reference-data batch as Subjects/Teachers/Classrooms. A record whose
  lesson_id doesn't resolve is skipped rather than bucketed under a
  fabricated "unknown" subject.

## 0.7.6

Promoted from the `0.7.6-beta.1`-`0.7.6-beta.3` testing line - a real,
live-reported Wiadomości reliability problem, confirmed fixed by direct
repeated observation on a real account throughout testing (empty ->
recovers on its own, message clicks load full content) rather than just
reasoning about it.

### Fixed
- **The Unread messages sensor could go permanently empty (and clicking
  a message to read it could fail outright) until a full Home Assistant
  restart.** The dedicated Wiadomości session (bootstrapped separately
  from the main Synergia one) turned out to die far more often than
  expected - repeatedly within an hour on a real account. Three related
  gaps, all fixed:
  - `_messages_bootstrapped` was only ever set `True`, never back - once
    either the bootstrap call or the primary fetch raised, every later
    poll cycle skipped the bootstrap step entirely and never tried again.
  - Recovering only on the *next* poll cycle still meant sitting empty
    for however long the poll interval is, every time it happened.
    Messages now gets the same immediate same-cycle retry the main
    session has had since `v0.4.2` - one extra bootstrap+fetch attempt
    right away, before ever surfacing a gap to the user.
  - The `get_message` service (clicking a message in a card) had its own,
    separate version of the same bug: its session-expiry recovery only
    ever refreshed the MAIN Synergia session, never the Wiadomości one -
    a retry reused the same stale Wiadomości cookies and failed again,
    this time uncaught. Now also forces a fresh Wiadomości bootstrap.
- Messages no longer runs in the same `asyncio.gather()` burst as the
  10-request reference-data refresh (`v0.7.4`'s own concurrency
  improvement) - a real, live-identified suspect for why the Wiadomości
  session specifically started dying more often than before: bundling a
  separate, more fragile session's bootstrap into that same ~15-request
  burst is cheap to avoid and removes a plausible contributing factor,
  even without certainty it was the sole cause.

## 0.7.5

Promoted from the `0.7.5-beta.1`-`0.7.5-beta.4` testing line - confirmed
live by the reporting user on both a full-access account (zero
regression) and the actual limited/restricted account the fix targets
(setup succeeds, exactly the right data comes through, everything else
degrades cleanly instead of failing).

### Fixed
- **A limited/restricted-access account (e.g. a preschool child's login,
  which only has the Wiadomości module enabled) could not be set up at
  all.** ([issue #5](https://github.com/MichalZaniewicz/ha-librus-synergia/issues/5))
  `Attendances/Types` returning a CONFIRMED 403 for such an account (the
  attendance module genuinely doesn't apply, not a session problem - the
  reporter verified independently against the real Synergia web UI) used
  to fail the whole setup, exactly the same class of issue `Timetables`
  already got fixed for once (an unpublished class schedule, #4).
  Generalized that same "confirmed 403 = module unavailable, degrade
  gracefully" treatment to the whole core data tier (Grades, Notes,
  Attendances, Attendances/Types, HomeWorks, SchoolNotices) instead of
  just Timetables - a genuine 401 anywhere still triggers the normal
  forced-relogin-and-retry-once recovery, unchanged.

### Added
- **"Download diagnostics" now shows whether the last update actually
  succeeded, exactly which endpoints are currently degraded and since
  when (`degraded_endpoints` - covers every degradable fetch: the core
  tier, the supplementary tier, reference data, and Timetable/
  LuckyNumbers/Messages), any open repair issues, the integration's own
  version, the options-flow feature toggles, and session/reference-data
  freshness.** Built live while diagnosing issue #5's account - a
  toggled-off feature is never mistaken for a degraded/broken endpoint,
  and a confirmed-403-degraded endpoint is never confused with a
  genuinely broken one or an account that simply has no data for it (all
  three used to look identical - an empty field - in a bare data dump).

## 0.7.4

### Fixed
- **The actual semester/year grade (once Librus posts it, distinct from
  the already-excluded proposed one) would have been silently folded into
  the weighted average alongside the day-to-day grades it summarizes.**
  Found by reading the reference parser (`IsConstituent`, `IsSemester`,
  `IsFinal` alongside the already-handled `IsSemesterProposition`/
  `IsFinalProposition`) after a live investigation into an unrelated raw
  API field - not yet observable on any account until a semester actually
  ends, but confirmed via the reference source's own field names and
  fixed ahead of time.
- **A dead session could go undetected on a non-JSON 401/403 response.**
  `_async_request_url` used to JSON-parse the body before checking for a
  401/403 status - a plain-text/HTML error body raised the wrong exception
  first, silently skipping both the forced-relogin-and-retry-once recovery
  and the "timetable not published yet" (403) detection.
- **One failing reference-data endpoint could wipe out every other one in
  the same refresh.** Subjects/teachers/classrooms/school/class/homework
  categories/free days/note categories/behaviour-grade categories are now
  fetched independently, the same all-or-nothing `asyncio.gather()` class
  of bug already fixed once for the core data fetch.
- A grade category's `Weight: 0` was silently coerced to `1`; an explicit
  `CountToTheAverage: null` was silently coerced to "doesn't count" instead
  of defaulting to "counts".
- `Attendances[].Type.Id` is now defensively coerced the same way the
  sibling `Id` field already is, instead of assuming it's always numeric.
- The Agenda calendar now uses proper date-range overlap (not single-point
  containment) when filtering events, matching the Free Days calendar -
  currently unobservable (every Agenda event is single-day today) but
  fixed properly ahead of a future multi-day event.

### Changed
- The lucky number, reference-data and messages fetches now run
  concurrently each cycle instead of one after another.
- Next/Current lesson sensors expose a new `has_parallel_group` attribute
  when the picked period slot holds more than one lesson (split subject
  subgroups) - Librus doesn't expose which group a student is actually in,
  so this makes the ambiguity visible instead of silently guessing.
- Internal cleanup: the config flow's login-error-to-error-code mapping and
  the message sender-name resolution (both previously duplicated across
  3 and 2 call sites respectively) are now single shared helpers.

## 0.7.3

### Fixed
- **Timetable/agenda broke (HTTP 500 / "Coś poszło nie tak") right after the
  session was renewed, until a manual refresh.** Saving the fresh session
  cookies into the config entry after a re-login fired the entry's update
  listener, which reloaded the whole integration - closing its aiohttp
  session while the request that had just triggered the re-login was about
  to be retried on it (`RuntimeError: Session is closed`). Since 0.7.1
  actually releases the session on unload (before that, the close was a
  silent no-op, which masked this), it hit about once a day, when Librus
  expired the session. The entry now reloads only when its *options*
  change; reauth/reconfigure still reload themselves.
- **"Download diagnostics" returned HTTP 500.** The coordinator dump kept
  `date`/`int` dictionary keys, which Home Assistant's JSON encoder rejects;
  keys are now stringified.

## 0.7.2

### Added
- **`librus_synergia.get_grades` service** - every grade for a student in
  one response (subject, value, category, date, semester, comments),
  optionally filtered to one `subject_id`. Reads straight from
  already-fetched data (no extra Librus request), unlike `get_message` -
  the single-call equivalent of reading each subject average sensor's own
  `grades` attribute separately.
- **`get_message` now also returns `attachments`** (a list of `id`/
  `filename`) - confirmed live that the modern JSON API does expose the
  real filename, even though the file itself still can't be downloaded
  through this integration (see README's "Known limitations").
- **Homework Due Tomorrow blueprint** - at a set time each day, runs your
  action with a ready-made summary only when a real Homework assignment
  ("zadanie domowe", not the general agenda/test feed) is due the next
  calendar day - silent otherwise.

## 0.7.1

### Fixed
- **A school not having published the class's timetable yet made the whole
  integration fail with a reauth prompt** ([issue #4](https://github.com/MichalZaniewicz/ha-librus-synergia/issues/4))
  - reported live: login succeeded fine, but Librus returns HTTP 403 on
  `Timetables` specifically when a class's schedule genuinely hasn't been
  published (Synergia's own web UI shows an explicit "not published"
  message for this exact case) - a real, permanent-until-the-school-acts
  condition that a fresh re-login can never fix. This was previously
  indistinguishable from a genuinely dead session (also a 401/403), so it
  forced a pointless relogin-and-retry that only ended in an incorrect
  `ConfigEntryAuthFailed`. `LibrusSessionExpiredError` now carries the real
  HTTP status code, and a 403 on `Timetables` specifically degrades to an
  empty timetable (both in the normal poll cycle and the calendar's
  on-demand week fetch) instead of ever triggering reauth. A genuine 401
  still recovers via the existing forced-relogin-and-retry-once path.
- **The integration logged a "closes the Home Assistant aiohttp session"
  deprecation report on every login/reload** (same issue #4 report) -
  `.close()` on a session from `async_create_clientsession` is silently
  replaced by Home Assistant's own frame helper with a no-op that only
  logs that warning; it never actually released anything. Switched both
  call sites (`LibrusApiClient.async_close`, the config flow's one-off
  login-validation session) to `.detach()` - the same mechanism Home
  Assistant's own internal auto-cleanup uses for exactly this case, and
  not neutered the way `.close()` is.
- **Notification blueprints gave no way to tell which child an event was
  about in a multi-child household** ([issue #3](https://github.com/MichalZaniewicz/ha-librus-synergia/issues/3))
  - one blueprint instance's action runs for every config entry (student)
  that fires the event, but no event carried the student's own name.
  Every event this integration fires (`new_grade`/`new_note`/
  `new_announcement`/`new_message`/`new_homework`/`timetable_changed`/
  `new_absence`/`achievement_unlocked`) now includes a `student` field,
  and all 9 event-triggered blueprints prefix their ready-made summary
  text with it automatically.

## 0.7.0

### Added
- **Optional "student's number in the class register" option**
  (`CONF_STUDENT_NUMBER`, options flow) - Librus's API does not expose this
  anywhere at all (confirmed via szkolny-android's own reference source;
  even that app just asks the user to type it in once), so it's a fact the
  user enters by hand, not fetched data. When set, the Lucky number sensor
  gains `student_number`/`is_yours` attributes so a card can highlight
  whenever today's (or the next published) lucky number is the student's
  own. Left unset, `is_yours` stays `None` rather than falsely reporting
  `False`.

## 0.6.2

### Fixed
- **The integration failed to load at all (`setup_retry`, no entities/data)
  on any account with at least one non-numeric attendance record id.**
  `Attendances[].Id` was assumed to always be a plain numeric value and
  unconditionally `int()`-converted; a real record with a `t`-prefixed id
  (e.g. `"t41685"`) raised `ValueError` and crashed the whole coordinator
  update every cycle, immediately after upgrading. Now kept as a string
  when it isn't cleanly convertible to an int, instead of assuming every
  Attendances id has the same shape.

## 0.6.1

Promoted from the `0.6.1-beta.1`/`0.6.1-beta.2` testing line, with one more
small addition on top (the "quiz" keyword below) - both fixes were
confirmed live by the reporting user before this promotion.

### Fixed
- **Multi-child households (2+ config entries) could intermittently show
  one child's data on another child's sensors** ([issue #2](https://github.com/MichalZaniewicz/ha-librus-synergia/issues/2))
  - reported live by a user running 3 config entries, one per student.
  Root cause: every entry's `LibrusApiClient` was built on Home
  Assistant's shared, hass-wide `async_get_clientsession(hass)`. This
  client's auth lives entirely in the session's cookie jar, and aiohttp's
  cookie jar is keyed only by domain (`synergia.librus.pl`) - not per
  account. With multiple entries sharing one jar, whichever entry logged
  in or re-imported its cookies most recently silently "won" that one
  `oauth_token` slot for every OTHER entry's next request too, however
  briefly, until the next login overwrote it again - and independent
  per-entry polling cycles racing on the event loop reproduced this
  exactly as reported (data crossing over between siblings, "fixed" only
  until the next background refresh from any entry re-triggered the race
  - matching why reloading the affected entry only ever helped
  temporarily). Fixed: each config entry now gets its own dedicated
  session (`async_create_clientsession`), closed on unload; the config
  flow's own one-off login-validation session was switched the same way,
  for the same reason.
- **Next exam sensor missed "kartkówka" entries filed under the generic
  "Inne" (Other) Agenda category** ([issue #1](https://github.com/MichalZaniewicz/ha-librus-synergia/issues/1))
  - some teachers only name the assessment type in the free-text
  description, not the category. `sensor.*_next_exam` now also matches the
  description against the same keyword list when the category itself
  doesn't match, instead of requiring the category alone to say
  "sprawdzian"/"kartkówka"/etc. Also added "quiz" to that keyword list, on
  the reporting user's follow-up request - another word some teachers use
  for a short/informal test.

## 0.6.0

### Added
- **Repair issues** - two new, deliberately conservative repairs: a fixable
  "school year end date looks out of date" issue (cached `end_school_year`
  more than a month in the past - normally self-heals within 24h, but a
  one-click fix reloads the entry to force an immediate re-check), and an
  informational "\<endpoint\> has not responded in over a week" issue for a
  supplementary endpoint (BehaviourGrades, DescriptiveGrades, etc.) that
  has failed on every attempt for 7 straight days - a single hiccup never
  raises it. Both clear themselves automatically on recovery, and are
  cleaned up for good if the config entry is deleted.
- **Four more options-flow toggles**, matching the existing "Fetch private
  messages" one: announcements, behaviour grade, descriptive grades, and
  the free days calendar can each be turned off individually. A disabled
  feature's sensor goes `unavailable` (the free days calendar entity isn't
  created at all) and its endpoint is never called - fewer requests to
  Librus for families that don't use a given module.
- **Subject teachers directory** - the School sensor gained a
  `subject_teachers` attribute (subject name -> sorted list of teacher
  names), derived client-side from the timetable already being fetched
  every cycle - no extra API calls. A subject taught by more than one
  teacher (parallel/split groups) lists all of them. Previously the
  homeroom teacher (Class sensor) was the only teacher surfaced anywhere.
- **Weekly Digest blueprint** - every Sunday at a set time, builds a
  `{{ digest_text }}` summary (tests due in the next 7 days from the Next
  exam sensor's own `upcoming` list, optionally homework due this week and
  any still-unexcused absence count) and runs your action.
- **Reconfigure support** - fix a typo'd login or an intentionally-changed
  password from the entry's own "Reconfigure" menu item, without deleting
  and re-adding the whole integration (which would lose entity ids,
  dashboard references and automations built on them). Refuses to
  silently repoint an entry at a genuinely different Librus account.
- **Quiet hours** - an off-by-default options-flow toggle + start/end time
  (wraps midnight; default 23:00 -> 06:00). While enabled and inside the
  window, the coordinator skips the network round-trip entirely and
  returns its last-known data unchanged - entities keep their last state,
  nothing goes stale/unavailable, and Librus gets no requests overnight.
- **Time to Leave blueprint** - fires once, timed from the Next lesson
  sensor's own `start` time minus a travel time you set, re-checked every
  minute against the real clock so the timing is accurate regardless of
  your poll interval.
- **Gamification** - three new "passa" (streak) sensors (days without an
  absence, days without a negative behaviour note, consecutive good
  grades in a row) and a cosmetic Bronze/Silver/Gold/Diamond Rank sensor
  derived from your overall average. Plus a new
  `librus_synergia_achievement_unlocked` event and matching Achievement
  Unlocked blueprint, firing at most once each for a handful of objective
  milestones (first six, grade/attendance/behaviour streaks reaching
  5/10/20 grades or 7/30/90 days) - deliberately not an invented points
  system, every number here is a plain fact derived from data already
  being fetched.

## 0.5.1

Small fix + a blueprint, both from live use.

### Fixed
- **Message bodies rendered Librus's link-converter tag soup** - Librus
  rewrites every link in a message into
  `<a href="https://liblink.pl/..." title="Link został skonwertowany...">`,
  and the list endpoint's `content` can carry other light HTML (`<br>`,
  `<p>`). The Wiadomości card showed it verbatim. `decode_message_content`
  now flattens it: keeps the link (its visible text, or the bare URL),
  turns `<br>`/`</p>` into newlines, drops the rest, unescapes entities.

### Added
- **Subject Average Dropped blueprint** - `numeric_state` trigger on one
  or more subject-average sensors crossing below a threshold you set
  (default 3.5). Fires once on the way down, again only after the average
  recovers above the threshold. Optional "stays below for" guard.

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

Two additions:

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
