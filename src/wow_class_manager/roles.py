"""Idempotent role reconciliation and shared role safety checks."""

import asyncio
from dataclasses import dataclass, field
from weakref import WeakValueDictionary

import discord

from .classes import GAMEPLAY_ROLE_TAGS
from .config import Settings
from .storage import Store


class UserError(RuntimeError):
    """Actionable, secret-free message safe to send to an interaction."""

    def __init__(self, message: str, *, code: str = "request_rejected") -> None:
        super().__init__(message)
        self.code = code


class Locks:
    def __init__(self) -> None:
        self._guilds: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self._members: WeakValueDictionary[tuple[int, int], asyncio.Lock] = (
            WeakValueDictionary()
        )
        self._panels: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

    def guild(self, guild_id: int) -> asyncio.Lock:
        return self._guilds.setdefault(guild_id, asyncio.Lock())

    def member(self, guild_id: int, member_id: int) -> asyncio.Lock:
        return self._members.setdefault((guild_id, member_id), asyncio.Lock())

    def panel(self, guild_id: int) -> asyncio.Lock:
        return self._panels.setdefault(guild_id, asyncio.Lock())


def require_guild(settings: Settings, guild: discord.Guild | None) -> discord.Guild:
    if guild is None:
        raise UserError("Use this command inside a server where the bot is installed.")
    if not settings.allowed(guild.id):
        raise UserError(
            "This server is not enabled in ALLOWED_GUILD_IDS. Contact the bot operator."
        )
    return guild


def require_bot_member(
    guild: discord.Guild, *, client: discord.Client | None = None
) -> None:
    # discord.py can fabricate guild.me for a commands-only installation. Require
    # Gateway membership for interactions and real guild role data in all paths.
    if (
        (client is not None and client.get_guild(guild.id) is None)
        or guild.me is None
        or guild.default_role is None
    ):
        raise UserError(
            "The bot's membership in this server is not available. An administrator"
            " should install this application using Guild Install with both bot and"
            " applications.commands scopes. If it was just invited, wait for it to"
            " connect and try again. The container token must belong to that same"
            " application.",
            code="bot_not_installed",
        )
    if guild.unavailable:
        raise UserError(
            "Discord reports this server temporarily unavailable. Wait for the bot to"
            " reconnect and try again.",
            code="guild_unavailable",
        )


def unsafe_reason(guild: discord.Guild, role: discord.Role) -> str | None:
    if role.is_default() or role.managed:
        return "the role is @everyone or belongs to another integration"
    if role.permissions.value:
        return (
            "the role grants permissions; use a dedicated class role with all"
            " permissions disabled"
        )
    if guild.me is None or not guild.me.guild_permissions.manage_roles:
        return "the bot needs Manage Roles"
    if role >= guild.me.top_role:
        return (
            "move the bot's highest role above this class role in Server Settings →"
            " Roles"
        )
    for channel in guild.channels:
        allow, _ = channel.overwrites_for(role).pair()
        if allow.value:
            return (
                "the role grants channel access or permissions through an override; use"
                " a dedicated cosmetic role"
            )
    return None


def api_problem(error: discord.HTTPException) -> str:
    # Use Discord's documented numeric codes, never its raw error text: the
    # latter can include request data or URLs containing interaction secrets.
    messages = {
        10002: (
            "Discord cannot find this application. Check that DISCORD_APPLICATION_ID,"
            " the bot token and the installation link belong to the same application."
        ),
        10003: (
            "Discord cannot find the selected channel. Choose an existing server text"
            " channel the bot can view, then run setup with that channel selected."
        ),
        10004: (
            "Discord cannot find this server for the bot. Install the same application"
            " using Guild Install with both bot and applications.commands scopes, and"
            " check that the container uses its bot token."
        ),
        10007: (
            "Discord cannot find that server member. Reopen the command from inside the"
            " server after confirming membership."
        ),
        10008: (
            "Discord cannot find that message. Reopen the character menu; if the public"
            " panel was deleted, ask an administrator to run setup again."
        ),
        10011: (
            "Discord cannot find a class role. Ask an administrator to run sync, then"
            " retry your selection."
        ),
        10015: (
            "Discord cannot find this response webhook. The interaction may have"
            " expired; reopen the menu or run the command again."
        ),
        10062: (
            "This interaction is no longer available or was not acknowledged in time."
            " Reopen the menu or run the command again."
        ),
        10063: (
            "Discord cannot find this application command. Reload Discord and wait for"
            " command registration to complete, then try again."
        ),
    }
    if error.code in messages:
        return f"{messages[error.code]} (Discord code {error.code}.)"
    if isinstance(error, discord.Forbidden):
        return (
            "Discord denied access. Check Manage Roles, channel permissions and the"
            " bot's role position."
        )
    if isinstance(error, discord.NotFound):
        return (
            "Discord could not find the requested resource. Retry the action; if it"
            " persists, ask an officer to check the bot logs for the Discord error"
            " code."
        )
    return (
        "Discord could not complete the request. Try again shortly; check the bot logs"
        " if it persists."
    )


@dataclass(slots=True)
class ReconcileResult:
    created: int = 0
    reused: int = 0
    updated: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = (
            f"Roles: {self.created} created, {self.reused} reused, {self.updated}"
            " updated."
        )
        if self.warnings:
            text += "\n" + "\n".join(f"• {warning}" for warning in self.warnings)
        return text


@dataclass(frozen=True, slots=True)
class ConfiguredRole:
    id: str
    name: str
    color: int
    label: str
    gameplay: bool = False


def configured_roles(settings: Settings) -> tuple[ConfiguredRole, ...]:
    classes = tuple(
        ConfiguredRole(entry.id, entry.name, entry.color, entry.definition.name)
        for entry in settings.classes
    )
    tags = (
        tuple(
            ConfiguredRole(key, name, 0, name, gameplay=True)
            for key, name in GAMEPLAY_ROLE_TAGS.items()
        )
        if settings.enable_character_management
        else ()
    )
    return classes + tags


class RoleManager:
    def __init__(self, settings: Settings, store: Store, locks: Locks):
        self.settings, self.store, self.locks = settings, store, locks

    async def reconcile(self, guild: discord.Guild) -> ReconcileResult:
        require_guild(self.settings, guild)
        result = ReconcileResult()
        async with self.locks.guild(guild.id):
            if guild.me is None or not guild.me.guild_permissions.manage_roles:
                result.warnings.append("Grant the bot Manage Roles, then run sync.")
                return result
            try:
                roles = list(await guild.fetch_roles())
            except discord.HTTPException as error:
                result.warnings.append(api_problem(error))
                return result
            class_associations = await self.store.roles(guild.id)
            gameplay_associations = await self.store.gameplay_roles(guild.id)
            entries = configured_roles(self.settings)
            for entry in entries:
                associations, other_associations = (
                    (gameplay_associations, class_associations)
                    if entry.gameplay
                    else (class_associations, gameplay_associations)
                )
                associate = (
                    self.store.associate_gameplay_role
                    if entry.gameplay
                    else self.store.associate
                )
                if (
                    sum(
                        item.name.casefold() == entry.name.casefold()
                        for item in entries
                    )
                    > 1
                ):
                    result.warnings.append(
                        f"{entry.label}: class roles and gameplay tags must have"
                        " distinct names. Change the class role name override, then"
                        " run sync."
                    )
                    continue
                role = next(
                    (r for r in roles if r.id == associations.get(entry.id)), None
                )
                if role is None:
                    matches = [
                        r for r in roles if r.name.casefold() == entry.name.casefold()
                    ]
                    if len(matches) > 1:
                        result.warnings.append(
                            f"{entry.label}: duplicate role names. Rename the unwanted"
                            " matching roles, then run sync; no role was chosen."
                        )
                        continue
                    role = matches[0] if matches else None
                if role is not None:
                    other = next(
                        (
                            key
                            for key, rid in associations.items()
                            if rid == role.id and key != entry.id
                        ),
                        None,
                    )
                    if other or role.id in other_associations.values():
                        result.warnings.append(
                            f"{entry.label}: this role already belongs to another class"
                            " or gameplay tag, including possibly a disabled class."
                            " Choose a different role name."
                        )
                        continue
                    reason = unsafe_reason(guild, role)
                    if reason:
                        result.warnings.append(f"{entry.label}: {reason}.")
                        continue
                    if any(
                        r.id != role.id and r.name.casefold() == entry.name.casefold()
                        for r in roles
                    ):
                        result.warnings.append(
                            f"{entry.label}: another role has the configured name."
                            " Rename the conflicting role before syncing."
                        )
                        continue
                try:
                    if role is None:
                        if not self.settings.auto_create_roles:
                            result.warnings.append(
                                f"{entry.label}: missing; enable AUTO_CREATE_ROLES or"
                                " create a safe matching role."
                            )
                            continue
                        role = await guild.create_role(
                            name=entry.name,
                            colour=discord.Colour(entry.color),
                            permissions=discord.Permissions.none(),
                            hoist=False,
                            mentionable=False,
                            reason="WoW character role setup",
                        )
                        roles.append(role)
                        # Persist immediately: subsequent retries reuse this ID.
                        await associate(guild.id, entry.id, role.id)
                        associations[entry.id] = role.id
                        result.created += 1
                    else:
                        await associate(guild.id, entry.id, role.id)
                        associations[entry.id] = role.id
                        result.reused += 1
                        if role.name != entry.name or role.colour.value != entry.color:
                            edited = await role.edit(
                                name=entry.name,
                                colour=discord.Colour(entry.color),
                                reason="WoW character role configuration update",
                            )
                            roles[roles.index(role)] = edited
                            result.updated += 1
                except discord.HTTPException as error:
                    result.warnings.append(f"{entry.label}: {api_problem(error)}")
        return result
