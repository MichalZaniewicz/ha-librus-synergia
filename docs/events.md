# Events

Librus Synergia fires Home Assistant bus events for new and changed items,
for building your own automations. The ready-made
[blueprints](../README.md#automation-blueprints) wrap most of them, and each
kind also has an event entity (*New grade*, *New absence*, ...) you can pick
in the automation editor without knowing the event name.

## Common rules

- **Every event** carries `entry_id` and `student` (the child's name), so one
  automation can serve several children.
- **Nothing fires on the very first sync** after setup: that run only records
  what's already there. What has been announced is saved, so something that
  arrives while Home Assistant is off still fires once it's back.
- Subject, teacher and category ids come with their resolved names
  (`subject_id` + `subject`, ...), so an automation doesn't need its own
  lookup.
- `id` is Librus's id of the item (a string for some kinds).

## New items

| Event | Fires for | Data |
|---|---|---|
| `librus_synergia_new_grade` | A new grade, including a text grade | `subject_id`, `subject`, `value`, `teacher`, `category`, `weight`, `counts_to_average`, `comments` (list), `date`, `semester`, `improves` (the value of the grade a correction replaces), `kind` (`normal` / `semester_proposition` / `semester` / `final_proposition` / `final` / `text`) |
| `librus_synergia_new_note` | A new behaviour note | `positive`, `sentiment` (`positive` / `negative` / `neutral`), `teacher`, `text` |
| `librus_synergia_new_absence` | A new real absence (not "present"/"late") | `date`, `type`, `excused`, `lesson_no` |
| `librus_synergia_new_announcement` | A new notice-board announcement | `subject` |
| `librus_synergia_new_message` | A new Wiadomości message in the inbox | `sender`, `topic` |
| `librus_synergia_new_homework` | A new Agenda entry (test, trip, meeting, ...) | `subject_id`, `subject`, `category`, `date`, `content` (first 200 characters) |
| `librus_synergia_new_homework_assignment` | A new real homework assignment ("zadanie domowe") | `topic`, `text` (first 500 characters), `date`, `due_date`, `teacher`, `subject_id`, `subject` (worked out from the teacher; empty when that teacher teaches more than one subject) |
| `librus_synergia_new_school_trip` | A new school trip | `destination`, `route`, `transport`, `date_from`, `date_to`, `coordinator` |
| `librus_synergia_new_school_document` | A new document the school shares with parents | `name`, `added`, `url` |

## Changes

| Event | Fires when | Data |
|---|---|---|
| `librus_synergia_timetable_changed` | A lesson today or later newly turns up cancelled or as a substitution | `date`, `lesson_no`, `kind` (`canceled` / `substitution`), `subject_id`, `subject`, `hour_from`, `teacher`, `change` (`canceled` / `substitution` / `room_change` / `moved`), `room_changed`, `classroom`, `original_classroom`, `original_subject`, `original_teacher`, `original_date`, `original_lesson_no` |
| `librus_synergia_agenda_changed` | An upcoming Agenda entry changes or disappears from Librus | `kind` (`changed` / `removed`), `date`, `time_from`, `content`, `category_id`, `category`, `subject_id`, `subject`; for `changed` also `changed_fields` and the old values in `previous` (e.g. a test moved to another day) |
| `librus_synergia_justification_status` | The school decides on an absence justification you sent | `status`, `previous_status`, `accepted`, `rejected`, `date_from`, `date_to`, `justified_absences`, `message`, `teachers` |
| `librus_synergia_forecast_changed` | A subject's forecast report-card grade moves up or down | `subject_id`, `subject`, `old`, `new`, `direction` (`up` / `down`), `average`, `sixes_to_next`, `ones_to_drop` |

## Other

| Event | Fires when | Data |
|---|---|---|
| `librus_synergia_achievement_unlocked` | A milestone is reached: the first 6, 5/10/20 good grades in a row, 7/30/90 days without an absence or without a negative note. Each fires once | `id` (e.g. `good_grade_streak_10`), `title` |
| `librus_synergia_weekly_summary` | The [weekly AI summary](../README.md#weekly-ai-summary) is ready | `manual`, `headline`, `status`, `sections`, `advice`, `warning`, `summary`, `week_from`, `week_to`, `audience`, `labels` |
