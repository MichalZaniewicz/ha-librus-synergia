"""Picking message recipients by what a person would type.

`resolve_recipient` turns "wychowawca", "Matematyka", a surname or an
account id into exactly one entry of the recipient directory
(`LibrusDataUpdateCoordinator.async_message_recipients`) - or explains why
it can't, so a message never goes to a guessed person. Pure functions,
no Home Assistant imports.
"""

from __future__ import annotations

from typing import Any

from librus_synergia.models import MessageRecipientData

# Words that mean "the class tutor" / "the secretary's office".
_TUTOR_WORDS = frozenset({"wychowawca", "wychowawczyni", "tutor", "homeroom teacher", "form teacher"})
_SECRETARY_WORDS = frozenset({"sekretariat", "secretary", "school office"})


class RecipientError(ValueError):
    """No recipient, or more than one, matches. `candidates` lists the
    names that matched (empty when none did)."""

    def __init__(self, query: str, candidates: list[str]) -> None:
        self.query = query
        self.candidates = candidates
        super().__init__(
            f"'{query}' matches more than one recipient: {', '.join(candidates)}"
            if candidates
            else f"No recipient matches '{query}'."
        )


def _fold(text: str) -> str:
    return " ".join(text.casefold().split())


def resolve_recipient(
    recipients: list[MessageRecipientData], query: str
) -> MessageRecipientData:
    """The one recipient `query` means. In this order: an account id; the
    words for the tutor or the secretary's office; a subject the student is
    taught (its teacher); a part of a person's name. Raises
    `RecipientError` when nothing matches or a step matches several."""
    text = _fold(query)
    if not text:
        raise RecipientError(query, [])
    if text.isdigit():
        found = [r for r in recipients if r.account_id == text]
        if found:
            return found[0]
        raise RecipientError(query, [])
    if text in _TUTOR_WORDS:
        return _one(query, [r for r in recipients if r.group == "tutors"])
    if text in _SECRETARY_WORDS:
        return _one(query, [r for r in recipients if r.group == "secretary"])
    by_subject = [r for r in recipients if any(_fold(s) == text for s in r.subjects)]
    if not by_subject:
        by_subject = [r for r in recipients if any(_fold(s).startswith(text) for s in r.subjects)]
    if by_subject:
        return _one(query, by_subject)
    return _one(query, [r for r in recipients if text in _fold(r.name)])


def _one(query: str, found: list[MessageRecipientData]) -> MessageRecipientData:
    if len(found) == 1:
        return found[0]
    raise RecipientError(query, [_label(r) for r in found[:10]])


def _label(recipient: MessageRecipientData) -> str:
    if recipient.subjects:
        return f"{recipient.name} ({', '.join(recipient.subjects)})"
    return f"{recipient.name} ({recipient.group_name})"


def recipient_dict(recipient: MessageRecipientData) -> dict[str, Any]:
    """A recipient as the services return it."""
    return {
        "account_id": recipient.account_id,
        "name": recipient.name,
        "group": recipient.group,
        "group_name": recipient.group_name,
        "subjects": recipient.subjects,
        "available": recipient.available,
        "return_at": recipient.return_at,
    }
