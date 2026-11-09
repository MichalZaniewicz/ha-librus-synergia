"""Readable logbook entries for the integration's bus events.

"Ola: new grade 5 - Matematyka (Sprawdzian)" in the logbook and on the
student's device page (the logbook matches events to a device by the
`device_id` in their data), instead of nothing. In Home Assistant's
language (English, Polish, Ukrainian, Portuguese, Russian, German, French;
English for any other) -
the logbook has no translation step for these lines.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ICON,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant, callback

from .const import (
    DOMAIN,
    EVENT_ACHIEVEMENT_UNLOCKED,
    EVENT_AGENDA_CHANGED,
    EVENT_FORECAST_CHANGED,
    EVENT_GRADE_CHANGED,
    EVENT_JUSTIFICATION_STATUS,
    EVENT_LIBRARY_CHANGED,
    EVENT_MESSAGE_READ,
    EVENT_NEW_ABSENCE,
    EVENT_NEW_ANNOUNCEMENT,
    EVENT_NEW_GRADE,
    EVENT_NEW_HOMEWORK,
    EVENT_NEW_HOMEWORK_ASSIGNMENT,
    EVENT_NEW_MESSAGE,
    EVENT_NEW_NOTE,
    EVENT_NEW_SCHOOL_DOCUMENT,
    EVENT_NEW_SCHOOL_TRIP,
    EVENT_TIMETABLE_CHANGED,
    EVENT_WEEKLY_SUMMARY,
)

_MAX_TEXT = 80


def _short(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1].rstrip() + "…"


# Words of the logbook lines, per language (Home Assistant's language; any
# other falls back to English). Librus data (subjects, categories, texts)
# stays as the school wrote it.
_WORDS: dict[str, dict[str, str]] = {
    "en": {
        "behaviour": "behaviour grade", "new_grade": "new grade",
        "grade_removed": "grade removed", "grade_changed": "grade changed",
        "note_positive": "positive note", "note_negative": "negative note", "note": "note",
        "absence_excused": "excused absence", "absence_unexcused": "unexcused absence",
        "lesson": "lesson",
        "message_from": "new message from {who}", "message": "new message",
        "announcement": "new announcement", "agenda": "new Agenda entry",
        "homework": "new homework", "due": "due",
        "canceled": "lesson cancelled", "substitution": "substitution",
        "room_change": "room change", "moved": "lesson moved",
        "agenda_removed": "Agenda entry removed", "agenda_changed": "Agenda entry changed",
        "justification_accepted": "justification accepted",
        "justification_rejected": "justification rejected",
        "justification_status": "justification status changed",
        "forecast": "forecast", "trip": "new school trip", "document": "new school document",
        "library_returned": "returned to the library", "library_lent": "borrowed from the library",
        "read_by": "{who} read your message", "read": "your message was read",
        "badge": "badge earned", "summary": "weekly summary ready",
        "for_parent": " (for the parent)", "for_student": " (for the student)",
    },
    "pl": {
        "behaviour": "ocena z zachowania", "new_grade": "nowa ocena",
        "grade_removed": "usunięta ocena", "grade_changed": "zmieniona ocena",
        "note_positive": "uwaga pozytywna", "note_negative": "uwaga negatywna", "note": "uwaga",
        "absence_excused": "nieobecność usprawiedliwiona",
        "absence_unexcused": "nieobecność nieusprawiedliwiona",
        "lesson": "lekcja",
        "message_from": "nowa wiadomość od {who}", "message": "nowa wiadomość",
        "announcement": "nowe ogłoszenie", "agenda": "nowy wpis w terminarzu",
        "homework": "nowe zadanie domowe", "due": "na",
        "canceled": "odwołana lekcja", "substitution": "zastępstwo",
        "room_change": "zmiana sali", "moved": "przeniesiona lekcja",
        "agenda_removed": "usunięty wpis w terminarzu", "agenda_changed": "zmieniony wpis w terminarzu",
        "justification_accepted": "usprawiedliwienie przyjęte",
        "justification_rejected": "usprawiedliwienie odrzucone",
        "justification_status": "usprawiedliwienie: nowy status",
        "forecast": "prognoza", "trip": "nowa wycieczka", "document": "nowy dokument szkoły",
        "library_returned": "oddana do biblioteki", "library_lent": "wypożyczona z biblioteki",
        "read_by": "wiadomość przeczytana przez {who}", "read": "wiadomość przeczytana",
        "badge": "zdobyta odznaka", "summary": "podsumowanie tygodnia gotowe",
        "for_parent": " (dla rodzica)", "for_student": " (dla ucznia)",
    },
    "uk": {
        "behaviour": "оцінка за поведінку", "new_grade": "нова оцінка",
        "grade_removed": "оцінку видалено", "grade_changed": "оцінку змінено",
        "note_positive": "позитивне зауваження", "note_negative": "негативне зауваження",
        "note": "зауваження",
        "absence_excused": "відсутність з поважною причиною",
        "absence_unexcused": "відсутність без поважної причини",
        "lesson": "урок",
        "message_from": "нове повідомлення від {who}", "message": "нове повідомлення",
        "announcement": "нове оголошення", "agenda": "новий запис у розкладі подій",
        "homework": "нове домашнє завдання", "due": "до",
        "canceled": "урок скасовано", "substitution": "заміна",
        "room_change": "зміна кабінету", "moved": "урок перенесено",
        "agenda_removed": "запис у розкладі подій видалено",
        "agenda_changed": "запис у розкладі подій змінено",
        "justification_accepted": "пояснення прийнято",
        "justification_rejected": "пояснення відхилено",
        "justification_status": "пояснення: новий статус",
        "forecast": "прогноз", "trip": "нова екскурсія", "document": "новий документ школи",
        "library_returned": "повернено до бібліотеки", "library_lent": "взято в бібліотеці",
        "read_by": "{who} прочитав(-ла) ваше повідомлення", "read": "ваше повідомлення прочитано",
        "badge": "отримано значок", "summary": "підсумок тижня готовий",
        "for_parent": " (для батьків)", "for_student": " (для учня)",
    },
    "pt": {
        "behaviour": "nota de comportamento", "new_grade": "nova nota",
        "grade_removed": "nota removida", "grade_changed": "nota alterada",
        "note_positive": "observação positiva", "note_negative": "observação negativa",
        "note": "observação",
        "absence_excused": "falta justificada", "absence_unexcused": "falta injustificada",
        "lesson": "aula",
        "message_from": "nova mensagem de {who}", "message": "nova mensagem",
        "announcement": "novo anúncio", "agenda": "nova entrada na agenda",
        "homework": "novo trabalho de casa", "due": "para",
        "canceled": "aula cancelada", "substitution": "substituição",
        "room_change": "mudança de sala", "moved": "aula mudada de horário",
        "agenda_removed": "entrada da agenda removida", "agenda_changed": "entrada da agenda alterada",
        "justification_accepted": "justificação aceite",
        "justification_rejected": "justificação recusada",
        "justification_status": "justificação: novo estado",
        "forecast": "previsão", "trip": "nova visita de estudo", "document": "novo documento da escola",
        "library_returned": "devolvido à biblioteca", "library_lent": "emprestado pela biblioteca",
        "read_by": "{who} leu a sua mensagem", "read": "a sua mensagem foi lida",
        "badge": "distintivo conquistado", "summary": "resumo da semana pronto",
        "for_parent": " (para o encarregado de educação)", "for_student": " (para o aluno)",
    },
    "ru": {
        "behaviour": "оценка за поведение", "new_grade": "новая оценка",
        "grade_removed": "оценка удалена", "grade_changed": "оценка изменена",
        "note_positive": "положительное замечание", "note_negative": "отрицательное замечание",
        "note": "замечание",
        "absence_excused": "пропуск по уважительной причине",
        "absence_unexcused": "пропуск по неуважительной причине",
        "lesson": "урок",
        "message_from": "новое сообщение от {who}", "message": "новое сообщение",
        "announcement": "новое объявление", "agenda": "новая запись в расписании событий",
        "homework": "новое домашнее задание", "due": "до",
        "canceled": "урок отменён", "substitution": "замена",
        "room_change": "смена кабинета", "moved": "урок перенесён",
        "agenda_removed": "запись в расписании событий удалена",
        "agenda_changed": "запись в расписании событий изменена",
        "justification_accepted": "объяснение принято",
        "justification_rejected": "объяснение отклонено",
        "justification_status": "объяснение: новый статус",
        "forecast": "прогноз", "trip": "новая экскурсия", "document": "новый документ школы",
        "library_returned": "возвращено в библиотеку", "library_lent": "взято в библиотеке",
        "read_by": "{who} прочитал(-а) ваше сообщение", "read": "ваше сообщение прочитано",
        "badge": "получен значок", "summary": "итоги недели готовы",
        "for_parent": " (для родителей)", "for_student": " (для ученика)",
    },
    "de": {
        "behaviour": "Verhaltensnote", "new_grade": "neue Note",
        "grade_removed": "Note entfernt", "grade_changed": "Note geändert",
        "note_positive": "positive Verhaltensnotiz", "note_negative": "negative Verhaltensnotiz",
        "note": "Verhaltensnotiz",
        "absence_excused": "entschuldigte Fehlzeit", "absence_unexcused": "unentschuldigte Fehlzeit",
        "lesson": "Stunde",
        "message_from": "neue Nachricht von {who}", "message": "neue Nachricht",
        "announcement": "neue Ankündigung", "agenda": "neuer Termin",
        "homework": "neue Hausaufgabe", "due": "fällig",
        "canceled": "Stunde entfällt", "substitution": "Vertretung",
        "room_change": "Raumänderung", "moved": "Stunde verlegt",
        "agenda_removed": "Termin entfernt", "agenda_changed": "Termin geändert",
        "justification_accepted": "Entschuldigung akzeptiert",
        "justification_rejected": "Entschuldigung abgelehnt",
        "justification_status": "Status der Entschuldigung geändert",
        "forecast": "Prognose", "trip": "neuer Schulausflug", "document": "neues Schuldokument",
        "library_returned": "an die Bibliothek zurückgegeben",
        "library_lent": "aus der Bibliothek ausgeliehen",
        "read_by": "{who} hat deine Nachricht gelesen", "read": "deine Nachricht wurde gelesen",
        "badge": "Abzeichen erhalten", "summary": "Wochenzusammenfassung fertig",
        "for_parent": " (für die Eltern)", "for_student": " (für Schüler/in)",
    },
    "fr": {
        "behaviour": "note de conduite", "new_grade": "nouvelle note",
        "grade_removed": "note supprimée", "grade_changed": "note modifiée",
        "note_positive": "observation positive", "note_negative": "observation négative",
        "note": "observation",
        "absence_excused": "absence justifiée", "absence_unexcused": "absence non justifiée",
        "lesson": "cours",
        "message_from": "nouveau message de {who}", "message": "nouveau message",
        "announcement": "nouvelle annonce", "agenda": "nouvelle entrée d'agenda",
        "homework": "nouveau devoir", "due": "pour le",
        "canceled": "cours annulé", "substitution": "remplacement",
        "room_change": "changement de salle", "moved": "cours déplacé",
        "agenda_removed": "entrée d'agenda supprimée", "agenda_changed": "entrée d'agenda modifiée",
        "justification_accepted": "justificatif accepté",
        "justification_rejected": "justificatif refusé",
        "justification_status": "statut du justificatif modifié",
        "forecast": "prévision", "trip": "nouvelle sortie scolaire", "document": "nouveau document scolaire",
        "library_returned": "rendu à la bibliothèque", "library_lent": "emprunté à la bibliothèque",
        "read_by": "{who} a lu votre message", "read": "votre message a été lu",
        "badge": "badge obtenu", "summary": "résumé de la semaine prêt",
        "for_parent": " (pour le parent)", "for_student": " (pour l'élève)",
    },
}


class _Words:
    """The words of one language, and how it writes a day."""

    def __init__(self, language: str | None) -> None:
        code = (language or "en").split("-")[0].lower()
        self.code = code if code in _WORDS else "en"
        self._words = _WORDS[self.code]

    def __getitem__(self, key: str) -> str:
        return self._words.get(key) or _WORDS["en"][key]

    def day(self, value: Any) -> str:
        """'02.11' (day.month) outside English, '2026-11-02' in English;
        anything else as it came."""
        try:
            parsed = date.fromisoformat(str(value)[:10])
        except ValueError:
            return str(value or "")
        return parsed.isoformat() if self.code == "en" else parsed.strftime("%d.%m")


def _join(*parts: Any) -> str:
    return " - ".join(str(p) for p in parts if p not in (None, ""))


def _paren(value: Any) -> str:
    return f" ({value})" if value not in (None, "") else ""


def _grade(d: Mapping[str, Any], w: _Words) -> str:
    kind = d.get("kind")
    if kind == "behaviour":
        return f"{w['behaviour']}: {d.get('name') or d.get('value')}"
    value = d.get("value")
    if kind == "point" and d.get("max_points") is not None:
        value = f"{value}/{d.get('max_points')}"
    return f"{w['new_grade']} {_join(value, d.get('subject'))}{_paren(d.get('category'))}"


def _grade_change(d: Mapping[str, Any], w: _Words) -> str:
    def shown(fields: Mapping[str, Any]) -> Any:
        value = fields.get("value")
        if d.get("kind") == "behaviour":
            return fields.get("name") or value
        if d.get("kind") == "point" and fields.get("max_points") is not None:
            return f"{value}/{fields.get('max_points')}"
        return value

    what = f"{_join(d.get('subject'))}{_paren(d.get('category'))}"
    if d.get("change") == "removed":
        return f"{w['grade_removed']} {_join(shown(d), what)}".rstrip()
    previous = {**d, **(d.get("previous") or {})}
    head = w["grade_changed"]
    if shown(previous) != shown(d):
        return f"{head} {shown(previous)} → {_join(shown(d), what)}"
    return f"{head} {_join(shown(d), what)}".rstrip()


def _note(d: Mapping[str, Any], w: _Words) -> str:
    key = {"positive": "note_positive", "negative": "note_negative"}.get(d.get("sentiment"), "note")
    return f"{w[key]}: {_short(d.get('text'))}"


def _absence(d: Mapping[str, Any], w: _Words) -> str:
    label = w["absence_excused"] if d.get("excused") else w["absence_unexcused"]
    lesson = f", {w['lesson']} {d['lesson_no']}" if d.get("lesson_no") else ""
    return f"{label}: {w.day(d.get('date'))}{lesson}"


def _message(d: Mapping[str, Any], w: _Words) -> str:
    sender = d.get("sender")
    head = w["message_from"].replace("{who}", str(sender)) if sender else w["message"]
    return f"{head}: {_short(d.get('topic'))}"


def _announcement(d: Mapping[str, Any], w: _Words) -> str:
    return f"{w['announcement']}: {_short(d.get('subject'))}"


def _agenda(d: Mapping[str, Any], w: _Words) -> str:
    what = _join(d.get("category"), d.get("subject")) or _short(d.get("content"))
    return f"{w['agenda']}: {what}, {w.day(d.get('date'))}"


def _homework(d: Mapping[str, Any], w: _Words) -> str:
    due = d.get("due_date")
    due_text = f" ({w['due']} {w.day(due)})" if due else ""
    return f"{w['homework']}: {_join(d.get('subject'), _short(d.get('topic') or d.get('text')))}{due_text}"


_CHANGES = ("canceled", "substitution", "room_change", "moved")


def _timetable(d: Mapping[str, Any], w: _Words) -> str:
    change = d.get("change") or d.get("kind") or "substitution"
    label = w[change if change in _CHANGES else "substitution"]
    lesson = d.get("lesson_no")
    when = w.day(d.get("date"))
    if lesson:
        when += f", {w['lesson']} {lesson}"
    return f"{label}: {_join(d.get('subject'), when)}"


def _agenda_change(d: Mapping[str, Any], w: _Words) -> str:
    head = w["agenda_removed"] if d.get("kind") == "removed" else w["agenda_changed"]
    what = _join(d.get("category"), d.get("subject")) or _short(d.get("content"))
    return f"{head}: {what}, {w.day(d.get('date'))}"


def _justification(d: Mapping[str, Any], w: _Words) -> str:
    if d.get("accepted"):
        label = w["justification_accepted"]
    elif d.get("rejected"):
        label = w["justification_rejected"]
    else:
        label = w["justification_status"]
    start, end = w.day(d.get("date_from")), w.day(d.get("date_to"))
    span = start if not end or end == start else f"{start}–{end}"
    return f"{label}: {span}"


def _forecast(d: Mapping[str, Any], w: _Words) -> str:
    move = f"{d.get('old')} → {d.get('new')}"
    return f"{w['forecast']}: {_join(d.get('subject'), move)}"


def _trip(d: Mapping[str, Any], w: _Words) -> str:
    return f"{w['trip']}: {_short(d.get('destination'))}, {w.day(d.get('date_from'))}"


def _document(d: Mapping[str, Any], w: _Words) -> str:
    return f"{w['document']}: {_short(d.get('name'))}"


def _library(d: Mapping[str, Any], w: _Words) -> str:
    head = w["library_returned"] if d.get("action") == "returned" else w["library_lent"]
    return f"{head}: {_short(d.get('title'))}"


def _read(d: Mapping[str, Any], w: _Words) -> str:
    who = d.get("receiver")
    head = w["read_by"].replace("{who}", str(who)) if who else w["read"]
    return f"{head}: {_short(d.get('topic'))}"


def _achievement(d: Mapping[str, Any], w: _Words) -> str:
    return f"{w['badge']}: {d.get('title') or d.get('id')}"


def _summary(d: Mapping[str, Any], w: _Words) -> str:
    who = {"parent": w["for_parent"], "student": w["for_student"]}.get(d.get("audience"), "")
    head = f"{w['summary']}{who}"
    return f"{head}: {_short(d.get('headline'))}" if d.get("headline") else head


# event -> (message builder, icon)
DESCRIBERS: dict[str, tuple[Callable[[Mapping[str, Any], _Words], str], str]] = {
    EVENT_NEW_GRADE: (_grade, "mdi:numeric-6-box-outline"),
    EVENT_NEW_NOTE: (_note, "mdi:note-text-outline"),
    EVENT_NEW_ABSENCE: (_absence, "mdi:account-cancel-outline"),
    EVENT_NEW_MESSAGE: (_message, "mdi:email-outline"),
    EVENT_NEW_ANNOUNCEMENT: (_announcement, "mdi:bullhorn-outline"),
    EVENT_NEW_HOMEWORK: (_agenda, "mdi:calendar-plus"),
    EVENT_NEW_HOMEWORK_ASSIGNMENT: (_homework, "mdi:notebook-edit-outline"),
    EVENT_TIMETABLE_CHANGED: (_timetable, "mdi:calendar-alert"),
    EVENT_AGENDA_CHANGED: (_agenda_change, "mdi:calendar-edit"),
    EVENT_GRADE_CHANGED: (_grade_change, "mdi:pencil-circle-outline"),
    EVENT_JUSTIFICATION_STATUS: (_justification, "mdi:file-check-outline"),
    EVENT_FORECAST_CHANGED: (_forecast, "mdi:crystal-ball"),
    EVENT_NEW_SCHOOL_TRIP: (_trip, "mdi:bus-school"),
    EVENT_NEW_SCHOOL_DOCUMENT: (_document, "mdi:file-document-outline"),
    EVENT_LIBRARY_CHANGED: (_library, "mdi:bookshelf"),
    EVENT_MESSAGE_READ: (_read, "mdi:email-open-outline"),
    EVENT_ACHIEVEMENT_UNLOCKED: (_achievement, "mdi:trophy-outline"),
    EVENT_WEEKLY_SUMMARY: (_summary, "mdi:robot-outline"),
}


def describe(event_type: str, data: Mapping[str, Any], language: str | None) -> dict[str, str]:
    """The logbook entry for one of the integration's events."""
    build, icon = DESCRIBERS[event_type]
    return {
        LOGBOOK_ENTRY_NAME: str(data.get("student") or "Librus"),
        LOGBOOK_ENTRY_MESSAGE: build(data, _Words(language)),
        LOGBOOK_ENTRY_ICON: icon,
    }


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe the integration's events for the logbook."""

    @callback
    def _describe(event: Event) -> dict[str, str]:
        return describe(event.event_type, event.data, hass.config.language)

    for event_type in DESCRIBERS:
        async_describe_event(DOMAIN, event_type, _describe)
