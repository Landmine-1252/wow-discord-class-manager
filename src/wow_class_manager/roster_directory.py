"""Read-only directory projections from a guild's registered characters."""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from .character_store import Character

DIRECTORY_PAGE_SIZE = 8
SEARCH_LIMIT = 100
KINDS = {"mains": "Mains", "alts": "Alts", "all": "All characters"}


def search_key(value: str) -> str:
    return unicodedata.normalize("NFC", " ".join(value.split())).casefold()


def character_order(character: Character) -> tuple[str, int, int]:
    return search_key(character.full_name), character.user_id, character.id


def directory_page[T](
    entries: Sequence[T], requested: int
) -> tuple[Sequence[T], int, int]:
    pages = max(1, (len(entries) + DIRECTORY_PAGE_SIZE - 1) // DIRECTORY_PAGE_SIZE)
    page = max(0, min(requested, pages - 1))
    start = page * DIRECTORY_PAGE_SIZE
    return entries[start : start + DIRECTORY_PAGE_SIZE], page, pages


@dataclass(frozen=True, slots=True)
class RosterFilters:
    user_id: int | None = None
    class_id: str | None = None
    gameplay_role: str | None = None
    kind: str = "mains"
    query: str = ""

    def matches(self, character: Character) -> bool:
        if self.user_id is not None and character.user_id != self.user_id:
            return False
        if self.class_id is not None and character.class_id != self.class_id:
            return False
        if self.kind == "mains" and not character.is_main:
            return False
        if self.kind == "alts" and character.is_main:
            return False
        role = character.gameplay_role or "unset"
        if self.gameplay_role is not None and role != self.gameplay_role:
            return False
        return search_key(self.query) in search_key(character.full_name)


class Directory:
    """Use one storage snapshot for totals, alt counts and filtered results."""

    def __init__(self, characters: Sequence[Character], filters: RosterFilters):
        self.characters = characters
        self.members = {character.user_id for character in characters}
        self.main_roles = Counter(
            character.gameplay_role for character in characters if character.is_main
        )
        self.alt_counts = Counter(
            character.user_id for character in characters if not character.is_main
        )
        main_owners = {
            character.user_id for character in characters if character.is_main
        }
        self.without_main = len(self.members - main_owners)
        self.matches = sorted(
            (character for character in characters if filters.matches(character)),
            key=character_order,
        )
