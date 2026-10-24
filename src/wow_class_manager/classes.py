"""The only registry of stable class IDs, labels, colors and gameplay roles."""

from dataclasses import dataclass

GAMEPLAY_ROLES = {"dps": "DPS", "healing": "Healing", "tank": "Tank"}


@dataclass(frozen=True, slots=True)
class WowClass:
    id: str
    name: str
    color: int
    classic: bool = True
    gameplay_roles: tuple[str, ...] = ("dps",)


CLASSES = (
    WowClass("warrior", "Warrior", 0xC69B6D, gameplay_roles=("dps", "tank")),
    WowClass("paladin", "Paladin", 0xF48CBA, gameplay_roles=("dps", "healing", "tank")),
    WowClass("hunter", "Hunter", 0xAAD372),
    WowClass("rogue", "Rogue", 0xFFF468),
    WowClass("priest", "Priest", 0xFFFFFF, gameplay_roles=("dps", "healing")),
    WowClass("shaman", "Shaman", 0x0070DD, gameplay_roles=("dps", "healing")),
    WowClass("mage", "Mage", 0x3FC7EB),
    WowClass("warlock", "Warlock", 0x8788EE),
    WowClass("druid", "Druid", 0xFF7C0A, gameplay_roles=("dps", "healing", "tank")),
    WowClass("death_knight", "Death Knight", 0xC41E3A, False, ("dps", "tank")),
    WowClass("monk", "Monk", 0x00FF98, False, ("dps", "healing", "tank")),
    WowClass("demon_hunter", "Demon Hunter", 0xA330C9, False, ("dps", "tank")),
    WowClass("evoker", "Evoker", 0x33937F, False, ("dps", "healing")),
)
REGISTRY = {entry.id: entry for entry in CLASSES}
DEFAULT_CLASSES = tuple(entry.id for entry in CLASSES if entry.classic)
ALPHABETICAL_CLASSES = tuple(sorted(CLASSES, key=lambda entry: entry.name.casefold()))


def supports_gameplay_role(class_id: str, gameplay_role: str) -> bool:
    definition = REGISTRY.get(class_id)
    return definition is not None and gameplay_role in definition.gameplay_roles


@dataclass(frozen=True, slots=True)
class ClassRole:
    definition: WowClass
    name: str
    color: int

    @property
    def id(self) -> str:
        return self.definition.id
