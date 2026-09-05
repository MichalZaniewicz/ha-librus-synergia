# Changelog

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
