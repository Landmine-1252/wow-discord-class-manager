"""The only registry of stable class IDs, labels, icons, colors and gameplay roles."""

from dataclasses import dataclass, field

GAMEPLAY_ROLES = {"dps": "DPS", "healing": "Healing", "tank": "Tank"}
GAMEPLAY_ROLE_EMOJIS = {"dps": "⚔️", "healing": "💚", "tank": "🛡️"}
# Unicode in ordinary role names works without Nitro or Server Boosts.
GAMEPLAY_ROLE_TAGS = {
    role: f"{GAMEPLAY_ROLE_EMOJIS[role]} {label}"
    for role, label in GAMEPLAY_ROLES.items()
}


@dataclass(frozen=True, slots=True)
class WowClass:
    id: str
    name: str
    color: int
    # Class-themed Unicode symbols need no custom emoji upload or guild setup.
    emoji: str = field(kw_only=True)
    classic: bool = True
    gameplay_roles: tuple[str, ...] = ("dps",)


CLASSES = (
    WowClass(
        id="warrior",
        name="Warrior",
        color=0xC69B6D,
        emoji="🪓",
        classic=True,
        gameplay_roles=("dps", "tank"),
    ),
    WowClass(
        id="paladin",
        name="Paladin",
        color=0xF48CBA,
        emoji="🔨",
        classic=True,
        gameplay_roles=("dps", "healing", "tank"),
    ),
    WowClass(
        id="hunter",
        name="Hunter",
        color=0xAAD372,
        emoji="🏹",
        classic=True,
        gameplay_roles=("dps",),
    ),
    WowClass(
        id="rogue",
        name="Rogue",
        color=0xFFF468,
        emoji="🗡️",
        classic=True,
        gameplay_roles=("dps",),
    ),
    WowClass(
        id="priest",
        name="Priest",
        color=0xFFFFFF,
        emoji="✨",
        classic=True,
        gameplay_roles=("dps", "healing"),
    ),
    WowClass(
        id="shaman",
        name="Shaman",
        color=0x0070DD,
        emoji="⚡",
        classic=True,
        gameplay_roles=("dps", "healing"),
    ),
    WowClass(
        id="mage",
        name="Mage",
        color=0x3FC7EB,
        emoji="🔮",
        classic=True,
        gameplay_roles=("dps",),
    ),
    WowClass(
        id="warlock",
        name="Warlock",
        color=0x8788EE,
        emoji="🔥",
        classic=True,
        gameplay_roles=("dps",),
    ),
    WowClass(
        id="druid",
        name="Druid",
        color=0xFF7C0A,
        emoji="🌿",
        classic=True,
        gameplay_roles=("dps", "healing", "tank"),
    ),
    WowClass(
        id="death_knight",
        name="Death Knight",
        color=0xC41E3A,
        emoji="💀",
        classic=False,
        gameplay_roles=("dps", "tank"),
    ),
    WowClass(
        id="monk",
        name="Monk",
        color=0x00FF98,
        emoji="🥋",
        classic=False,
        gameplay_roles=("dps", "healing", "tank"),
    ),
    WowClass(
        id="demon_hunter",
        name="Demon Hunter",
        color=0xA330C9,
        emoji="🦇",
        classic=False,
        gameplay_roles=("dps", "tank"),
    ),
    WowClass(
        id="evoker",
        name="Evoker",
        color=0x33937F,
        emoji="🐉",
        classic=False,
        gameplay_roles=("dps", "healing"),
    ),
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
