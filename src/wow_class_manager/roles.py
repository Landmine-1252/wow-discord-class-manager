"""Idempotent role reconciliation and shared role safety checks."""

import asyncio
from dataclasses import dataclass, field
from weakref import WeakValueDictionary

import discord

from .config import Settings
from .storage import Store


class UserError(RuntimeError):
    """Actionable, secret-free message safe to send to an interaction."""


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


def unsafe_reason(guild: discord.Guild, role: discord.Role) -> str | None:
    if role.is_default() or role.managed:
        return "the role is @everyone or belongs to another integration"
    if role.permissions.value:
        return "the role grants permissions; use a dedicated class role with all permissions disabled"
    if guild.me is None or not guild.me.guild_permissions.manage_roles:
        return "the bot needs Manage Roles"
    if role >= guild.me.top_role:
        return "move the bot's highest role above this class role in Server Settings → Roles"
    for channel in guild.channels:
        allow, _ = channel.overwrites_for(role).pair()
        if allow.value:
            return "the role grants channel access or permissions through an override; use a dedicated cosmetic role"
    return None


def api_problem(error: discord.HTTPException) -> str:
    if isinstance(error, discord.Forbidden):
        return "Discord denied access. Check Manage Roles, channel permissions and the bot's role position."
    if isinstance(error, discord.NotFound):
        return "A Discord resource was deleted. Run setup or sync again."
    return "Discord could not complete the request. Try again shortly; check the bot logs if it persists."


@dataclass(slots=True)
class ReconcileResult:
    created: int = 0
    reused: int = 0
    updated: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"Roles: {self.created} created, {self.reused} reused, {self.updated} updated."
        if self.warnings:
            text += "\n" + "\n".join(f"• {warning}" for warning in self.warnings)
        return text


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
            associations = await self.store.roles(guild.id)
            for entry in self.settings.classes:
                role = next(
                    (r for r in roles if r.id == associations.get(entry.id)), None
                )
                if role is None:
                    matches = [
                        r for r in roles if r.name.casefold() == entry.name.casefold()
                    ]
                    if len(matches) > 1:
                        result.warnings.append(
                            f"{entry.definition.name}: duplicate role names. Rename the unwanted matching roles, then run sync; no role was chosen."
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
                    if other:
                        result.warnings.append(
                            f"{entry.definition.name}: this role already belongs to another class, including possibly a disabled class. Choose a different role name."
                        )
                        continue
                    reason = unsafe_reason(guild, role)
                    if reason:
                        result.warnings.append(f"{entry.definition.name}: {reason}.")
                        continue
                    if any(
                        r.id != role.id and r.name.casefold() == entry.name.casefold()
                        for r in roles
                    ):
                        result.warnings.append(
                            f"{entry.definition.name}: another role has the configured name. Rename the conflicting role before syncing."
                        )
                        continue
                try:
                    if role is None:
                        if not self.settings.auto_create_roles:
                            result.warnings.append(
                                f"{entry.definition.name}: missing; enable AUTO_CREATE_ROLES or create a safe matching role."
                            )
                            continue
                        role = await guild.create_role(
                            name=entry.name,
                            colour=discord.Colour(entry.color),
                            permissions=discord.Permissions.none(),
                            hoist=False,
                            mentionable=False,
                            reason="WoW class role setup",
                        )
                        roles.append(role)
                        # Persist immediately: subsequent retries reuse this ID.
                        await self.store.associate(guild.id, entry.id, role.id)
                        associations[entry.id] = role.id
                        result.created += 1
                    else:
                        await self.store.associate(guild.id, entry.id, role.id)
                        associations[entry.id] = role.id
                        result.reused += 1
                        if role.name != entry.name or role.colour.value != entry.color:
                            edited = await role.edit(
                                name=entry.name,
                                colour=discord.Colour(entry.color),
                                reason="WoW class role configuration update",
                            )
                            roles[roles.index(role)] = edited
                            result.updated += 1
                except discord.HTTPException as error:
                    result.warnings.append(
                        f"{entry.definition.name}: {api_problem(error)}"
                    )
        return result
