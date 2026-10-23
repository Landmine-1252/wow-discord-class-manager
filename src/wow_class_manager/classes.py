"""The only registry of stable class IDs, display names and official colors."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WowClass:
    id: str
    name: str
    color: int
    classic: bool = True


CLASSES = (
    WowClass("warrior", "Warrior", 0xC69B6D),
    WowClass("paladin", "Paladin", 0xF48CBA),
    WowClass("hunter", "Hunter", 0xAAD372),
    WowClass("rogue", "Rogue", 0xFFF468),
    WowClass("priest", "Priest", 0xFFFFFF),
    WowClass("shaman", "Shaman", 0x0070DD),
    WowClass("mage", "Mage", 0x3FC7EB),
    WowClass("warlock", "Warlock", 0x8788EE),
    WowClass("druid", "Druid", 0xFF7C0A),
    WowClass("death_knight", "Death Knight", 0xC41E3A, False),
    WowClass("monk", "Monk", 0x00FF98, False),
    WowClass("demon_hunter", "Demon Hunter", 0xA330C9, False),
    WowClass("evoker", "Evoker", 0x33937F, False),
)
REGISTRY = {entry.id: entry for entry in CLASSES}
DEFAULT_CLASSES = tuple(entry.id for entry in CLASSES if entry.classic)
ALPHABETICAL_CLASSES = tuple(sorted(CLASSES, key=lambda entry: entry.name.casefold()))


@dataclass(frozen=True, slots=True)
class ClassRole:
    definition: WowClass
    name: str
    color: int

    @property
    def id(self) -> str:
        return self.definition.id
