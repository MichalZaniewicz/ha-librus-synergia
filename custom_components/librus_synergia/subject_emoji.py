"""Emoji in front of calendar titles - the "Emoji in calendar titles"
option (const.CONF_CALENDAR_EMOJI). Pure: no Home Assistant imports.

"📐 Matematyka", "📝 [Sprawdzian] Chemia: ..." read faster on a phone or a
watch, where a calendar shows little else. Subjects and Agenda categories
are school data, not a fixed list, so they are matched by word stems
(case and Polish letters ignored); an unknown subject gets a generic book,
an unknown category nothing.
"""

from __future__ import annotations

import re
import unicodedata

# First match wins: the more specific stems go first ("edukacja dla
# bezpieczeństwa" before "edukacja", "wiedza o społeczeństwie" before
# "wiedza").
_SUBJECTS: tuple[tuple[str, str], ...] = (
    ("matemat", "📐"),
    ("polski", "📖"),
    ("angiel", "🇬🇧"),
    ("niemiec", "🇩🇪"),
    ("francus", "🇫🇷"),
    ("hiszpan", "🇪🇸"),
    ("rosyj", "🇷🇺"),
    ("wlosk", "🇮🇹"),
    ("lacin", "🏺"),
    ("histori", "🏛️"),
    ("wiedza o spoleczenstwie", "🗳️"),
    ("biologi", "🧬"),
    ("chemi", "🧪"),
    ("fizyk", "⚛️"),
    ("geografi", "🌍"),
    ("przyrod", "🌱"),
    ("informaty", "💻"),
    ("wychowanie fizyczne", "⚽"),
    ("plasty", "🎨"),
    ("muzyk", "🎵"),
    ("techni", "🔧"),
    ("edukacja dla bezpieczenstwa", "🚑"),
    ("zdrowot", "🍎"),
    ("religi", "🕊️"),
    ("etyk", "⚖️"),
    ("filozof", "💭"),
    ("godzina wychowawcza", "👥"),
    ("zajecia z wychowawca", "👥"),
    ("doradztwo", "🧭"),
    ("wspomaganie", "🤝"),
    ("biznes", "💼"),
    ("przedsiebiorcz", "💼"),
    ("edukacja wczesnoszkolna", "✏️"),
)
SUBJECT_FALLBACK = "📚"

_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("kartkow", "✏️"),
    ("quiz", "✏️"),
    ("sprawdzian", "📝"),
    ("praca klasowa", "📝"),
    ("egzamin", "📝"),
    ("diagnoz", "📝"),
    ("test", "📝"),
    ("wycieczk", "🚌"),
    ("zebrani", "👪"),
    ("wywiadowk", "👪"),
    ("konkurs", "🏆"),
    ("apel", "🎤"),
    ("zawody", "🏅"),
    ("teatr", "🎭"),
    ("kino", "🎬"),
    ("projekt", "🧩"),
    ("odpowiedz", "🗣️"),
    ("zadanie", "📚"),
)


def fold(text: str) -> str:
    """Lower case without Polish letters ("Język" -> "jezyk"). "ł" has no
    decomposition, so it is mapped by hand."""
    text = text.lower().replace("ł", "l")
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def _match(text: str, table: tuple[tuple[str, str], ...]) -> str | None:
    folded = re.sub(r"\s+", " ", fold(text))
    return next((emoji for stem, emoji in table if stem in folded), None)


def subject_emoji(subject: str) -> str:
    """The emoji for a subject name; a book when nothing matches."""
    return _match(subject, _SUBJECTS) or SUBJECT_FALLBACK


def category_emoji(category: str) -> str | None:
    """The emoji for an Agenda category name (Sprawdzian, Wycieczka...), or
    None when the category isn't one of the known kinds."""
    return _match(category, _CATEGORIES)


def with_emoji(emoji: str | None, summary: str) -> str:
    return f"{emoji} {summary}" if emoji else summary
