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
- **Grade `+`/`-` modifier value** — `sensor._parse_grade_value` adds
  `+0.5` / `-0.25`; that convention is a third-party inference, never
  observed. Verify against `Grades/Types`' numeric entries once a real
  numeric grade lands.
- **`Notes[].Positive`** — resolved (0=neg, 1=pos, else neutral, via the
  reference parser) — keep as-is unless a real note contradicts it.
- **Confirmed-real-but-empty endpoints**, client methods exist, not wired
  to any entity: `PointGrades` (disabled for this school), `TextGrades`
  (enablement unknown), `VirtualClasses` (nothing references a
  virtual-class id), `DescriptiveGrades` (enabled here, still empty —
  sensor exists, will populate when data does), `HomeWorkAssignments`
  (sensor exists, empty), `ParentTeacherConferences` (merged into the
  Agenda calendar defensively).
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
- **`librus_synergia.download_attachment` service** — checked again
  (2026-09-17): `LibrusMessagesGetAttachment.kt` (szkolny-android) is
  NOT a usable reference for this after all - it targets Librus's older
  XML "sandbox" messages protocol (`messagesGet`/`doc.select(...)`, Jsoup
  doc parsing), not the JSON `wiadomosci.librus.pl/api/...` REST API this
  integration actually uses (same trap as `LibrusMessagesGetMessage.kt`/
  `LibrusSynergiaGetMessage.kt` hit before, back when `get_message` was
  being built). The modern API's real attachment-download endpoint shape
  is genuinely unknown - guessing a URL pattern risks another dead end.
  Two real messages on the live account currently have `has_attachment:
  true` (Siwik Anna, Włodarczyk Małgorzata, both 2026-09-14), so a
  one-time live probe (same consent-first pattern used for `get_message`
  back in 2026-09-06) would settle this quickly whenever the user wants
  to do it.
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
