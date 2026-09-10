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
- **Fuller options-flow toggles** — announcements / behaviour / descriptive
  grades / free-days calendar on-off, calendar weeks-ahead, a quiet-hours
  polling window. `messages_enabled` is the only one so far. The invasive
  bit is gating entity creation vs. just skipping the fetch — a
  multi-select "features" option where an off group makes its sensor
  `unavailable` (like `messages` already does) is the safe shape.
- **`Units` data** — surface the bell schedule (`LessonsRange`), the more
  specific school-unit name ("Szkoła Podstawowa 32" vs the broad "Zespół
  Szkolno-Przedszkolny nr 21"), grade-system flags. Client method
  `async_get_units` exists but isn't wired to the coordinator. Needs the
  reference parser (`szkolny-android`) for exact field names — modest
  payoff (bell schedule is already derived from the timetable).
- **More response services** — `get_timetable`, `get_grades` (so a card
  can pull data without it sitting in an attribute).
- **More blueprints** — "subject average dropped below X" (numeric_state
  trigger on a `subject_average` sensor), weekly Sunday digest (needs
  calendar templating), "lucky number == the child's roll number" (needs
  the child's own number, not exposed).
- **Message coverage** — other mailboxes (`notes` / `absences` / `trash`)
  get unread *counts* only, not content; sent messages aren't fetched;
  `librus_synergia.download_attachment` service (`LibrusMessagesGetAttachment`
  is a real endpoint) not built; a `mark_message_read` service (would just
  wrap `get_message`, low value).
- **Teacher directory** — only the homeroom teacher is surfaced anywhere;
  subject teachers aren't in any attribute.
- **`repairs.py`** — a repair issue for e.g. "an endpoint has 403'd for N
  days" or "school year rolled over, re-check semester dates". Dropped so
  far.
- **`manual_smoke_test.py`** — extend to the newer endpoints (behaviour
  grades, justifications, `next_exam`-relevant).
