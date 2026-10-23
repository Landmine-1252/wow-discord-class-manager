"""Validate environment configuration before making any network requests."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from string import Formatter

from .classes import DEFAULT_CLASSES, REGISTRY, ClassRole

SUBCOMMANDS = ("setup", "sync", "status", "set", "me", "clear", "help")
PANEL_DESCRIPTION = (
    "Select your primary World of Warcraft class. Your Discord name will "
    "automatically use your class color. You can change your selection at any time."
)
CHARACTER_PANEL_DESCRIPTION = (
    "Register your main character and alts, manage your characters, and change "
    "mains whenever you want. Your main character determines your Discord class color."
)


class ConfigError(ValueError):
    """Safe to display: messages name settings, never their supplied values."""


def color(value: str, key: str) -> int:
    if not re.fullmatch(r"#?[0-9a-fA-F]{6}", value):
        raise ConfigError(f"{key} must be a six-digit hex color, such as #3FC7EB.")
    return int(value.removeprefix("#"), 16)


def snowflake(value: str, key: str) -> int:
    if not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**63:
        raise ConfigError(f"{key} must be a positive Discord ID (digits only).")
    return int(value)


@dataclass(frozen=True, slots=True)
class Settings:
    token: str = field(repr=False)
    classes: tuple[ClassRole, ...]
    application_id: int | None = None
    allowed_guild_ids: frozenset[int] = frozenset()
    development_guild_id: int | None = None
    command_sync_scope: str = "global"
    auto_create_roles: bool = True
    root_command: str = "class"
    command_names: tuple[str, ...] = SUBCOMMANDS
    panel_title: str = "Choose Your Class"
    panel_description: str = CHARACTER_PANEL_DESCRIPTION
    panel_placeholder: str = "Select your class..."
    panel_footer: str = ""
    panel_accent_color: int = 0x5865F2
    allow_clear_selection: bool = False
    role_change_cooldown: float = 3.0
    log_level: str = "INFO"
    admin_webhook_url: str | None = field(default=None, repr=False)
    database_path: Path = Path("/data/classes.sqlite3")
    health_path: Path = Path("/tmp/wow-class-manager-health.json")
    enable_character_management: bool = True
    character_command_root: str = "character"
    max_characters_per_user: int = 0
    character_primary_name_min_length: int = 2
    character_primary_name_max_length: int = 12
    character_secondary_name_min_length: int = 2
    character_secondary_name_max_length: int = 12
    enable_guild_roster: bool = True
    roster_visibility: str = "members"
    character_panel_title: str = "World of Warcraft Character Manager"
    allow_character_class_edit: bool = True

    def allowed(self, guild_id: int) -> bool:
        return not self.allowed_guild_ids or guild_id in self.allowed_guild_ids

    def command(self, name: str) -> str:
        return f"/{self.root_command} {self.command_names[SUBCOMMANDS.index(name)]}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env

        def get(key: str, default: str = "") -> str:
            return e.get(key, default).strip()

        def boolean(key: str, default: bool) -> bool:
            value = get(key, str(default)).lower()
            if value not in {"true", "false", "1", "0"}:
                raise ConfigError(f"{key} must be true or false.")
            return value in {"true", "1"}

        def identifier(key: str) -> int | None:
            value = get(key)
            return snowflake(value, key) if value else None

        def integer(key: str, default: int, minimum: int, maximum: int) -> int:
            value = get(key, str(default))
            if (
                len(value) > len(str(maximum))
                or not re.fullmatch(r"[0-9]+", value)
                or not minimum <= int(value) <= maximum
            ):
                raise ConfigError(
                    f"{key} must be an integer from {minimum} to {maximum}."
                )
            return int(value)

        def text(key: str, default: str, limit: int, empty: bool = False) -> str:
            value = get(key, default)
            if (not value and not empty) or len(value) > limit or "\x00" in value:
                raise ConfigError(
                    f"{key} must contain {'0' if empty else '1'}–{limit} characters without NUL."
                )
            return value

        token, token_file = get("DISCORD_BOT_TOKEN"), get("DISCORD_BOT_TOKEN_FILE")
        if bool(token) == bool(token_file):
            raise ConfigError(
                "Configure exactly one of DISCORD_BOT_TOKEN or DISCORD_BOT_TOKEN_FILE."
            )
        if token_file:
            try:
                with Path(token_file).open(encoding="utf-8") as handle:
                    token = handle.read(4097).strip()
            except (OSError, UnicodeError):
                raise ConfigError(
                    "DISCORD_BOT_TOKEN_FILE could not be read; check the mount and permissions."
                ) from None
        if (
            not re.fullmatch(
                r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}", token
            )
            or len(token) > 512
        ):
            raise ConfigError(
                "The Discord bot token format is invalid. Copy the token from the Developer Portal's Bot page, without a Bot prefix."
            )

        enabled = tuple(
            part.strip().lower()
            for part in get("ENABLED_CLASSES", ",".join(DEFAULT_CLASSES)).split(",")
        )
        if (
            not enabled
            or any(part not in REGISTRY for part in enabled)
            or len(set(enabled)) != len(enabled)
        ):
            raise ConfigError(
                "ENABLED_CLASSES must contain unique, comma-separated IDs from the class registry; see docs/configuration.md."
            )
        template = get("ROLE_NAME_TEMPLATE", "{name}")
        try:
            fields = list(Formatter().parse(template))
            if any(
                name not in {None, "name"} or spec or conversion
                for _, name, spec, conversion in fields
            ):
                raise ValueError
            names = {
                key: template.format(name=entry.name) for key, entry in REGISTRY.items()
            }
        except (ValueError, KeyError, IndexError):
            raise ConfigError(
                "ROLE_NAME_TEMPLATE supports only the {name} placeholder, with no formatting or conversions."
            ) from None
        configured = {}
        for key, entry in REGISTRY.items():
            name_key = f"ROLE_NAME_{key.upper()}"
            color_key = f"ROLE_COLOR_{key.upper()}"
            name = text(name_key, names[key], 100)
            configured[key] = ClassRole(
                entry, name, color(get(color_key, f"#{entry.color:06X}"), color_key)
            )
        if len({item.name.casefold() for item in configured.values()}) != len(
            configured
        ):
            raise ConfigError(
                "Configured role names must be unique across all classes (case-insensitive)."
            )
        known_overrides = {
            f"ROLE_{kind}_{key.upper()}"
            for kind in ("NAME", "COLOR")
            for key in REGISTRY
        } | {"ROLE_NAME_TEMPLATE"}
        if any(
            key.startswith(("ROLE_NAME_", "ROLE_COLOR_")) and key not in known_overrides
            for key in e
        ):
            raise ConfigError(
                "An unsupported ROLE_NAME_ or ROLE_COLOR_ setting was supplied; check the class IDs."
            )

        command_keys = ["SLASH_COMMAND_ROOT"] + [
            f"SLASH_{name.upper()}_NAME" for name in SUBCOMMANDS
        ]
        commands = tuple(
            get(key, default)
            for key, default in zip(command_keys, ("class", *SUBCOMMANDS), strict=True)
        )
        for key, value in zip(command_keys, commands, strict=True):
            if not re.fullmatch(r"[a-z0-9_-]{1,32}", value):
                raise ConfigError(
                    f"{key} must use 1–32 lowercase ASCII letters, digits, underscores or hyphens."
                )
        if len(set(commands[1:])) != len(SUBCOMMANDS):
            raise ConfigError("Slash subcommand names must be unique.")
        characters_enabled = boolean("ENABLE_CHARACTER_MANAGEMENT", True)
        roster_enabled = boolean("ENABLE_GUILD_ROSTER", True)
        character_root = get("CHARACTER_COMMAND_ROOT", "character")
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", character_root):
            raise ConfigError(
                "CHARACTER_COMMAND_ROOT must use 1–32 lowercase ASCII letters, digits, underscores or hyphens."
            )
        visibility = get("ROSTER_VISIBILITY", "members")
        if visibility not in {"members", "admins", "disabled"}:
            raise ConfigError("ROSTER_VISIBILITY must be members, admins or disabled.")
        if characters_enabled and character_root == commands[0]:
            raise ConfigError(
                "CHARACTER_COMMAND_ROOT and SLASH_COMMAND_ROOT must be distinct."
            )
        if (
            characters_enabled
            and roster_enabled
            and visibility != "disabled"
            and "roster" in {commands[0], character_root}
        ):
            raise ConfigError(
                "The roster command name is reserved while the guild roster is enabled."
            )
        name_limits = {}
        for component in ("PRIMARY", "SECONDARY"):
            minimum_key = f"CHARACTER_{component}_NAME_MIN_LENGTH"
            maximum_key = f"CHARACTER_{component}_NAME_MAX_LENGTH"
            minimum = integer(minimum_key, 2, 1, 50)
            maximum = integer(maximum_key, 12, 1, 50)
            if minimum > maximum:
                raise ConfigError(f"{minimum_key} must not exceed {maximum_key}.")
            name_limits[component.lower()] = (minimum, maximum)
        allowed = frozenset(
            snowflake(value.strip(), "ALLOWED_GUILD_IDS")
            for value in get("ALLOWED_GUILD_IDS").split(",")
            if value.strip()
        )
        dev_guild = identifier("DEVELOPMENT_GUILD_ID")
        scope = get("COMMAND_SYNC_SCOPE", "global")
        if scope not in {"global", "guild"} or (scope == "guild" and dev_guild is None):
            raise ConfigError(
                "COMMAND_SYNC_SCOPE must be global or guild; guild requires DEVELOPMENT_GUILD_ID."
            )
        if dev_guild and allowed and dev_guild not in allowed:
            raise ConfigError(
                "DEVELOPMENT_GUILD_ID must be in ALLOWED_GUILD_IDS when an allowlist is configured."
            )
        try:
            cooldown = float(get("ROLE_CHANGE_COOLDOWN_SECONDS", "3"))
            if not math.isfinite(cooldown) or not 0 <= cooldown <= 3600:
                raise ValueError
        except ValueError:
            raise ConfigError(
                "ROLE_CHANGE_COOLDOWN_SECONDS must be a finite number between 0 and 3600."
            ) from None
        level = get("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError(
                "LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL."
            )
        webhook = get("ADMIN_WEBHOOK_URL") or None
        if webhook and not re.fullmatch(
            r"https://discord\.com/api/(?:v10/)?webhooks/[0-9]{1,20}/[A-Za-z0-9_-]{20,200}",
            webhook,
        ):
            raise ConfigError(
                "ADMIN_WEBHOOK_URL must be an HTTPS discord.com/api/webhooks/ID/TOKEN URL, without query parameters."
            )
        title = text("PANEL_TITLE", "Choose Your Class", 256)
        character_title = text(
            "CHARACTER_PANEL_TITLE", "World of Warcraft Character Manager", 256
        )
        description = text(
            "PANEL_DESCRIPTION",
            CHARACTER_PANEL_DESCRIPTION if characters_enabled else PANEL_DESCRIPTION,
            4096,
        )
        footer = text("PANEL_FOOTER", "", 2048, empty=True)
        active_title = character_title if characters_enabled else title
        if len(active_title) + len(description) + len(footer) > 6000:
            raise ConfigError(
                "The active panel title, PANEL_DESCRIPTION and PANEL_FOOTER together must not exceed 6000 characters."
            )
        database = Path(get("DATABASE_PATH", "/data/classes.sqlite3"))
        health = Path(get("HEALTH_PATH", "/tmp/wow-class-manager-health.json"))
        if not database.is_absolute() or not health.is_absolute() or database == health:
            raise ConfigError(
                "DATABASE_PATH and HEALTH_PATH must be distinct absolute file paths."
            )
        return cls(
            token=token,
            classes=tuple(configured[key] for key in enabled),
            application_id=identifier("DISCORD_APPLICATION_ID"),
            allowed_guild_ids=allowed,
            development_guild_id=dev_guild,
            command_sync_scope=scope,
            auto_create_roles=boolean("AUTO_CREATE_ROLES", True),
            root_command=commands[0],
            command_names=commands[1:],
            panel_title=title,
            panel_description=description,
            panel_footer=footer,
            panel_placeholder=text("PANEL_PLACEHOLDER", "Select your class...", 150),
            panel_accent_color=color(
                get("PANEL_ACCENT_COLOR", "#5865F2"), "PANEL_ACCENT_COLOR"
            ),
            allow_clear_selection=boolean("ALLOW_CLEAR_SELECTION", False),
            role_change_cooldown=cooldown,
            log_level=level,
            admin_webhook_url=webhook,
            database_path=database,
            health_path=health,
            enable_character_management=characters_enabled,
            character_command_root=character_root,
            max_characters_per_user=integer("MAX_CHARACTERS_PER_USER", 0, 0, 2**31 - 1),
            character_primary_name_min_length=name_limits["primary"][0],
            character_primary_name_max_length=name_limits["primary"][1],
            character_secondary_name_min_length=name_limits["secondary"][0],
            character_secondary_name_max_length=name_limits["secondary"][1],
            enable_guild_roster=roster_enabled,
            roster_visibility=visibility,
            character_panel_title=character_title,
            allow_character_class_edit=boolean("ALLOW_CHARACTER_CLASS_EDIT", True),
        )
