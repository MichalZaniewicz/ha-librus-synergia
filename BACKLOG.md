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
  weeks-ahead and a quiet-hours polling window are still open, if wanted.
- **`Units` data** — surface the bell schedule (`LessonsRange`), the more
  specific school-unit name ("Szkoła Podstawowa 32" vs the broad "Zespół
  Szkolno-Przedszkolny nr 21"), grade-system flags. Client method
  `async_get_units` exists but isn't wired to the coordinator. Needs the
  reference parser (`szkolny-android`) for exact field names — modest
  payoff (bell schedule is already derived from the timetable).
- **More response services** — `get_timetable`, `get_grades` (so a card
  can pull data without it sitting in an attribute).
- ~~**Weekly Sunday digest blueprint**~~ — done (Unreleased): built off
  already-fetched sensor attributes (Next exam's `upcoming` list,
  Homework assignments' `recent`, Unexcused absences) rather than a
  `calendar.get_events` service-call template — sidesteps the templating
  complexity originally blocking this.
- **More blueprints** — "lucky number == the child's roll number" (needs
  the child's own number, not exposed).
- **Message coverage** — other mailboxes (`notes` / `absences` / `trash`)
  get unread *counts* only, not content; sent messages aren't fetched;
  `librus_synergia.download_attachment` service (`LibrusMessagesGetAttachment`
  is a real endpoint) not built; a `mark_message_read` service (would just
  wrap `get_message`, low value).
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
- **`manual_smoke_test.py`** — extend to the newer endpoints (behaviour
  grades, justifications, `next_exam`-relevant).
