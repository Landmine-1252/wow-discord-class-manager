"""Application name policy: two Unicode letter-based components, never guessed."""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

from .roles import UserError

if TYPE_CHECKING:
    from .config import Settings


def normalized_name_key(primary_name: str, secondary_name: str) -> str:
    """A local, per-owner comparison key; this does not verify game ownership."""
    full_name = f"{primary_name} {secondary_name}" if secondary_name else primary_name
    return unicodedata.normalize("NFC", full_name.casefold())


def valid_component(value: str) -> bool:
    """Allow letters and their attached combining marks, excluding separators."""
    if (
        not isinstance(value, str)
        or not value
        or not unicodedata.category(value[0]).startswith("L")
    ):
        return False
    invisible = {0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x3164, 0xFFA0}
    return all(
        unicodedata.category(character)[0] in {"L", "M"}
        and ord(character) not in invisible
        and not 0x180B <= ord(character) <= 0x180F
        and not 0xFE00 <= ord(character) <= 0xFE0F
        and not 0xE0100 <= ord(character) <= 0xE01EF
        for character in value
    )


def validate_names(
    settings: Settings, primary_name: str, secondary_name: str
) -> tuple[str, str, str]:
    """Preserve capitalization and reject unsafe input rather than rewriting it."""
    result: list[str] = []
    for label, value, minimum, maximum in (
        (
            "First Name",
            primary_name,
            settings.character_primary_name_min_length,
            settings.character_primary_name_max_length,
        ),
        (
            "Secondary Name",
            secondary_name,
            settings.character_secondary_name_min_length,
            settings.character_secondary_name_max_length,
        ),
    ):
        normalized = unicodedata.normalize("NFC", value)
        if not minimum <= len(normalized) <= maximum:
            raise UserError(f"{label} must contain {minimum}–{maximum} characters.")
        if not valid_component(normalized):
            raise UserError(
                f"{label} must contain letters and optional accents only, without"
                " spaces, punctuation, mentions or invisible characters."
            )
        result.append(normalized)
    return result[0], result[1], normalized_name_key(*result)
