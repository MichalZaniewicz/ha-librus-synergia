# Backlog — ha-librus-synergia

Ideas raised but deliberately not built yet. Grouped by *why*. Not a
roadmap; pick from here when it makes sense.

## Waiting for real data
The test account's gradebook / comments / several endpoints are empty, so
these can't be built or verified against a real shape:

- **`Grades/Comments` correlation** — `coordinator._parse_grades` currently
  assumes each grade's `Comments` field holds embedded `{"Text": ...}`
  objects; the reference parser says it's actually a list of ids into the
  separate `Grades/Comments` endpoint. `_resolve_comment_ids` handles both
  defensively, but which is right is unconfirmed. Check the first time a
  real graded + commented item exists.
- **Grade `+`/`-` modifier value** — `coordinator.parse_grade_value` adds
  `+0.5` / `-0.25`; that convention was a third-party inference. **+0.5
  now CONFIRMED (2026-09-22)**: a raw probe of Librus's own `/Grades` API
  showed it carries no numeric value at all for a modified grade (only
  the string, e.g. `"Grade": "4+"`) - so the only real check was
  cross-referencing the real Librus app's own displayed average for that
  `4+` grade, which the account owner confirmed also shows 4.5. **-0.25
  still unverified** - no `-`-modified grade has appeared live yet.
- **`Grades[].IsConstituent`** — RESOLVED (2026-09-22) via the reference
  parser (`LibrusApiGrades.kt`): it's a grade-type CLASSIFICATION flag
  (checked first, in priority order, against `IsSemester`/
  `IsSemesterProposition`/`IsFinal`/`IsFinalProposition` - `true` means
  "ordinary day-to-day grade"), NOT a "counts toward the average" switch
  (that's the pre-existing `CountToTheAverage`/weight-0-for-`bz`/`np`/`+`/
  `-` logic, unrelated). Not itself wired in (no reason to - our own
  `is_semester_proposition`/`is_final_proposition`/`is_semester`/
  `is_final` flags already classify a grade correctly without it), but
  reading this file is what surfaced the REAL bug: `IsSemester`/`IsFinal`
  (the actual, not proposed, semester/year grade) were never tracked at
  all - fixed same session, see CHANGELOG's `0.7.4` entry.
- **`Notes[].Positive`** — resolved (0=neg, 1=pos, else neutral, via the
  reference parser) — keep as-is unless a real note contradicts it.
- **Limited/restricted-access account types (e.g. preschool)** — issue #5
  confirmed a real one exists in the wild: a preschool child's login only
  has Wiadomości enabled, `Attendances/Types` 403s. Fixed (`0.7.5-beta.1`)
  by generalizing the confirmed-403-degrades-gracefully treatment from
  Timetables (issue #4) to the whole core tier. Not confirmed live against
  OUR OWN test account (which has full access) - only against the
  reporter's own description + their independent check of the real
  Synergia web UI. If the reporter confirms the beta works, worth asking
  whether OTHER core endpoints (Grades? Notes? HomeWorks?) also 403 for
  that account type, to know if this class of account can ever get a
  fuller picture than just Wiadomości + degraded-empty everything else.
- **Confirmed-real-but-empty endpoints**, client methods exist, not wired
  to any entity: `PointGrades` (disabled for this school), `TextGrades`
  (enablement unknown), `VirtualClasses` (nothing references a
  virtual-class id), `ParentTeacherConferences` (merged into the Agenda
  calendar defensively). `DescriptiveGrades`/`BehaviourGrades/Points`
  have real sensors but are still empty on the live account as of
  2026-09-22. `HomeWorkAssignments` is no longer in this bucket — it got
  real data (2026-09-17) and the sensor is confirmed parsing correctly.
- **Whether `HomeWorks` accepts a date-range query param** — never tried;
  `calendar.py` filters client-side so it's not blocking.
- **Substitutions / TeacherFreeDays** — both 403 for a parent/student
  login. Would need a teacher/staff account to develop.

## Buildable now, just not done
- ~~**Fuller options-flow toggles**~~ — done (Unreleased): announcements /
  behaviour grade / descriptive grades / free-days calendar can each be
  turned off individually now, same "sensor goes unavailable, endpoint
  never called" shape as the pre-existing `messages_enabled`. Calendar
  weeks-ahead is still open, if wanted.
- ~~**Quiet-hours polling window**~~ — done (Unreleased): an off-by-default
  toggle + start/end time (wraps midnight, default 23:00->06:00) - the
  coordinator skips the network round-trip entirely while inside the
  window and returns its last-known data unchanged, except on the very
  first refresh (nothing to fall back to yet).
- ~~**Reconfigure flow**~~ — done (Unreleased): the entry's own
  "Reconfigure" menu item now lets you fix a typo'd login or an
  intentionally-changed password without deleting and re-adding the
  entry. `_abort_if_unique_id_mismatch` stops it from silently repointing
  an entry at a genuinely different Librus account.
- ~~**"Time to leave" blueprint**~~ — done (Unreleased): fires once, timed
  from the Next lesson sensor's own `start` minus a configurable travel
  time, re-checked every minute against the real clock (not just once per
  Librus poll cycle) for accurate timing regardless of poll interval.
- **`Units` data** — surface the bell schedule (`LessonsRange`), the more
  specific school-unit name ("Szkoła Podstawowa 32" vs the broad "Zespół
  Szkolno-Przedszkolny nr 21"), grade-system flags. Client method
  `async_get_units` exists but isn't wired to the coordinator. Needs the
  reference parser (`szkolny-android`) for exact field names — modest
  payoff (bell schedule is already derived from the timetable).
- ~~**`get_grades` response service**~~ — done (Unreleased): every grade
  across every subject in one call, optional `subject_id` filter - reads
  straight from already-fetched coordinator data, no extra Librus request.
  **`get_timetable` deliberately NOT built** - HA's own built-in
  `calendar.get_events` service against `calendar.*_plan_lekcji` already
  covers this (same on-demand out-of-range week fetch + session-recovery
  logic `LibrusTimetableCalendar` uses), so a dedicated service would just
  be a thinner duplicate with different field names, not a real gap.
- ~~**Weekly Sunday digest blueprint**~~ — done (Unreleased): built off
  already-fetched sensor attributes (Next exam's `upcoming` list,
  Homework assignments' `recent`, Unexcused absences) rather than a
  `calendar.get_events` service-call template — sidesteps the templating
  complexity originally blocking this.
- **"Lucky number == the child's roll number" blueprint** — investigated,
  genuinely NOT just "data not exposed yet": szkolny-android's own
  `LibrusFeatures.kt` lists `STUDENT_NUMBER` as available only via
  `ENDPOINT_LIBRUS_SYNERGIA_INFO` / `LoginMethod.LIBRUS_SYNERGIA` - the
  OLD HTML-scraping mechanism against the classic synergia.librus.pl
  portal, not the modern JSON REST API this whole integration is built on
  (and deliberately moved TO, away from fragile scraping - see CLAUDE.md's
  own "History / why it looks like this"). `Me`/`Users`/`Classes`'
  reference parsers confirm no roll number anywhere in the JSON API. Not
  worth building a second, fragile HTML-scraping subsystem for one minor
  notification idea - staying blocked is the right call here, not a gap
  to close later.
- **Message coverage** — other mailboxes (`notes` / `absences` / `trash`)
  get unread *counts* only, not content; sent messages aren't fetched;
  a `mark_message_read` service (would just wrap `get_message`, low value).
- **`librus_synergia.download_attachment` service** — investigated live
  (2026-09-17, one-time consented probe, same pattern as `get_message`
  back in 2026-09-06). **Genuinely blocked, not just unverified.**
  `client.async_get_message("inbox", "428360")` against a real message
  confirmed the modern JSON API's `attachments` field shape:
  `[{"filename": "...", "id": "7982536"}]` - so at least THAT part is now
  known. But two reasoned guesses at a REST download URL under
  `wiadomosci.librus.pl/api/...` (`.../messages/{id}/attachments/{id}`,
  `/api/attachments/{id}`) both 404'd with an empty-route JSON body
  (`[]`), meaning those routes don't exist in this API's router at all.
  Reading szkolny-android's reference classes all the way down (not just
  `LibrusMessagesGetAttachment.kt`, but its base `LibrusMessages.kt` and
  `LibrusSandboxDownloadAttachment.kt`) confirmed why: attachment download
  in that reference app goes through a COMPLETELY SEPARATE legacy XML
  protocol (`sandboxGet`/`sandboxGetFile` against a `LIBRUS_SANDBOX_URL`,
  a `singleUseKey` polling handshake, `CSCheckKey`/`CSDownload` actions)
  authenticated with its own `messagesSessionId`/`DZIENNIKSID` cookie -
  NOT the `oauth_token` this integration's whole session model is built
  on. Building this for real would mean standing up a second, fully
  separate auth/session subsystem just for file downloads - a large,
  disproportionate undertaking for one feature. Not pursuing further
  without a stronger signal (e.g. capturing the real modern web app's own
  network requests some other way) that a same-session JSON download path
  actually exists.
- ~~**Teacher directory**~~ — done (Unreleased): `sensor.*_school` gained a
  `subject_teachers` attribute (subject name -> sorted teacher name list),
  derived client-side from the already-fetched timetable, same approach as
  `bell_schedule`. Previously only the homeroom teacher (Class sensor) was
  surfaced anywhere.
- ~~**`repairs.py`**~~ — done (Unreleased): a fixable "school year end date
  looks out of date" issue (cached `end_school_year` >30 days in the past;
  the fix just reloads the entry) and an informational "\<endpoint\> has
  not responded in over a week" issue for a supplementary endpoint failing
  on every attempt for 7 straight days.
- ~~**`manual_smoke_test.py`**~~ — done (Unreleased): behaviour grades/
  next_exam-relevant (`HomeWorks/Categories`) endpoints turned out to
  already be probed - the real gap was the secondary message mailboxes
  (substitutions/alerts/justifications content, not just the unread
  count), plus next week's timetable. All added, mirroring exactly what
  `coordinator.py` itself fetches every cycle.
