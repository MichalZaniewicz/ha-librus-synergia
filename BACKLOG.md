# Backlog — ha-librus-synergia

Ideas raised but deliberately not built yet. Grouped by *why*. Not a
roadmap; pick from here when it makes sense.

## Known, narrow edge cases - not urgent

- **New-item tracking can seed wrong if the very first refresh partly
  fails.** New grades, notes, announcements, messages, agenda entries,
  absences and timetable changes are diffed by `librus_synergia.changes.
  ChangeTracker`; homework assignments, school trips, documents, text
  grades and achievements by `_fire_for_new_ids`. Both record whatever the
  entry's first-ever refresh returned as "already seen". If that one
  refresh hit a transient failure on some data type, it is recorded as
  "seen, empty", and once the data type recovers every item in it fires as
  new at once. Since 0.12.0 the seen state is saved across restarts, so
  this only concerns the entry's first-ever refresh. A real fix needs the
  callers to tell "confidently empty" from "degraded".

## Waiting for real data

- **Semester and year grades, proposed and final** — the flags are parsed
  and those grades are kept out of averages, but none has been issued yet
  (the first semester ends 2027-01-31). A sensor for them is planned for
  January.
- **Grade corrections ("poprawy")** — `Improvement.Id` comes from another
  client's reading of the API; no correction has appeared live yet.
- **`Grades/Comments` item shape** — ids into `Grades/Comments` are
  confirmed with real comments; whether each item is a bare id or an
  `{"Id": ...}` object wasn't captured. librus-synergia's
  `resolve_comment_ids` handles both.
- **Point grades** — sensor and attributes are in place (0.12.0), but the
  field names come from another client; no point grade has been seen on a
  real account (the test school has them off).
- **Behaviour grade on a points scale** — the classic scale is confirmed
  live (`BehaviourGrade.Id`); the points variant hasn't been seen.
- **Descriptive grades** — the sensor is in place; the endpoint has been
  empty on every check.
- **A rejected absence justification** — handled, but only accepted ones
  have been seen live.
- **Limited account types (e.g. preschool)** — issue #5: a preschool
  login has only Wiadomości, `Attendances/Types` answers 403. Since 0.7.5
  a confirmed 403 on any core endpoint degrades to empty instead of
  failing; kindergarten timetables are read since 0.7.8. Still unknown
  which other core endpoints such accounts can read.
- **Whether `HomeWorks` accepts a date-range query** — never tried;
  `calendar.py` filters client-side, so it's not blocking.
- **Substitutions / TeacherFreeDays** — both 403 for a parent/student
  login. Would need a teacher/staff account.

## Buildable now, just not done

- **`VirtualClasses`** — the client can fetch them, but nothing references
  a virtual-class id. Lessons themselves carry a virtual-class name/URL in
  `Timetables` (`VirtualClass`, `SubstitutionClassUrl`), which could be
  surfaced on the lesson sensors and the Timetable calendar instead.
- **`Units` data** — fetched daily, but only
  `GradesSettings.PointGradesEnabled` is used. The bell schedule
  (`LessonsRange`) and the school unit's own name aren't surfaced (the bell
  schedule is already derived from the timetable).
- **Message coverage** — the `notes` / `absences` / `trash` mailboxes get
  unread counts only; sent messages and the archive aren't fetched; no
  `mark_message_read` service.
- **Assist tools** — the seven tools don't cover lesson topics, school
  trips, documents, text/point/descriptive grades or justifications yet.
- **Calendar weeks ahead option** — the timetable calendar fetches other
  weeks on demand; a configurable look-ahead for the Agenda isn't there.

## Done (kept for context)

- Options toggles (announcements, behaviour grade, descriptive grades,
  free-days calendar), quiet hours, Reconfigure flow, "Time to leave"
  blueprint, `get_grades` service (`get_timetable` deliberately not built -
  `calendar.get_events` covers it), weekly digest blueprint, teacher
  directory (`subject_teachers` on the School sensor), `repairs.py`,
  `scripts/manual_smoke_test.py`.
- **Lucky number is yours** — the class register number turned out to be
  in the JSON API after all (`Users/{Me.Account.UserId}.ClassRegisterNumber`,
  with Synergia's `informacja` page as a fallback); the Lucky number sensor
  has `is_yours` and there's a blueprint for it.
- **Grade `+`/`-` modifiers** — +0.5 / -0.25, both confirmed against the
  Librus app's own averages.
- **Message attachment download** — done in 0.12.0 through a logged-in
  HTTP view (`attachment_view.py`); nothing is saved in Home Assistant and
  the message isn't marked read.
- **`Notes[].Positive`** — 0 negative, 1 positive, otherwise neutral.
- **`Grades[].IsConstituent`** — a classification flag, not "counts towards
  the average"; the actual semester/year grades (`IsSemester`/`IsFinal`)
  are tracked since 0.7.4.
