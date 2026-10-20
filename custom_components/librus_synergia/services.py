"""Service registrations for the Librus Synergia (unofficial) integration.

- `get_message` - see its handler's docstring for why this is deliberately
  a service (explicit user action) rather than anything wired into routine
  polling.
- `refresh` - force an immediate data refresh (e.g. right before a morning
  briefing automation, instead of waiting for the next poll).
- `get_grades` - all of a student's grades (optionally filtered to one
  subject) in one response, instead of reading each subject sensor's own
  `grades` attribute separately. Purely a reshape of `coordinator.data`
  already in memory - no extra Librus request, unlike `get_message`.
- `get_message_recipients` - who a message can go to (read-only).
- `send_message` / `withdraw_message` - write to Wiadomości. Only when
  "Allow sending messages" is on under Configure; a send is never retried,
  at most MESSAGE_SEND_LIMIT_PER_HOUR an hour, and `dry_run` shows what
  would go out without sending. Deliberately not offered to Assist or the
  AI summary (llm_api.py / ai_summary.py) - only a person or an automation
  someone wrote can send.
- `mark_announcement_read` - marks school notices read in Librus (one, a
  few, or every unread one), as opening them there does. A person's action
  (the Announcements card does it when you open a notice) - never polling.
- `mark_message_read` - marks Wiadomości messages read (one, a few, or
  every unread one on a mailbox's list) by opening them, the only way
  Wiadomości offers. Also only ever a person's action.
- `sync_calendar` - copies the Agenda and days off into another calendar
  (e.g. a family Google Calendar) and keeps the copies up to date; see
  calendar_sync.py.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.util import dt as dt_util

from librus_synergia import LibrusError, LibrusMessageSendError
from librus_synergia.parsers import (
    parse_message,
    parse_message_receivers,
    point_grades_percentage,
    reply_topic,
)

from .const import (
    CONF_CALENDAR_EMOJI,
    CONF_MESSAGE_SENDING_ENABLED,
    DEFAULT_CALENDAR_EMOJI,
    DEFAULT_MESSAGE_SENDING_ENABLED,
    DOMAIN,
)
from .coordinator import (
    LibrusDataUpdateCoordinator,
    decode_message_content,
    grade_improvements,
    resolve_sender_name,
)
from .messaging import RecipientError, recipient_dict, resolve_recipient

_LOGGER = logging.getLogger(__name__)

SERVICE_GET_MESSAGE = "get_message"
SERVICE_REFRESH = "refresh"
SERVICE_GET_GRADES = "get_grades"
SERVICE_GET_MESSAGE_RECIPIENTS = "get_message_recipients"
SERVICE_SEND_MESSAGE = "send_message"
SERVICE_WITHDRAW_MESSAGE = "withdraw_message"
SERVICE_MARK_ANNOUNCEMENT_READ = "mark_announcement_read"
SERVICE_MARK_MESSAGE_READ = "mark_message_read"
SERVICE_SYNC_CALENDAR = "sync_calendar"
_SERVICES = (
    SERVICE_GET_MESSAGE,
    SERVICE_REFRESH,
    SERVICE_GET_GRADES,
    SERVICE_GET_MESSAGE_RECIPIENTS,
    SERVICE_SEND_MESSAGE,
    SERVICE_WITHDRAW_MESSAGE,
    SERVICE_MARK_ANNOUNCEMENT_READ,
    SERVICE_MARK_MESSAGE_READ,
    SERVICE_SYNC_CALENDAR,
)

_GET_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("message_id"): cv.string,
        vol.Optional("mailbox", default="inbox"): cv.string,
    }
)

_REFRESH_SCHEMA = vol.Schema({vol.Optional("device_id"): cv.string})

_DEVICE_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})

_SEND_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional("recipient"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("topic"): cv.string,
        vol.Required("message"): cv.string,
        vol.Optional("reply_to"): cv.string,
        vol.Optional("dry_run", default=False): cv.boolean,
    }
)

_WITHDRAW_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("message_id"): cv.string,
    }
)


# No `announcement_id`: every unread notice.
_MARK_ANNOUNCEMENT_READ_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional("announcement_id"): vol.All(cv.ensure_list, [cv.string]),
    }
)

# No `message_id`: every unread message on the mailbox's list.
_MARK_MESSAGE_READ_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional("message_id"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("mailbox", default="inbox"): vol.In(
            ["inbox", "substitutions", "alerts", "justifications", "archive/inbox"]
        ),
    }
)

_SYNC_CALENDAR_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("target_calendar"): cv.entity_domain("calendar"),
        vol.Optional("days", default=30): vol.All(vol.Coerce(int), vol.Range(min=1, max=180)),
        # "sprawdzian, kartkówka" or a list; empty = every Agenda entry.
        vol.Optional("categories"): vol.Any(cv.string, [cv.string]),
        vol.Optional("free_days", default=True): cv.boolean,
        # "Ola:" in front of every title - handy with more than one child.
        vol.Optional("prefix"): cv.string,
        vol.Optional("dry_run", default=False): cv.boolean,
    }
)

_GET_GRADES_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional("subject_id"): vol.Coerce(int),
    }
)


def resolve_coordinator(hass: HomeAssistant, device_id: str) -> LibrusDataUpdateCoordinator:
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(f"Unknown device: {device_id}")

    # Newer Home Assistant: one config entry per device (`config_entry_id`);
    # older: the `config_entries` set (deprecated, gone in 2027.10).
    single = getattr(device, "config_entry_id", None)
    entry_ids = [single] if single else device.config_entries
    entry = next(
        (
            e
            for entry_id in entry_ids
            if (e := hass.config_entries.async_get_entry(entry_id)) is not None
            and e.domain == DOMAIN
        ),
        None,
    )
    if entry is None:
        raise ServiceValidationError(f"Device {device_id} is not a Librus Synergia device.")
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(f"The Librus Synergia entry for {device_id} isn't loaded.")
    return entry.runtime_data


def async_setup_services(hass: HomeAssistant) -> None:
    """Register this integration's services, once for the whole domain
    regardless of how many config entries (students) exist - guarded so a
    second config entry being set up doesn't try to double-register."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_MESSAGE):
        return

    async def _async_handle_refresh(call: ServiceCall) -> None:
        """Force an immediate data refresh. With `device_id` set, just that
        student; otherwise every loaded Librus Synergia entry. Uses the
        coordinator's debounced request, so spamming it is harmless."""
        device_id = call.data.get("device_id")
        if device_id:
            coordinators = [resolve_coordinator(hass, device_id)]
        else:
            coordinators = [
                entry.runtime_data
                for entry in hass.config_entries.async_entries(DOMAIN)
                if entry.state is ConfigEntryState.LOADED
            ]
        # Together: each student's refresh waits on Librus for seconds, and
        # one after another made the call last that times the students.
        await asyncio.gather(*(c.async_force_refresh() for c in coordinators))

    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, _async_handle_refresh, schema=_REFRESH_SCHEMA
    )

    async def _async_handle_get_message(call: ServiceCall) -> ServiceResponse:
        """Fetch ONE message's full, untruncated content.

        Deliberately a service, not a sensor attribute or coordinator
        field: CONFIRMED live (2026-09-06) that fetching a single message
        marks it read server-side on Librus (readDate flips from null to a
        real timestamp on the very next poll) - exactly like opening a
        message in the real Librus app. This must only ever run when a
        person has explicitly asked to read ONE specific message (e.g.
        clicking it in a dashboard card) - never from the coordinator's
        routine polling, which only ever uses the list/count endpoints and
        never marks anything read (see LibrusApiClient's module comment
        above async_get_message).
        """
        coordinator = resolve_coordinator(hass, call.data["device_id"])
        message_id = call.data["message_id"]
        mailbox = call.data["mailbox"]
        try:
            raw = await coordinator.async_fetch_message(mailbox, message_id)
        except LibrusError as err:
            raise HomeAssistantError(f"Failed to fetch message {message_id}: {err}") from err

        data = raw.get("data")
        if not isinstance(data, dict):
            raise HomeAssistantError(f"Unexpected response fetching message {message_id}.")

        sender_name = resolve_sender_name(data)
        full = parse_message(raw, mailbox, message_id)
        # CONFIRMED live (2026-09-17): each entry is {"filename": ..., "id":
        # ...}. The file itself downloads through attachment_view.py.
        attachments = [
            {"id": str(a["id"]), "filename": a.get("filename")}
            for a in data.get("attachments") or []
            if isinstance(a, dict) and a.get("id") is not None
        ]
        return {
            "id": message_id,
            "mailbox": mailbox,
            "sender": sender_name,
            "topic": data.get("topic", ""),
            "content": decode_message_content(data.get("Message", "")),
            "send_date": data.get("sendDate"),
            "read_date": data.get("readDate"),
            "has_attachment": bool(attachments),
            "attachments": attachments,
            # A sent message: who it went to and when each one read it.
            "receivers": [
                {"name": r.name, "group": r.group, "read_date": r.read_date}
                for r in parse_message_receivers(data)
            ],
            # For writing (send_message): whether Librus takes a reply to it,
            # and - for a sent one - whether it can still be withdrawn.
            "no_reply": full.no_reply if full else False,
            "can_withdraw": full.can_withdraw if full else None,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_MESSAGE,
        _async_handle_get_message,
        schema=_GET_MESSAGE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    async def _async_handle_get_grades(call: ServiceCall) -> ServiceResponse:
        """All of a student's grades in one response, optionally filtered
        to one `subject_id`.

        Unlike `get_message`, this reads straight from the coordinator's
        already-fetched data - no live Librus request, so it's safe to
        call from routine automations, not just direct user action. Each
        subject average sensor already exposes its OWN `grades` attribute,
        but reading "every grade across every subject" today means polling
        16+ separate sensors; this is the single-call equivalent.
        """
        coordinator = resolve_coordinator(hass, call.data["device_id"])
        if coordinator.data is None:
            raise HomeAssistantError("No data available yet - the integration hasn't completed its first refresh.")

        subject_id = call.data.get("subject_id")
        data = coordinator.data
        improves, improved = grade_improvements(data.grades)
        grades = [
            {
                "subject": data.subjects.get(grade.subject_id) if grade.subject_id is not None else None,
                "subject_id": grade.subject_id,
                "value": grade.value,
                "category": (
                    category.name
                    if (category := data.grade_categories.get(grade.category_id)) is not None
                    else None
                ),
                "date": grade.add_date,
                "semester": grade.semester,
                "comments": list(grade.comments),
                "teacher": data.teachers.get(grade.teacher_id) if grade.teacher_id is not None else None,
                "improves": improves.get(grade.id),
                "improved": grade.id in improved,
            }
            for grade in data.grades
            if subject_id is None or grade.subject_id == subject_id
        ]
        grades.sort(key=lambda g: g["date"] or "", reverse=True)
        response: dict[str, Any] = {"grades": grades, "count": len(grades)}
        text_grades = [
            g for g in data.text_grades if subject_id is None or g.subject_id == subject_id
        ]
        if text_grades:
            response["text_grades"] = [
                {
                    "subject": data.subjects.get(g.subject_id) if g.subject_id is not None else None,
                    "subject_id": g.subject_id,
                    "value": g.value,
                    "category": g.category,
                    "date": g.date,
                    "semester": g.semester,
                    "teacher": data.teachers.get(g.teacher_id) if g.teacher_id is not None else None,
                }
                for g in text_grades
            ]
        descriptive_grades = [
            g for g in data.descriptive_grades if subject_id is None or g.subject_id == subject_id
        ]
        if descriptive_grades:
            response["descriptive_grades"] = [
                {
                    "subject": data.subjects.get(g.subject_id) if g.subject_id is not None else None,
                    "subject_id": g.subject_id,
                    "value": g.value,
                    "skill": g.skill,
                    "comments": list(g.comments),
                    "requirements": list(g.requirements),
                    "date": g.date or g.add_date,
                    "semester": g.semester,
                    "teacher": data.teachers.get(g.teacher_id if g.teacher_id is not None else g.teacher_lid),
                }
                for g in descriptive_grades
            ]
        # Schools grading in points (0-100 etc.) - only when there are any.
        point_grades = [
            g for g in data.point_grades if subject_id is None or g.subject_id == subject_id
        ]
        if point_grades:
            response["point_grades"] = [
                {
                    "subject": data.subjects.get(g.subject_id) if g.subject_id is not None else None,
                    "subject_id": g.subject_id,
                    "value": g.value,
                    "points": g.points,
                    "max_points": g.max_points,
                    "percentage": g.percentage,
                    "category": g.category,
                    "date": g.add_date,
                    "semester": g.semester,
                    "teacher": data.teachers.get(g.teacher_id) if g.teacher_id is not None else None,
                }
                for g in sorted(point_grades, key=lambda g: g.add_date or "", reverse=True)
            ]
            response["points_percentage"] = point_grades_percentage(point_grades)
        return response

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_GRADES,
        _async_handle_get_grades,
        schema=_GET_GRADES_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    async def _async_handle_mark_announcement_read(call: ServiceCall) -> ServiceResponse:
        """Marks the given notices (or every unread one) read in Librus."""
        coordinator = resolve_coordinator(hass, call.data["device_id"])
        ids: list[str] | None = call.data.get("announcement_id") or None
        try:
            marked = await coordinator.async_mark_announcements_read(ids)
        except LibrusError as err:
            raise HomeAssistantError(f"Librus didn't mark the announcement read: {err}") from err
        return {"marked": marked}

    hass.services.async_register(
        DOMAIN,
        SERVICE_MARK_ANNOUNCEMENT_READ,
        _async_handle_mark_announcement_read,
        schema=_MARK_ANNOUNCEMENT_READ_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _async_handle_mark_message_read(call: ServiceCall) -> ServiceResponse:
        """Marks the given messages (or every unread one on the mailbox's
        list) read, by opening each one - Wiadomości has no other way."""
        coordinator = resolve_coordinator(hass, call.data["device_id"])
        ids: list[str] | None = call.data.get("message_id") or None
        try:
            marked = await coordinator.async_mark_messages_read(call.data["mailbox"], ids)
        except LibrusError as err:
            raise HomeAssistantError(f"Librus didn't mark the message read: {err}") from err
        return {"marked": marked}

    hass.services.async_register(
        DOMAIN,
        SERVICE_MARK_MESSAGE_READ,
        _async_handle_mark_message_read,
        schema=_MARK_MESSAGE_READ_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    _async_setup_message_writing(hass)


def _require_sending(coordinator: LibrusDataUpdateCoordinator) -> None:
    entry = coordinator.config_entry
    enabled = entry is not None and entry.options.get(
        CONF_MESSAGE_SENDING_ENABLED, DEFAULT_MESSAGE_SENDING_ENABLED
    )
    if not enabled:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="message_sending_disabled"
        )


def _send_error(err: LibrusMessageSendError) -> HomeAssistantError:
    # The short user-facing text leaves the cause out; it goes to the log.
    _LOGGER.warning("Librus message: %s", err)
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="message_maybe_sent" if err.sent is None else "message_not_sent",
        translation_placeholders={"error": str(err)},
    )


def _async_setup_message_writing(hass: HomeAssistant) -> None:
    """get_message_recipients, send_message and withdraw_message."""

    async def _async_handle_recipients(call: ServiceCall) -> ServiceResponse:
        """Who a message can go to - read-only, works with sending off."""
        coordinator = resolve_coordinator(hass, call.data["device_id"])
        try:
            recipients = await coordinator.async_message_recipients()
        except LibrusError as err:
            raise HomeAssistantError(f"Couldn't read the message recipients: {err}") from err
        return {"recipients": [recipient_dict(r) for r in recipients]}

    async def _async_handle_send(call: ServiceCall) -> ServiceResponse:
        """Send a message, or a reply in a received message's thread
        (`reply_to`). With `dry_run`, only says what would go out."""
        coordinator = resolve_coordinator(hass, call.data["device_id"])
        dry_run: bool = call.data["dry_run"]
        if not dry_run:
            # A dry run sends nothing, so it works with sending switched off.
            _require_sending(coordinator)
        text: str = call.data["message"]
        reply_to: str | None = call.data.get("reply_to")
        queries: list[str] = call.data.get("recipient") or []
        if reply_to is None and not queries:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="message_no_recipient"
            )
        if reply_to is None and not (call.data.get("topic") or "").strip():
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="message_no_topic"
            )
        try:
            if reply_to is not None:
                if dry_run:
                    # Opening the message (to find its sender) would mark it
                    # read - a dry run only describes it from the inbox list.
                    listed = next(
                        (
                            m
                            for m in (coordinator.data.messages if coordinator.data else [])
                            if m.id == reply_to
                        ),
                        None,
                    )
                    return {
                        "sent": False,
                        "dry_run": True,
                        "reply_to": reply_to,
                        "to": [listed.sender_name] if listed else [],
                        "topic": call.data.get("topic")
                        or (reply_topic(listed.topic) if listed else None),
                        "message": text,
                    }
                original = parse_message(
                    await coordinator.async_fetch_message("inbox", reply_to), "inbox", reply_to
                )
                if original is None or not original.sender_id:
                    raise LibrusMessageSendError(
                        f"Message {reply_to} or its sender wasn't found - not sent.", sent=False
                    )
                if original.no_reply:
                    raise LibrusMessageSendError(
                        "This message doesn't accept replies - not sent.", sent=False
                    )
                to_ids, to_names = [original.sender_id], [original.sender_name]
                topic = call.data.get("topic") or reply_topic(original.topic)
            else:
                recipients = await coordinator.async_message_recipients()
                chosen = [resolve_recipient(recipients, q) for q in queries]
                if len({r.account_id for r in chosen}) != len(chosen):
                    raise ServiceValidationError(
                        translation_domain=DOMAIN, translation_key="message_duplicate_recipient"
                    )
                to_ids = [r.account_id for r in chosen]
                to_names = [r.name for r in chosen]
                topic = call.data["topic"]
            if dry_run:
                return {
                    "sent": False,
                    "dry_run": True,
                    "to": to_names,
                    "account_ids": to_ids,
                    "topic": topic,
                    "message": text,
                }
            sent = await coordinator.async_send_message(to_ids, topic, text, reply_to=reply_to)
        except RecipientError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="message_recipient_ambiguous"
                if err.candidates
                else "message_recipient_unknown",
                translation_placeholders={
                    "recipient": err.query,
                    "candidates": ", ".join(err.candidates),
                },
            ) from err
        except LibrusMessageSendError as err:
            raise _send_error(err) from err
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except LibrusError as err:
            raise HomeAssistantError(f"Couldn't prepare the message - not sent: {err}") from err
        _LOGGER.info("Librus message %s sent to %s", sent.message_id, ", ".join(to_names))
        return {
            "sent": True,
            "message_id": sent.message_id,
            "to": to_names,
            "topic": topic,
            "reply_to": reply_to,
        }

    async def _async_handle_withdraw(call: ServiceCall) -> None:
        coordinator = resolve_coordinator(hass, call.data["device_id"])
        _require_sending(coordinator)
        try:
            await coordinator.async_withdraw_message(call.data["message_id"])
        except LibrusError as err:
            raise HomeAssistantError(
                f"Librus didn't withdraw message {call.data['message_id']}: {err}"
            ) from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_MESSAGE_RECIPIENTS,
        _async_handle_recipients,
        schema=_DEVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        _async_handle_send,
        schema=_SEND_MESSAGE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_WITHDRAW_MESSAGE,
        _async_handle_withdraw,
        schema=_WITHDRAW_MESSAGE_SCHEMA,
    )

    async def _async_handle_sync_calendar(call: ServiceCall) -> ServiceResponse:
        """Copy the student's Agenda (and days off) into another calendar,
        keeping earlier copies up to date - see calendar_sync.py. Reads
        `coordinator.data` only; no extra Librus request."""
        # Here, not at the top: calendar_sync builds on calendar.py, which
        # imports the package's __init__ - still loading when this module is.
        from .calendar_sync import (  # noqa: PLC0415
            async_sync_calendar,
            parse_categories,
            sync_items,
        )

        coordinator = resolve_coordinator(hass, call.data["device_id"])
        data = coordinator.data
        entry = coordinator.config_entry
        if data is None or entry is None:
            raise HomeAssistantError("No Librus data yet - try again after the next refresh.")
        today = dt_util.now().date()
        days = call.data["days"]
        items = sync_items(
            data,
            today=today,
            days=days,
            categories=parse_categories(call.data.get("categories")),
            free_days=call.data["free_days"],
            prefix=call.data.get("prefix") or "",
            emoji=bool(entry.options.get(CONF_CALENDAR_EMOJI, DEFAULT_CALENDAR_EMOJI)),
        )
        return await async_sync_calendar(
            hass,
            entry_id=entry.entry_id,
            target_entity_id=call.data["target_calendar"],
            items=items,
            today=today,
            days=days,
            dry_run=call.data["dry_run"],
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SYNC_CALENDAR,
        _async_handle_sync_calendar,
        schema=_SYNC_CALENDAR_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove this integration's services - call only once the LAST config
    entry is about to be unloaded (see __init__.py::async_unload_entry),
    so a second student's entry doesn't lose the service while the first
    one is just being reloaded."""
    for service in _SERVICES:
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)

