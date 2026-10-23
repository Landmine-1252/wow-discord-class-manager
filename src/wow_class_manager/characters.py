"""Character policy and recoverable coordination with the existing role service.

Each mutation is serialized with legacy class changes by the same member lock.
The journal is committed before Discord writes; the old roster remains authoritative
until Discord confirms the target roles and a short SQLite transaction commits.
"""

import asyncio
import logging
from uuid import uuid4

import discord

from .character_names import validate_names
from .character_store import Character, CharacterStore, Operation, Roster
from .classes import REGISTRY
from .config import Settings
from .members import MemberManager, RolePlan
from .roles import Locks, UserError, api_problem, require_guild
from .storage import StorageError, Store

log = logging.getLogger(__name__)


def character_label(character: Character) -> str:
    return discord.utils.escape_markdown(
        discord.utils.escape_mentions(character.full_name)
    )[:160]


class CharacterManager:
    def __init__(
        self, settings: Settings, store: Store, locks: Locks, members: MemberManager
    ):
        self.settings, self.store, self.locks, self.members = (
            settings,
            store,
            locks,
            members,
        )
        self.records = CharacterStore(store)

    def _require(self, guild: discord.Guild) -> None:
        require_guild(self.settings, guild)
        if not self.settings.enable_character_management:
            raise UserError("Character management is disabled by the bot operator.")

    def _enabled(self, class_id: str) -> None:
        if class_id not in {entry.id for entry in self.settings.classes}:
            raise UserError(
                "That character's class is disabled or unknown. Your characters are preserved; edit or choose a main with an enabled class."
            )

    def _promotable(self, character: Character) -> None:
        if character.needs_completion:
            raise UserError(
                "Complete this character's Primary Name and Secondary Name using Edit Character before selecting it as main. No name has been guessed."
            )
        self._enabled(character.class_id)

    async def _role_plan(
        self, guild: discord.Guild, user_id: int, class_id: str | None
    ) -> RolePlan:
        return await self.members.plan_locked(
            guild, user_id, class_id, allow_clear=True, include_disabled=True
        )

    async def _sync_current_locked(
        self, guild: discord.Guild, user_id: int, roster: Roster
    ) -> None:
        if roster.main is not None:
            self._enabled(roster.main.class_id)
            plan = await self._role_plan(guild, user_id, roster.main.class_id)
            await self.members.apply_locked(guild, user_id, plan)

    async def _recover_locked(self, guild: discord.Guild, user_id: int) -> None:
        pending = await self.records.pending(guild.id, user_id)
        if pending is None:
            return
        roster = await self.records.roster(guild.id, user_id)
        try:
            if pending.previous_roles != pending.wanted_roles:
                if roster.main and roster.main.class_id in {
                    entry.id for entry in self.settings.classes
                }:
                    # Role IDs may have been recreated since the interruption.
                    # The unchanged durable main remains the authority.
                    plan = await self._role_plan(guild, user_id, roster.main.class_id)
                    await self.members.apply_locked(guild, user_id, plan)
                else:
                    previous, wanted = await self._recovery_roles(guild.id, pending)
                    if not await self.members._restore(
                        guild, user_id, previous, wanted
                    ):
                        raise UserError(
                            "The previous class roles could not be restored."
                        )
            await self.records.abort(guild.id, user_id, pending.op_id)
        except Exception as error:
            detail = (
                str(error)
                if isinstance(error, (UserError, StorageError))
                else "Discord is unavailable or denied access."
            )
            raise UserError(
                f"An interrupted character change still needs recovery. {detail} Ask an officer to check permissions and run {self.settings.command('sync')}, then open Manage Characters again. Your stored characters and main have been retained."
            ) from None

    async def _recovery_roles(
        self, guild_id: int, operation: Operation
    ) -> tuple[set[int], set[int]]:
        mapping = await self.store.roles(guild_id)

        def remap(snapshot: frozenset[int], key: str) -> set[int]:
            result = set(snapshot)
            for class_id, old_id in operation.payload.get(key, {}).items():
                if old_id in result and class_id in mapping:
                    result.remove(old_id)
                    result.add(mapping[class_id])
            return result

        return remap(operation.previous_roles, "_previous_classes"), remap(
            operation.wanted_roles, "_wanted_classes"
        )

    async def _compensate_locked(
        self, guild: discord.Guild, user_id: int, op_id: str
    ) -> str:
        """Return restored/pending/committed; never undo an acknowledged DB commit."""
        try:
            async with asyncio.timeout(8):
                pending = await self.records.pending(guild.id, user_id)
                if pending is None:
                    # Commit can finish in the SQLite worker while this task is
                    # cancelled. Absence of the journal means do not undo roles.
                    return "committed"
                if pending.op_id != op_id:
                    return "pending"
                if pending.previous_roles != pending.wanted_roles:
                    previous, wanted = await self._recovery_roles(guild.id, pending)
                    if not await self.members._restore(
                        guild,
                        user_id,
                        previous,
                        wanted,
                    ):
                        return "pending"
                await self.records.abort(guild.id, user_id, op_id)
                return "restored"
        except Exception as error:
            log.warning(
                "character_recovery_pending guild_id=%s error_type=%s",
                guild.id,
                type(error).__name__,
            )
            return "pending"

    async def _commit_locked(
        self, guild: discord.Guild, user_id: int, payload: dict, plan: RolePlan | None
    ) -> Roster:
        operation_id = uuid4().hex
        if plan:
            payload = {
                **payload,
                "_previous_classes": {
                    cid: rid
                    for cid, rid in plan.class_roles.items()
                    if rid in plan.previous
                },
                "_wanted_classes": {
                    cid: rid
                    for cid, rid in plan.class_roles.items()
                    if rid in plan.wanted
                },
            }
        prepared = False
        try:
            await self.records.prepare(
                guild.id,
                user_id,
                operation_id,
                payload,
                plan.previous if plan else set(),
                plan.wanted if plan else set(),
            )
            prepared = True
            if plan is not None:
                await self.members.apply_locked(guild, user_id, plan, recover=False)
            return await self.records.commit(guild.id, user_id, operation_id)
        except asyncio.CancelledError:
            await self._compensate_locked(guild, user_id, operation_id)
            raise
        except Exception as error:
            if not prepared and isinstance(error, UserError):
                raise
            state = await self._compensate_locked(guild, user_id, operation_id)
            if state == "restored":
                recovery = "Your previous roster and main were kept, and the prior class roles were restored."
            elif state == "committed":
                recovery = "Reopen Manage Characters to check the recorded result before retrying."
            else:
                recovery = "Recovery is pending. Reopen Manage Characters to reconcile; ask an officer to check role permissions if it cannot recover."
            detail = (
                str(error)
                if isinstance(error, (UserError, StorageError))
                else (
                    api_problem(error)
                    if isinstance(error, discord.HTTPException)
                    else "The database or Discord request could not be completed."
                )
            )
            raise UserError(
                f"The character change could not be confirmed. {detail} {recovery}"
            ) from None

    async def roster(
        self, guild: discord.Guild, user_id: int, reconcile: bool = True
    ) -> Roster:
        self._require(guild)
        async with self.locks.member(guild.id, user_id):
            if reconcile:
                await self._recover_locked(guild, user_id)
            roster = await self.records.roster(guild.id, user_id)
            if reconcile:
                await self._sync_current_locked(guild, user_id, roster)
            return roster

    async def add(
        self,
        guild: discord.Guild,
        user_id: int,
        primary_name: str,
        secondary_name: str,
        class_id: str,
    ) -> str:
        self._require(guild)
        self._enabled(class_id)
        primary, secondary, key = validate_names(
            self.settings, primary_name, secondary_name
        )
        async with self.locks.member(guild.id, user_id):
            await self._recover_locked(guild, user_id)
            before = await self.records.roster(guild.id, user_id)
            if before.characters and before.main is None:
                raise UserError(
                    "Your imported roster has no confirmed main. Complete a character's names and select it as main before adding more characters."
                )
            self.members.check_cooldown(guild.id, user_id)
            if before.main and before.main.class_id in {
                entry.id for entry in self.settings.classes
            }:
                await self._sync_current_locked(guild, user_id, before)
            plan = (
                await self._role_plan(guild, user_id, class_id)
                if not before.characters
                else None
            )
            await self._commit_locked(
                guild,
                user_id,
                {
                    "kind": "add",
                    "primary_name": primary,
                    "secondary_name": secondary,
                    "name_key": key,
                    "class_id": class_id,
                    "max_characters": self.settings.max_characters_per_user,
                },
                plan,
            )
            full_name = f"{primary} {secondary}"
            return (
                f"{full_name} has been registered as your main {REGISTRY[class_id].name}."
                if not before.characters
                else f"{full_name} has been added as a {REGISTRY[class_id].name} alt."
            )

    async def promote(
        self, guild: discord.Guild, user_id: int, character_id: int
    ) -> str:
        self._require(guild)
        async with self.locks.member(guild.id, user_id):
            await self._recover_locked(guild, user_id)
            selected = await self.records.character(guild.id, user_id, character_id)
            self._promotable(selected)
            before = await self.records.roster(guild.id, user_id)
            plan = await self._role_plan(guild, user_id, selected.class_id)
            if selected.is_main:
                await self.members.apply_locked(guild, user_id, plan)
                return f"{character_label(selected)} is already your main character."
            self.members.check_cooldown(guild.id, user_id)
            await self._commit_locked(
                guild, user_id, {"kind": "promote", "character_id": character_id}, plan
            )
            demotion = (
                f" {character_label(before.main)} is now an alt." if before.main else ""
            )
            return f"Your main is now {character_label(selected)} ({REGISTRY[selected.class_id].name}).{demotion} Your Discord class role is synchronized."

    async def edit(
        self,
        guild: discord.Guild,
        user_id: int,
        character_id: int,
        primary_name: str,
        secondary_name: str,
        class_id: str,
        *,
        expected_updated_at: str | None = None,
    ) -> str:
        self._require(guild)
        primary, secondary, key = validate_names(
            self.settings, primary_name, secondary_name
        )
        async with self.locks.member(guild.id, user_id):
            await self._recover_locked(guild, user_id)
            selected = await self.records.character(guild.id, user_id, character_id)
            if (
                expected_updated_at is not None
                and selected.updated_at != expected_updated_at
            ):
                raise UserError(
                    "This character changed while the edit form was open. Reopen Edit Character to use the latest details; your newer changes were kept."
                )
            if class_id != selected.class_id:
                if not self.settings.allow_character_class_edit:
                    raise UserError(
                        "Character class editing is disabled; you can still edit both name fields."
                    )
                self._enabled(class_id)
            self.members.check_cooldown(guild.id, user_id)
            before = await self.records.roster(guild.id, user_id)
            plan = None
            if selected.is_main and class_id in {
                entry.id for entry in self.settings.classes
            }:
                plan = await self._role_plan(guild, user_id, class_id)
            elif before.main and before.main.class_id in {
                entry.id for entry in self.settings.classes
            }:
                await self._sync_current_locked(guild, user_id, before)
            await self._commit_locked(
                guild,
                user_id,
                {
                    "kind": "edit",
                    "character_id": character_id,
                    "primary_name": primary,
                    "secondary_name": secondary,
                    "name_key": key,
                    "class_id": class_id,
                },
                plan,
            )
            note = " Choose your main with Change Main." if before.main is None else ""
            return f"{primary} {secondary} has been updated.{note}"

    async def remove(
        self,
        guild: discord.Guild,
        user_id: int,
        character_id: int,
        replacement_id: int | None = None,
    ) -> str:
        self._require(guild)
        async with self.locks.member(guild.id, user_id):
            await self._recover_locked(guild, user_id)
            selected = await self.records.character(guild.id, user_id, character_id)
            before = await self.records.roster(guild.id, user_id)
            plan = None
            replacement = None
            if selected.is_main:
                if len(before.characters) > 1:
                    if replacement_id is None or replacement_id == character_id:
                        raise UserError(
                            "Choose a replacement main before removing this character."
                        )
                    replacement = await self.records.character(
                        guild.id, user_id, replacement_id
                    )
                    self._promotable(replacement)
                    plan = await self._role_plan(guild, user_id, replacement.class_id)
                else:
                    if not self.settings.allow_clear_selection:
                        raise UserError(
                            "Removing your last character is disabled. The operator can enable ALLOW_CLEAR_SELECTION."
                        )
                    plan = await self._role_plan(guild, user_id, None)
            elif replacement_id is not None:
                raise UserError(
                    "A replacement main is only needed when removing the current main. Refresh your roster."
                )
            elif before.main and before.main.class_id in {
                entry.id for entry in self.settings.classes
            }:
                await self._sync_current_locked(guild, user_id, before)
            self.members.check_cooldown(guild.id, user_id)
            await self._commit_locked(
                guild,
                user_id,
                {
                    "kind": "remove",
                    "character_id": character_id,
                    "replacement_id": replacement_id,
                },
                plan,
            )
            suffix = (
                f" {character_label(replacement)} is now your main."
                if replacement
                else ""
            )
            return f"{character_label(selected)} has been removed.{suffix}"

    async def purge(
        self, guild: discord.Guild, actor: discord.Member, target_user_id: int
    ) -> str:
        self._require(guild)
        if (
            not isinstance(actor, discord.Member)
            or actor.guild.id != guild.id
            or not actor.guild_permissions.manage_guild
            or not actor.guild_permissions.manage_roles
        ):
            raise UserError(
                "You need Manage Server and Manage Roles in this server to erase stored character data."
            )
        async with self.locks.member(guild.id, target_user_id):
            await self.records.erase(guild.id, target_user_id)
        return "The member's stored character data and pending recovery records in this server have been deleted. Discord roles were left unchanged."

    async def guild_roster(
        self,
        guild: discord.Guild,
        requester: discord.Member,
        user_id: int | None = None,
        class_id: str | None = None,
        kind: str = "all",
    ) -> list[Character]:
        self._require(guild)
        if (
            not isinstance(requester, discord.Member)
            or requester.guild.id != guild.id
            or requester.bot
        ):
            raise UserError("You must be a member of this server to view its roster.")
        if (
            not self.settings.enable_guild_roster
            or self.settings.roster_visibility == "disabled"
        ):
            raise UserError(
                "The guild roster is disabled. You can still privately view your own characters."
            )
        if (
            self.settings.roster_visibility == "admins"
            and not requester.guild_permissions.manage_guild
        ):
            raise UserError("You need Manage Server to view the guild roster.")
        if class_id is not None and class_id not in REGISTRY:
            raise UserError("Choose a recognized class for the roster filter.")
        return await self.records.list_guild(guild.id, user_id, class_id, kind)
