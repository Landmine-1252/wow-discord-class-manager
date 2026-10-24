"""Per-member transactions using only Discord's single-role PUT/DELETE APIs."""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

import aiohttp
import discord

from .character_roles import validate_gameplay_role
from .character_store import CharacterStore
from .config import Settings
from .roles import Locks, UserError, api_problem, require_guild, unsafe_reason
from .storage import Store

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RolePlan:
    member: discord.Member
    roles: dict[int, discord.Role]
    previous: set[int]
    wanted: set[int]
    managed_ids: set[int]
    class_roles: dict[str, int]
    gameplay_roles: dict[str, int] = field(default_factory=dict)


class MemberManager:
    def __init__(self, settings: Settings, store: Store, locks: Locks):
        self.settings, self.store, self.locks = settings, store, locks
        self._cooldowns: dict[tuple[int, int], float] = {}

    async def current(self, guild: discord.Guild, member_id: int) -> str:
        require_guild(self.settings, guild)
        member = await guild.fetch_member(member_id)
        mapping = await self.store.roles(guild.id)
        held = {role.id for role in member.roles}
        names = [
            entry.definition.name
            for entry in self.settings.classes
            if mapping.get(entry.id) in held
        ]
        return (
            "Your current class: " + ", ".join(names) + "."
            if names
            else "You have no enabled class selected."
        )

    def check_cooldown(self, guild_id: int, member_id: int) -> None:
        now = time.monotonic()
        self._cooldowns = {
            key: deadline for key, deadline in self._cooldowns.items() if deadline > now
        }
        key = (guild_id, member_id)
        if key in self._cooldowns:
            raise UserError(
                f"Please wait {self._cooldowns[key] - now:.0f} seconds before changing"
                " class again."
            )
        self._cooldowns[key] = now + self.settings.role_change_cooldown

    async def change(
        self, guild: discord.Guild, member_id: int, class_id: str | None
    ) -> str:
        require_guild(self.settings, guild)
        async with self.locks.member(guild.id, member_id):
            if self.settings.enable_character_management:
                characters = CharacterStore(self.store)
                roster = await characters.roster(guild.id, member_id)
                if roster.characters or await characters.pending(guild.id, member_id):
                    raise UserError(
                        "Your registered main determines your class. Use"
                        f" /{self.settings.character_command_root} main or Manage"
                        " Characters to select or register a main."
                    )
            self.check_cooldown(guild.id, member_id)
            plan = await self.plan_locked(
                guild,
                member_id,
                class_id,
                allow_clear=self.settings.allow_clear_selection,
            )
            unchanged = plan.previous == plan.wanted
            await self.apply_locked(guild, member_id, plan)
            if class_id is None:
                return (
                    "You have no enabled class selected."
                    if unchanged
                    else "Your class selection has been cleared."
                )
            entry = next(
                entry for entry in self.settings.classes if entry.id == class_id
            )
            return (
                f"Your class is already {entry.definition.name}."
                if unchanged
                else f"Your class has been updated to {entry.definition.name}."
            )

    async def plan_locked(
        self,
        guild: discord.Guild,
        member_id: int,
        class_id: str | None,
        *,
        allow_clear: bool = False,
        include_disabled: bool = False,
        sync_class: bool = True,
        sync_gameplay: bool = False,
        gameplay_role: str | None = None,
    ) -> RolePlan:
        """Read/validate only. Caller holds the shared per-member lock."""
        require_guild(self.settings, guild)
        enabled = {entry.id for entry in self.settings.classes}
        if sync_class and class_id is None and not allow_clear:
            raise UserError("Clearing your class is disabled by the bot operator.")
        if sync_class and class_id is not None and class_id not in enabled:
            raise UserError(
                "That class is disabled or unknown. Choose an enabled class."
            )
        if sync_gameplay:
            if not self.settings.enable_character_management:
                raise UserError("Character management is disabled by the bot operator.")
            if gameplay_role is not None:
                validate_gameplay_role(class_id, gameplay_role)
        async with self.locks.guild(guild.id):
            mapping = await self.store.roles(guild.id)
            tag_mapping = await self.store.gameplay_roles(guild.id)
        if set(mapping.values()) & set(tag_mapping.values()):
            raise UserError(
                "Class roles and gameplay tags have conflicting associations. Ask an"
                " officer to check role setup."
            )
        roles = {role.id: role for role in await guild.fetch_roles()}
        member = await guild.fetch_member(member_id)
        if member.bot:
            raise UserError("Class selection is available to human server members.")
        held = {role.id for role in member.roles}
        if not sync_class:
            active_ids = set()
        elif include_disabled:
            active_ids = set(mapping.values())
        else:
            active_ids = {mapping[cid] for cid in enabled if cid in mapping}
        if sync_gameplay:
            active_ids.update(tag_mapping.values())
        previous = active_ids & held
        target = mapping.get(class_id) if sync_class and class_id is not None else None
        if (
            sync_class
            and class_id is not None
            and (target is None or target not in roles)
        ):
            raise UserError(
                "This class role is not ready. Ask an officer to run"
                f" {self.settings.command('sync')}."
            )
        wanted = {target} if target is not None else set()
        if sync_gameplay and gameplay_role is not None:
            tag = tag_mapping.get(gameplay_role)
            if tag is None or tag not in roles:
                raise UserError(
                    "The gameplay role tag is not ready. Ask an officer to run"
                    f" {self.settings.command('sync')}."
                )
            if roles[tag].colour.value:
                raise UserError(
                    "The gameplay role tag must use the default color so your class"
                    " color is preserved. Ask an officer to run sync."
                )
            wanted.add(tag)
        for rid in previous | wanted:
            if rid not in roles:
                raise UserError(
                    "A managed role was deleted. Ask an officer to run sync."
                )
            reason = unsafe_reason(guild, roles[rid])
            if reason:
                raise UserError(f"The class change is unavailable: {reason}.")
        return RolePlan(
            member,
            roles,
            previous,
            wanted,
            active_ids,
            mapping if sync_class else {},
            tag_mapping if sync_gameplay else {},
        )

    async def _checked_role(
        self, guild: discord.Guild, role_id: int, *, neutral: bool = False
    ) -> discord.Role:
        """Refresh safety after journal I/O and before each targeted Discord write."""
        role = next(
            (role for role in await guild.fetch_roles() if role.id == role_id), None
        )
        if role is None:
            raise UserError("A managed role was deleted. Ask an officer to run sync.")
        reason = unsafe_reason(guild, role)
        if reason:
            raise UserError(f"The role change is unavailable: {reason}.")
        if neutral and role.colour.value:
            raise UserError(
                "The gameplay role tag must use the default color. Ask an officer to"
                " run sync."
            )
        return role

    async def apply_locked(
        self,
        guild: discord.Guild,
        member_id: int,
        plan: RolePlan,
        *,
        recover: bool = True,
    ) -> None:
        """One shared targeted role implementation for legacy and character flows."""
        if plan.previous == plan.wanted:
            return
        try:
            for rid in sorted(plan.wanted - plan.previous):
                role = await self._checked_role(
                    guild, rid, neutral=rid in plan.gameplay_roles.values()
                )
                await plan.member.add_roles(
                    role,
                    atomic=True,
                    reason="Member selected WoW main character roles",
                )
            for rid in sorted(plan.previous - plan.wanted):
                role = await self._checked_role(guild, rid)
                await plan.member.remove_roles(
                    role,
                    atomic=True,
                    reason="Member changed WoW main character roles",
                )
            confirmed = await guild.fetch_member(member_id)
            if {role.id for role in confirmed.roles} & plan.managed_ids != plan.wanted:
                raise UserError(
                    "Discord did not confirm the expected character roles; another role"
                    " change may have occurred."
                )
        except asyncio.CancelledError:
            if recover:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(8):
                        await self._restore(
                            guild, member_id, plan.previous, plan.wanted
                        )
            raise
        except (
            discord.DiscordException,
            aiohttp.ClientError,
            UserError,
            OSError,
            TimeoutError,
        ) as error:
            if not recover:
                raise
            restored = await self._restore(guild, member_id, plan.previous, plan.wanted)
            detail = (
                api_problem(error)
                if isinstance(error, discord.HTTPException)
                else "Discord could not complete the role update."
            )
            recovery = (
                "Your previous class roles were restored."
                if restored
                else (
                    "The previous state could not be confirmed. Ask an officer to check"
                    " your class roles before retrying."
                )
            )
            raise UserError(
                f"The class change was not completed. {detail} {recovery}"
            ) from None

    async def _restore(
        self,
        guild: discord.Guild,
        member_id: int,
        previous: set[int],
        wanted: set[int],
    ) -> bool:
        try:
            member = await guild.fetch_member(member_id)
            tag_mapping = await self.store.gameplay_roles(guild.id)
            held = {role.id for role in member.roles}
            # Recheck safety: never restore a role that became privileged.
            for rid in previous - held:
                role = await self._checked_role(
                    guild, rid, neutral=rid in tag_mapping.values()
                )
                await member.add_roles(
                    role,
                    atomic=True,
                    reason="Recover interrupted character role change",
                )
            for rid in (wanted - previous) & held:
                role = await self._checked_role(guild, rid)
                await member.remove_roles(
                    role,
                    atomic=True,
                    reason="Recover interrupted character role change",
                )
            confirmed = await guild.fetch_member(member_id)
            return {role.id for role in confirmed.roles} & (
                previous | wanted
            ) == previous
        except (
            discord.DiscordException,
            aiohttp.ClientError,
            UserError,
            OSError,
            TimeoutError,
        ):
            log.warning("member_role_recovery_failed guild_id=%s", guild.id)
            return False
