"""Private, owner-bound character workflows behind a persistent public panel."""

from __future__ import annotations

import asyncio
import logging
import math
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

import aiohttp
import discord

from .character_names import normalized_name_key, validate_names
from .character_roles import validate_gameplay_role
from .classes import GAMEPLAY_ROLE_TAGS, REGISTRY
from .roles import UserError, require_bot_member, require_guild
from .storage import StorageError

if TYPE_CHECKING:
    from .character_store import Character, Roster
    from .commands import Controller
    from .config import Settings

MANAGE_ID = "wow:character:manage:v1"
MY_CHARACTERS_ID = "wow:character:list:v1"
COMMUNITY_ROSTER_ID = "wow:character:roster:v1"
PAGE_SIZE = 15
PRIVATE_TIMEOUT = 300
log = logging.getLogger(__name__)

type SessionKey = tuple[int, int, int | None]
type CharacterDraft = tuple[str, str, str, str | None]

ROLE_DESCRIPTIONS = {
    "dps": "Deal damage",
    "healing": "Restore health and support your allies",
    "tank": "Protect your allies and hold the enemy's attention",
}


def safe_text(value: str, limit: int = 100) -> str:
    """Escape self-reported and legacy data without rewriting the saved value."""
    clean = "".join(
        "�" if unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} else char
        for char in value
    )
    escaped = "".join(
        "\\" + char if char in "\\`*_~|[]()<>#" else char
        for char in discord.utils.escape_mentions(clean)
    )
    return escaped if len(escaped) <= limit else escaped[: limit - 1] + "…"


def class_name(class_id: str) -> str:
    definition = REGISTRY.get(class_id)
    return definition.name if definition else "Unavailable class"


def class_emoji(class_id: str) -> str | None:
    definition = REGISTRY.get(class_id)
    return definition.emoji if definition else None


def class_color(settings: Settings, class_id: str | None) -> int:
    configured = next(
        (entry for entry in settings.classes if entry.id == class_id), None
    )
    if configured is not None:
        return configured.color
    definition = REGISTRY.get(class_id)
    return definition.color if definition else settings.panel_accent_color


def role_badge(gameplay_role: str | None) -> str:
    return GAMEPLAY_ROLE_TAGS.get(gameplay_role, "Role not set")


def page_slice[T](items: Sequence[T], requested: int) -> tuple[Sequence[T], int, int]:
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = max(0, min(requested, pages - 1))
    return items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE], page, pages


def character_options(
    characters: Sequence[Character], *, default_main: bool = False
) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label=safe_text(entry.full_name, 100),
            value=str(entry.id),
            emoji=class_emoji(entry.class_id),
            default=default_main and entry.is_main,
            description=(
                f"{class_name(entry.class_id)} | {role_badge(entry.gameplay_role)} |"
                f" {'Current Main' if entry.is_main else 'Alt'}"
                + (" | Complete name first" if entry.needs_completion else "")
                + (
                    " | " + safe_text(entry.secondary_name, 50)
                    if len(entry.full_name) > 100 and not entry.needs_completion
                    else ""
                )
            )[:100],
        )
        for entry in characters
    ]


class CharacterView(discord.ui.View):
    """Public controls carry no member-specific state and survive restarts."""

    def __init__(self, controller: Controller, *, register_all: bool = False):
        super().__init__(timeout=None)
        self.controller = controller
        manage = discord.ui.Button(
            label="My Characters",
            custom_id=MANAGE_ID,
            style=discord.ButtonStyle.primary,
            emoji="🧙",
        )
        manage.callback = self.manage
        self.add_item(manage)
        if register_all:
            # Keep older public panels working with their original custom IDs.
            mine = discord.ui.Button(
                label="My Characters",
                custom_id=MY_CHARACTERS_ID,
                style=discord.ButtonStyle.secondary,
                emoji="🧙",
            )
            mine.callback = self.mine
            self.add_item(mine)
        if register_all or (
            controller.settings.enable_guild_roster
            and controller.settings.roster_visibility != "disabled"
        ):
            browse = discord.ui.Button(
                label="Community Roster",
                custom_id=COMMUNITY_ROSTER_ID,
                style=discord.ButtonStyle.secondary,
                emoji="👥",
            )
            browse.callback = self.browse
            self.add_item(browse)

    async def manage(self, interaction: discord.Interaction) -> None:
        await self.controller.characters_ui.open(interaction, panel=True)

    async def mine(self, interaction: discord.Interaction) -> None:
        await self.controller.characters_ui.open(interaction, panel=True)

    async def browse(self, interaction: discord.Interaction) -> None:
        await self.controller.characters_ui.guild_roster(interaction, panel=True)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        await self.controller.handle_error(interaction, error, action="character")


class CharacterController:
    def __init__(self, controller: Controller):
        self.controller = controller
        self.settings, self.bot, self.store = (
            controller.settings,
            controller.bot,
            controller.store,
        )
        self.manager = self.bot.characters
        self._navigation_locks: WeakValueDictionary[SessionKey, asyncio.Lock] = (
            WeakValueDictionary()
        )
        self._private_views: WeakValueDictionary[int, OwnedView] = WeakValueDictionary()
        self._private_sessions: WeakValueDictionary[SessionKey, OwnedView] = (
            WeakValueDictionary()
        )

    @staticmethod
    def session_key(interaction: discord.Interaction) -> SessionKey:
        return interaction.guild_id, interaction.user.id, interaction.channel_id

    def private_source(self, interaction: discord.Interaction) -> bool:
        message = interaction.message
        return (
            interaction.type
            in (
                discord.InteractionType.component,
                discord.InteractionType.modal_submit,
            )
            and message is not None
            and getattr(getattr(message, "flags", None), "ephemeral", False) is True
            and message.author.id == self.bot.user.id
        )

    def track_view(self, message_id: int, view: discord.ui.View | None) -> None:
        previous = self._private_views.pop(message_id, None)
        if previous is not None and previous is not view:
            previous.stop()
        if isinstance(view, OwnedView):
            view.message_id = message_id
            self._private_views[message_id] = view

    async def bind_view(
        self,
        interaction: discord.Interaction,
        message_id: int,
        view: discord.ui.View | None,
        delete_message: Callable[[], Awaitable[None]],
    ) -> None:
        self.track_view(message_id, view)
        if isinstance(view, OwnedView):
            key = self.session_key(interaction)
            previous = self._private_sessions.get(key)
            view.session_key, view.delete_message = key, delete_message
            self._private_sessions[key] = view
            if previous is not None and previous.message_id != message_id:
                await previous.dismiss()

    def guard(
        self,
        interaction: discord.Interaction,
        *,
        owner_id: int | None = None,
        guild_id: int | None = None,
    ) -> discord.Guild:
        guild = require_guild(self.settings, interaction.guild)
        if not self.settings.enable_character_management:
            raise UserError(
                "Character management is disabled. Use the class selection panel."
            )
        if self.controller.stopping:
            raise UserError(
                "The bot is restarting. Please reopen character management shortly."
            )
        if (
            not isinstance(interaction.user, discord.Member)
            or interaction.user.guild.id != guild.id
            or interaction.user.bot
        ):
            raise UserError(
                "Character management requires your membership in this server."
            )
        if (owner_id is not None and interaction.user.id != owner_id) or (
            guild_id is not None and guild.id != guild_id
        ):
            raise UserError(
                "This private character menu belongs to another member or server. Open"
                " your own menu."
            )
        require_bot_member(guild, client=interaction.client)
        return guild

    async def execute(
        self,
        interaction: discord.Interaction,
        callback: Callable[[discord.Guild], Awaitable[None]],
        *,
        defer: bool = True,
        owner_id: int | None = None,
        guild_id: int | None = None,
        panel: bool = False,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self.controller.active.add(task)
        operation = "validate_context"
        try:
            guild = self.guard(interaction, owner_id=owner_id, guild_id=guild_id)
            update = defer and not panel and self.private_source(interaction)
            message_id = interaction.message.id if update else None
            previous = self._private_views.get(message_id) if update else None
            if defer and not interaction.response.is_done():
                operation = "acknowledge_interaction"
                await interaction.response.defer(ephemeral=True, thinking=not update)
            if panel:
                operation = "validate_panel"
                saved = await self.store.panel(guild.id)
                if (
                    not saved
                    or not interaction.message
                    or interaction.message.id != saved.message_id
                    or interaction.channel_id != saved.channel_id
                    or interaction.message.author.id != self.bot.user.id
                ):
                    raise UserError(
                        "This panel is no longer active. Open the current character"
                        " panel or use the character commands."
                    )
            operation = callback.__name__
            if defer:
                lock = self._navigation_locks.setdefault(
                    self.session_key(interaction), asyncio.Lock()
                )
                async with lock:
                    if (
                        previous is not None
                        and self._private_views.get(message_id) is not previous
                    ):
                        raise UserError(
                            "This menu has just changed. Use the current controls to"
                            " continue."
                        )
                    await callback(guild)
            else:
                # Modal launches must be immediate, without waiting for I/O or a lock.
                await callback(guild)
        except Exception as error:
            await self.controller.handle_error(
                interaction, error, action="character", operation=operation
            )
        finally:
            if task is not None:
                self.controller.active.discard(task)

    async def send(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> None:
        kwargs = {
            "content": self.bot.redact(content) if content else None,
            "embed": embed,
            "view": view,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if (
            interaction.response.type
            == discord.InteractionResponseType.deferred_message_update
            and self.private_source(interaction)
        ):
            await interaction.edit_original_response(**kwargs)
            await self.bind_view(
                interaction,
                interaction.message.id,
                view,
                interaction.delete_original_response,
            )
        elif interaction.response.is_done():
            message = await interaction.followup.send(
                **kwargs, ephemeral=True, wait=True
            )
            if message is not None and isinstance(message.id, int):
                await self.bind_view(interaction, message.id, view, message.delete)
        else:
            response = await interaction.response.send_message(**kwargs, ephemeral=True)
            message_id = response.message_id
            if isinstance(message_id, int):
                await self.bind_view(
                    interaction, message_id, view, interaction.delete_original_response
                )

    async def after_change(
        self,
        interaction: discord.Interaction,
        notice: str,
        *,
        page: int = 0,
        selected_id: int | None = None,
        selected_name_key: str | None = None,
    ) -> None:
        """Show confirmed success and fresh controls without another role write."""
        try:
            roster = await self.manager.roster(
                interaction.guild, interaction.user.id, reconcile=False
            )
        except StorageError:
            await self.send(
                interaction,
                content=notice
                + " Your roster could not refresh. Reopen My Characters to view it.",
                view=BackView(self, interaction.guild_id, interaction.user.id),
            )
            return
        if selected_name_key is not None:
            selected_id = next(
                (
                    char.id
                    for char in roster.characters
                    if normalized_name_key(char.primary_name, char.secondary_name)
                    == selected_name_key
                ),
                selected_id,
            )
        await self.send_roster(
            interaction, roster, page=page, notice=notice, selected_id=selected_id
        )

    async def open(
        self,
        interaction: discord.Interaction,
        *,
        panel: bool = False,
        page: int = 0,
        selected_id: int | None = None,
    ) -> None:
        async def display(guild: discord.Guild) -> None:
            warning = ""
            try:
                roster = await self.manager.roster(guild, interaction.user.id)
            except (
                UserError,
                discord.DiscordException,
                aiohttp.ClientError,
                OSError,
                TimeoutError,
            ) as error:
                roster = await self.manager.roster(
                    guild, interaction.user.id, reconcile=False
                )
                warning = (
                    str(error)
                    if isinstance(error, UserError)
                    else (
                        "Discord could not confirm your main's class role. Your saved"
                        " roster is available; reopen this menu to retry"
                        " synchronization."
                    )
                )
            if not roster.characters:
                try:
                    legacy = await self.bot.members.current(guild, interaction.user.id)
                    warning = (
                        legacy + " Register your main character to begin managing your"
                        " characters and alts."
                    )
                except (
                    discord.DiscordException,
                    aiohttp.ClientError,
                    OSError,
                    TimeoutError,
                ):
                    warning = (
                        "Register your main character to begin. Existing class roles"
                        " remain until registration succeeds."
                    )
            await self.send_roster(
                interaction, roster, page=page, warning=warning, selected_id=selected_id
            )

        await self.execute(interaction, display, panel=panel)

    async def send_roster(
        self,
        interaction: discord.Interaction,
        roster: Roster,
        *,
        page: int = 0,
        warning: str = "",
        notice: str | None = None,
        selected_id: int | None = None,
    ) -> None:
        ordered = sorted(
            roster.characters, key=lambda char: (not char.is_main, char.id)
        )
        if selected_id is not None:
            page = next(
                (
                    index // PAGE_SIZE
                    for index, char in enumerate(ordered)
                    if char.id == selected_id
                ),
                page,
            )
        entries, page, pages = page_slice(ordered, page)
        limit = self.settings.max_characters_per_user
        at_limit = bool(limit and len(ordered) >= limit)
        selected = next(
            (char for char in entries if char.id == selected_id),
            entries[0] if entries else None,
        )
        lines = (
            [
                "Choose a character to preview it. **Make Main** sets your Discord"
                " class color and gameplay tag."
            ]
            if entries
            else []
        )
        if warning:
            lines.append(safe_text(warning, 700) + "\n")
        for character in entries:
            designation = "⭐ Main" if character.is_main else "Alt"
            completion = " • Name incomplete" if character.needs_completion else ""
            lines.append(
                f"**{safe_text(character.full_name, 110)}**\n"
                f"{class_name(character.class_id)}"
                f" • {role_badge(character.gameplay_role)} • {designation}{completion}"
            )
        if not entries:
            lines.append(
                "Start with **Add Character** below. Enter both name parts and choose"
                " **Class & Role**, then submit to save. Your first character becomes"
                " your main and sets your class color and gameplay tag.\n\n⚔️ **DPS** ·"
                " Deal damage\n💚 **Healing** · Support your allies\n🛡️ **Tank** ·"
                " Protect your allies"
            )
        elif roster.main is None:
            lines.append(
                "\nNo main is designated. Complete any unfinished name, select a"
                " character, then choose Make Main."
            )
        if at_limit:
            lines.append(
                f"You have reached this server's limit of {limit} characters. Edit or"
                " remove a character before adding another."
            )
        if any(character.needs_completion for character in entries):
            lines.append("Use Edit to complete each unfinished two-part name.")
        embed = discord.Embed(
            title="Your Characters",
            description="\n\n".join(lines),
            colour=class_color(self.settings, selected.class_id if selected else None),
        )
        embed.set_footer(
            text=(
                f"Page {page + 1}/{pages} • {len(ordered)}"
                f" character{'s' if len(ordered) != 1 else ''} • Closes after 5 minutes"
                " idle"
            )
        )
        if selected:
            detail = (
                f"**{safe_text(selected.full_name, 110)}**\n"
                f"{class_name(selected.class_id)}"
                f" • {role_badge(selected.gameplay_role)} •"
                f" {'⭐ Main' if selected.is_main else 'Alt'}"
            )
            if selected.gameplay_role is None:
                detail += (
                    "\nUse Edit to choose DPS, Healing or Tank for this character."
                )
            if (
                selected.is_main
                and len(ordered) == 1
                and not self.settings.allow_clear_selection
            ):
                detail += (
                    "\nThis server requires a main character. Edit this character or"
                    " add a replacement before removing it."
                )
            if selected.needs_completion:
                detail += (
                    "\nComplete both name fields with Edit before making this your"
                    " main."
                )
            if selected.class_id not in {entry.id for entry in self.settings.classes}:
                detail += (
                    "\nThis class is disabled. Edit the class or choose another"
                    " character to make main."
                    if self.settings.allow_character_class_edit
                    else (
                        "\nThis class is disabled. Choose a character with an enabled"
                        " class to make main."
                    )
                )
            embed.add_field(name="Selected Character", value=detail, inline=False)
        if roster.main is not None and page > 0:
            embed.add_field(
                name="Current Main",
                value=(
                    f"⭐ {safe_text(roster.main.full_name, 110)} •"
                    f" {class_name(roster.main.class_id)} •"
                    f" {role_badge(roster.main.gameplay_role)}"
                ),
                inline=False,
            )
        await self.send(
            interaction,
            content=notice,
            embed=embed,
            view=RosterView(
                self,
                interaction.guild_id,
                interaction.user.id,
                page,
                pages,
                entries,
                selected=selected,
                total_characters=len(ordered),
                can_add=not at_limit and (not ordered or roster.main is not None),
            ),
        )

    async def add(self, interaction: discord.Interaction) -> None:
        async def show(guild: discord.Guild) -> None:
            await interaction.response.send_modal(
                CharacterModal(self, guild.id, interaction.user.id)
            )

        await self.execute(interaction, show, defer=False)

    async def retry_character(
        self,
        interaction: discord.Interaction,
        error: UserError,
        character: Character | None,
        draft: CharacterDraft,
    ) -> None:
        async def retry(message: str) -> None:
            await self.send(
                interaction,
                content=message
                + "\n\nYour entries are kept below. Choose Edit Details to try again,"
                " or Back to Characters to review your roster.",
                view=RetryCharacterView(
                    self, interaction.guild_id, interaction.user.id, character, draft
                ),
            )

        await self.controller.handle_error(
            interaction,
            error,
            action="character",
            operation="save",
            respond_with=retry,
        )

    async def choose(
        self,
        interaction: discord.Interaction,
        action: str,
        *,
        selected: str | None = None,
        page: int = 0,
    ) -> None:
        if selected and action == "edit":
            # Slash autocomplete selection needs a fresh read before presenting
            # a button that can immediately open its modal within the deadline.
            async def selected_edit(guild: discord.Guild) -> None:
                roster = await self.manager.roster(
                    guild, interaction.user.id, reconcile=False
                )
                character = self.owned(roster, selected)
                await self.send(
                    interaction,
                    content=f"Edit **{safe_text(character.full_name)}**.",
                    view=EditLauncher(self, guild.id, interaction.user.id, character),
                )

            await self.execute(interaction, selected_edit)
            return

        async def show(guild: discord.Guild) -> None:
            roster = await self.manager.roster(
                guild, interaction.user.id, reconcile=False
            )
            if not roster.characters:
                await self.send_roster(
                    interaction,
                    roster,
                    notice=(
                        "Register your first character below. It will automatically"
                        " become your main."
                    ),
                )
                return
            if selected:
                character = self.owned(roster, selected)
                if action == "main":
                    notice = await self.manager.promote(
                        guild, interaction.user.id, character.id
                    )
                    await self.after_change(interaction, notice)
                else:
                    await self.remove_prompt(interaction, roster, character)
                return
            titles = {
                "main": "Choose Your New Main",
                "edit": "Edit a Character",
                "remove": "Remove a Character",
            }
            await self.send(
                interaction,
                content=(
                    titles[action]
                    + (
                        ". Your previous main will automatically become an alt."
                        if action == "main"
                        else ". Choose a character below."
                    )
                ),
                view=CharacterPicker(
                    self,
                    guild.id,
                    interaction.user.id,
                    roster.characters,
                    action,
                    page=page,
                ),
            )

        await self.execute(interaction, show)

    @staticmethod
    def owned(roster: Roster, selected: str | int) -> Character:
        character = next(
            (char for char in roster.characters if str(char.id) == str(selected)), None
        )
        if character is None:
            raise UserError(
                "That character is no longer in your roster. Reopen the menu and select"
                " one of your characters."
            )
        return character

    async def remove_prompt(
        self,
        interaction: discord.Interaction,
        roster: Roster,
        character: Character,
        *,
        page: int = 0,
    ) -> None:
        alternatives = [
            entry for entry in roster.characters if entry.id != character.id
        ]
        if character.is_main and alternatives:
            await self.send(
                interaction,
                content=(
                    "Choose a replacement main before removing"
                    f" **{safe_text(character.full_name)}**. No character changes until"
                    " you confirm."
                ),
                view=CharacterPicker(
                    self,
                    interaction.guild_id,
                    interaction.user.id,
                    alternatives,
                    "replacement",
                    page=page,
                    removed=character,
                ),
            )
            return
        if character.is_main and not self.settings.allow_clear_selection:
            raise UserError(
                "Removing your last character is disabled for this server. Edit it, add"
                " a replacement, or ask an officer to allow an empty roster."
            )
        await self.confirm_removal(interaction, character, None)

    async def confirm_removal(
        self,
        interaction: discord.Interaction,
        character: Character,
        replacement: Character | None,
    ) -> None:
        text = f"Remove **{safe_text(character.full_name)}** from your roster?"
        if replacement:
            text += (
                f" **{safe_text(replacement.full_name)}** will become your main before"
                " the old main is removed."
            )
        elif character.is_main:
            text += (
                " This clears your main and removes its managed class role and gameplay"
                " tag."
            )
        await self.send(
            interaction,
            content=text,
            view=RemovalConfirmation(
                self, interaction.guild_id, interaction.user.id, character, replacement
            ),
        )

    async def help(self, interaction: discord.Interaction) -> None:
        async def show(guild: discord.Guild) -> None:
            root = self.settings.character_command_root
            await self.send(
                interaction,
                content=(
                    "Open My Characters to add a character or select one to "
                    "Edit, Make Main or Remove. "
                    "Enter a First Name and Secondary Name, then choose "
                    "**Class & Role** in the same form. "
                    "Choices stay grouped by class and show only supported roles: "
                    "⚔️ DPS, 💚 Healing or 🛡️ Tank. Submit once to save. "
                    "Your first character becomes main; later characters become alts.\n"
                    f"`/{root} main` switches mains and automatically makes "
                    "the previous main an alt. Your main sets your Discord class "
                    "color and a ⚔️ DPS, 💚 Healing or 🛡️ Tank role tag. "
                    "Click a member's name or avatar in this server to see their "
                    "tag in their profile; no boosts or Nitro needed. "
                    "Gameplay roles also appear in the roster. Use Edit to change "
                    "a role or complete an older character's role.\n"
                    f"`/{root} list`, `/{root} edit` and `/{root} remove` also open "
                    "private workflows. Removal always asks for confirmation.\n"
                    "Done closes your menu; inactive menus close after five minutes. "
                    "Names and ownership are self-reported; registration does not "
                    "verify game availability or ownership."
                ),
                view=BackView(self, guild.id, interaction.user.id),
            )

        await self.execute(interaction, show)

    async def purge_prompt(
        self, interaction: discord.Interaction, target: discord.User
    ) -> None:
        async def show(guild: discord.Guild) -> None:
            self.authorize_purge(interaction)
            await self.send(
                interaction,
                content=(
                    f"Permanently delete all stored characters for <@{target.id}> in"
                    " this server? This cannot be undone. Discord roles remain"
                    " unchanged as a legacy class-only selection. This also works for"
                    " a member who has left the server."
                ),
                view=PurgeConfirmation(self, guild.id, interaction.user.id, target.id),
            )

        await self.execute(interaction, show)

    @staticmethod
    def authorize_purge(interaction: discord.Interaction) -> None:
        if (
            not interaction.permissions.manage_guild
            or not interaction.permissions.manage_roles
        ):
            raise UserError(
                "Manage Server and Manage Roles are required to delete another member's"
                " stored character data."
            )

    async def guild_roster(
        self,
        interaction: discord.Interaction,
        *,
        user_id: int | None = None,
        class_id: str | None = None,
        kind: str = "all",
        page: int = 0,
        panel: bool = False,
    ) -> None:
        from .roster_ui import show_guild_roster

        async def show(guild: discord.Guild) -> None:
            await show_guild_roster(
                self,
                interaction,
                user_id=user_id,
                class_id=class_id,
                kind=kind,
                page=page,
            )

        await self.execute(interaction, show, panel=panel)


class OwnedView(discord.ui.View):
    def __init__(self, controller: CharacterController, guild_id: int, owner_id: int):
        super().__init__(timeout=PRIVATE_TIMEOUT)
        self.controller, self.guild_id, self.owner_id = controller, guild_id, owner_id
        self.message_id: int | None = None
        self.session_key: SessionKey | None = None
        self.delete_message: Callable[[], Awaitable[None]] | None = None
        self.button("Done", self.done, row=4)

    async def dismiss(self) -> None:
        """Retire only this private menu, even if a new menu already replaced it."""
        self.stop()
        if self.controller._private_views.get(self.message_id) is self:
            self.controller._private_views.pop(self.message_id, None)
        if self.controller._private_sessions.get(self.session_key) is self:
            self.controller._private_sessions.pop(self.session_key, None)
        if self.delete_message is not None:
            try:
                await self.delete_message()
            except discord.NotFound:
                pass  # Already dismissed in the client.
            except (discord.HTTPException, aiohttp.ClientError, OSError, TimeoutError):
                # Tokens expire and restarts can interrupt cleanup. Never log URLs.
                log.warning("private_menu_cleanup_failed guild_id=%s", self.guild_id)

    async def on_timeout(self) -> None:
        if self.session_key is None:
            return
        lock = self.controller._navigation_locks.setdefault(
            self.session_key, asyncio.Lock()
        )
        async with lock:
            if self.controller._private_views.get(self.message_id) is self:
                await self.dismiss()

    async def done(self, interaction: discord.Interaction) -> None:
        async def close(guild: discord.Guild) -> None:
            if not self.controller.private_source(interaction):
                raise UserError("Open your private character menu to close it.")
            # Use this click's fresh token, not the original menu's token.
            try:
                await interaction.delete_original_response()
            except discord.NotFound:
                pass
            self.delete_message = None
            await self.dismiss()

        await self.run(interaction, close)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        try:
            self.controller.guard(
                interaction, owner_id=self.owner_id, guild_id=self.guild_id
            )
            if self.is_finished() or (
                self.message_id is not None
                and self.controller._private_views.get(self.message_id) is not self
            ):
                raise UserError(
                    "This menu has been replaced or expired. Use the current controls"
                    " or reopen My Characters."
                )
            if (
                self.message_id is not None
                and self.controller.private_source(interaction)
                and interaction.message.id == self.message_id
            ):
                # Even opening/cancelling a modal renews the idle timer. Keep a
                # fresh webhook token so cleanup also works in long sessions.
                self.delete_message = partial(
                    interaction.followup.delete_message, self.message_id
                )
            return True
        except UserError as error:
            await self.controller.controller.handle_error(
                interaction, error, action="character"
            )
            return False

    async def run(
        self,
        interaction: discord.Interaction,
        callback: Callable[[discord.Guild], Awaitable[None]],
        *,
        defer: bool = True,
    ) -> None:
        if not await self.interaction_check(interaction):
            return
        await self.controller.execute(
            interaction,
            callback,
            defer=defer,
            owner_id=self.owner_id,
            guild_id=self.guild_id,
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        await self.controller.controller.handle_error(
            interaction, error, action="character"
        )

    def button(
        self,
        label: str,
        callback: Callable[[discord.Interaction], Awaitable[None]],
        *,
        row: int = 0,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        disabled: bool = False,
        emoji: str | None = None,
    ) -> None:
        button = discord.ui.Button(
            label=label, row=row, style=style, disabled=disabled, emoji=emoji
        )
        button.callback = callback
        self.add_item(button)

    async def back(self, interaction: discord.Interaction) -> None:
        if await self.interaction_check(interaction):
            await self.controller.open(interaction)


class BackView(OwnedView):
    def __init__(self, controller: CharacterController, guild_id: int, owner_id: int):
        super().__init__(controller, guild_id, owner_id)
        self.button("Back to Characters", self.back)


class RosterView(OwnedView):
    def __init__(
        self,
        controller: CharacterController,
        guild_id: int,
        owner_id: int,
        page: int,
        pages: int,
        characters: Sequence[Character],
        *,
        selected: Character | None = None,
        total_characters: int | None = None,
        can_add: bool = True,
    ):
        super().__init__(controller, guild_id, owner_id)
        self.page = page
        self.characters = tuple(characters)
        self.selected = selected or (characters[0] if characters else None)
        total = total_characters if total_characters is not None else len(characters)
        if characters:
            options = character_options(characters)
            for option in options:
                option.default = option.value == str(self.selected.id)
            self.character_select = discord.ui.Select(
                placeholder="Choose a character to manage...",
                min_values=1,
                max_values=1,
                options=options,
                row=0,
            )
            self.character_select.callback = self.select_character
            self.add_item(self.character_select)
        if self.selected is not None:
            # Compact labels keep the selected character's actions on one row
            # on mobile. Destructive styling is reserved for confirmation.
            self.button("Edit", self.edit, row=1, emoji="✏️")
            self.button(
                "Make Main",
                self.main,
                row=1,
                style=discord.ButtonStyle.success,
                emoji="⭐",
                disabled=(
                    self.selected.is_main
                    or self.selected.needs_completion
                    or self.selected.class_id
                    not in {entry.id for entry in controller.settings.classes}
                ),
            )
            self.button(
                "Remove",
                self.remove,
                row=1,
                disabled=(
                    self.selected.is_main
                    and total == 1
                    and not controller.settings.allow_clear_selection
                ),
            )
        self.button(
            "Add Character",
            self.add,
            row=2,
            style=discord.ButtonStyle.primary,
            disabled=not can_add,
            emoji="➕",
        )
        if pages > 1:
            self.button("Previous", self.previous, row=3, disabled=page == 0)
            self.button("Next", self.next, row=3, disabled=page == pages - 1)
        if characters:
            self.button("Refresh", self.refresh, row=4)
        if (
            controller.settings.enable_guild_roster
            and controller.settings.roster_visibility != "disabled"
        ):
            self.button("Community Roster", self.browse, row=2, emoji="👥")

    async def add(self, interaction: discord.Interaction) -> None:
        async def show(guild: discord.Guild) -> None:
            await interaction.response.send_modal(
                CharacterModal(
                    self.controller, guild.id, self.owner_id, source_view=self
                )
            )

        await self.run(interaction, show, defer=False)

    async def select_character(self, interaction: discord.Interaction) -> None:
        async def select(guild: discord.Guild) -> None:
            values = (interaction.data or {}).get("values", [])
            selected = values[0] if len(values) == 1 else ""
            if selected not in {str(entry.id) for entry in self.characters}:
                raise UserError("Choose a character from this page's dropdown.")
            roster = await self.controller.manager.roster(
                guild, self.owner_id, reconcile=False
            )
            character = self.controller.owned(roster, selected)
            await self.controller.send_roster(
                interaction, roster, page=self.page, selected_id=character.id
            )

        await self.run(interaction, select)

    def selected_character(self) -> Character:
        if self.selected is None:
            raise UserError("Add a character first, then choose it from the dropdown.")
        return self.selected

    async def main(self, interaction: discord.Interaction) -> None:
        async def promote(guild: discord.Guild) -> None:
            selected = self.selected_character()
            notice = await self.controller.manager.promote(
                guild, self.owner_id, selected.id
            )
            await self.controller.after_change(
                interaction, notice, page=self.page, selected_id=selected.id
            )

        await self.run(interaction, promote)

    async def refresh(self, interaction: discord.Interaction) -> None:
        if await self.interaction_check(interaction):
            await self.controller.open(
                interaction,
                page=self.page,
                selected_id=self.selected.id if self.selected else None,
            )

    async def browse(self, interaction: discord.Interaction) -> None:
        if await self.interaction_check(interaction):
            await self.controller.guild_roster(interaction)

    async def edit(self, interaction: discord.Interaction) -> None:
        async def show(guild: discord.Guild) -> None:
            await interaction.response.send_modal(
                CharacterModal(
                    self.controller,
                    guild.id,
                    self.owner_id,
                    self.selected_character(),
                    source_view=self,
                )
            )

        await self.run(interaction, show, defer=False)

    async def remove(self, interaction: discord.Interaction) -> None:
        async def show(guild: discord.Guild) -> None:
            roster = await self.controller.manager.roster(
                guild, self.owner_id, reconcile=False
            )
            character = self.controller.owned(roster, self.selected_character().id)
            await self.controller.remove_prompt(interaction, roster, character)

        await self.run(interaction, show)

    async def turn_page(self, interaction: discord.Interaction, page: int) -> None:
        async def show(guild: discord.Guild) -> None:
            roster = await self.controller.manager.roster(
                guild, self.owner_id, reconcile=False
            )
            await self.controller.send_roster(interaction, roster, page=page)

        await self.run(interaction, show)

    async def previous(self, interaction: discord.Interaction) -> None:
        await self.turn_page(interaction, self.page - 1)

    async def next(self, interaction: discord.Interaction) -> None:
        await self.turn_page(interaction, self.page + 1)


class CharacterModal(discord.ui.Modal):
    """Discord/discord.py 2.7 support Label-wrapped selects inside modals."""

    def __init__(
        self,
        controller: CharacterController,
        guild_id: int,
        owner_id: int,
        character: Character | None = None,
        *,
        source_view: OwnedView | None = None,
        draft: CharacterDraft | None = None,
    ):
        super().__init__(
            title="Edit Character" if character else "Add Character",
            timeout=PRIVATE_TIMEOUT,
        )
        self.controller, self.guild_id, self.owner_id, self.character = (
            controller,
            guild_id,
            owner_id,
            character,
        )
        self.source_view = source_view
        settings = controller.settings

        def default_name(value: str, maximum: int) -> str | None:
            # Unknown legacy strings must be completed by the owner. A blank
            # field is safer than silently truncating or rewriting old data.
            if not value or len(value) > maximum:
                return None
            if not unicodedata.category(value[0]).startswith("L"):
                return None
            if any(unicodedata.category(char)[0] not in {"L", "M"} for char in value):
                return None
            return value

        self.primary = discord.ui.TextInput(
            custom_id="primary_name",
            placeholder="John",
            default=(
                default_name(
                    character.primary_name, settings.character_primary_name_max_length
                )
                if character
                else None
            ),
            required=True,
            min_length=settings.character_primary_name_min_length,
            max_length=settings.character_primary_name_max_length,
        )
        self.secondary = discord.ui.TextInput(
            custom_id="secondary_name",
            placeholder="Stormwind",
            default=(
                default_name(
                    character.secondary_name,
                    settings.character_secondary_name_max_length,
                )
                if character
                else None
            ),
            required=True,
            min_length=settings.character_secondary_name_min_length,
            max_length=settings.character_secondary_name_max_length,
        )
        self.add_item(
            discord.ui.Label(
                text="First Name",
                component=self.primary,
                description=(
                    "Original legacy data is preserved. Enter the first name here."
                    if character and character.needs_completion
                    else None
                ),
            )
        )
        self.add_item(discord.ui.Label(text="Secondary Name", component=self.secondary))
        if draft is not None:
            # Keep rejected form input in memory for correction, never in storage.
            self.primary.default = (
                draft[0] if len(draft[0]) <= self.primary.max_length else None
            )
            self.secondary.default = (
                draft[1] if len(draft[1]) <= self.secondary.max_length else None
            )
        fixed_class = character is not None and not settings.allow_character_class_edit
        available = (
            {}
            if fixed_class
            else {entry.id: entry.definition for entry in settings.display_classes}
        )
        # Keep a saved, disabled class available only to its existing character.
        # It still cannot be chosen by a new character or used to change mains.
        if character is not None and character.class_id in REGISTRY:
            available[character.class_id] = REGISTRY[character.class_id]
        if not available:
            raise UserError(
                "This character's saved class is unavailable. Ask an admin to enable"
                " character class editing."
            )
        if draft is not None:
            saved_pair = (draft[2], draft[3])
        elif character is not None:
            saved_pair = (character.class_id, character.gameplay_role)
        else:
            saved_pair = (None, None)
        enabled = {entry.id for entry in settings.classes}
        options = [
            discord.SelectOption(
                label=f"{entry.name} · {role_badge(role)}",
                value=f"{entry.id}:{role}",
                emoji=entry.emoji,
                description=(
                    f"Saved class · {ROLE_DESCRIPTIONS[role]}"
                    if entry.id not in enabled
                    else ROLE_DESCRIPTIONS[role]
                ),
                default=(entry.id, role) == saved_pair,
            )
            for entry in sorted(
                available.values(), key=lambda entry: entry.name.casefold()
            )
            for role in entry.gameplay_roles
        ]
        self.class_role_select = discord.ui.Select(
            # Retain the existing component ID for each editing policy.
            custom_id="wow:character:gameplay-role:v1" if fixed_class else "class_id",
            placeholder="Choose your class and role",
            options=options,
            min_values=1,
            max_values=1,
            required=True,
        )
        self.add_item(
            discord.ui.Label(
                text="Class & Role",
                description=(
                    "Your class is fixed. Choose how this character plays."
                    if fixed_class
                    else "Grouped by class. Only supported roles are shown."
                ),
                component=self.class_role_select,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        primary, secondary = self.primary.value, self.secondary.value
        values = self.class_role_select.values
        choice = values[0] if len(values) == 1 else ""
        class_id, separator, role = choice.partition(":")
        gameplay_role = role or None
        draft = (primary, secondary, class_id, gameplay_role)

        async def save(guild: discord.Guild) -> None:
            if self.is_finished() or (
                self.source_view is not None and self.source_view.is_finished()
            ):
                raise UserError(
                    "This menu has closed or changed. Reopen My Characters to try"
                    " again."
                )
            try:
                validate_names(self.controller.settings, primary, secondary)
                if not separator or not class_id or not gameplay_role:
                    raise UserError(
                        "Choose a class and role from the Class & Role list."
                    )
                if class_id not in REGISTRY:
                    raise UserError("Choose a recognized character class.")
                if self.character is None or class_id != self.character.class_id:
                    if (
                        self.character is not None
                        and not self.controller.settings.allow_character_class_edit
                    ):
                        raise UserError("Character class editing is disabled.")
                    if class_id not in {
                        entry.id for entry in self.controller.settings.classes
                    }:
                        raise UserError("Choose an enabled character class.")
                validate_gameplay_role(class_id, gameplay_role)
                if self.character:
                    notice = await self.controller.manager.edit(
                        guild,
                        self.owner_id,
                        self.character.id,
                        primary,
                        secondary,
                        class_id,
                        gameplay_role,
                        expected_updated_at=self.character.updated_at,
                    )
                else:
                    notice = await self.controller.manager.add(
                        guild,
                        self.owner_id,
                        primary,
                        secondary,
                        class_id,
                        gameplay_role,
                    )
            except UserError as error:
                await self.controller.retry_character(
                    interaction, error, self.character, draft
                )
                return
            self.stop()
            await self.controller.after_change(
                interaction,
                notice,
                selected_id=self.character.id if self.character else None,
                selected_name_key=(
                    None if self.character else normalized_name_key(primary, secondary)
                ),
            )

        await self.controller.execute(
            interaction, save, owner_id=self.owner_id, guild_id=self.guild_id
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await self.controller.controller.handle_error(
            interaction, error, action="character"
        )


class RetryCharacterView(OwnedView):
    def __init__(
        self,
        controller: CharacterController,
        guild_id: int,
        owner_id: int,
        character: Character | None,
        draft: CharacterDraft,
    ):
        super().__init__(controller, guild_id, owner_id)
        self.character, self.draft = character, draft
        self.button("Edit Details", self.retry, style=discord.ButtonStyle.primary)
        self.button("Back to Characters", self.back)

    async def retry(self, interaction: discord.Interaction) -> None:
        async def show(guild: discord.Guild) -> None:
            await interaction.response.send_modal(
                CharacterModal(
                    self.controller,
                    guild.id,
                    self.owner_id,
                    self.character,
                    source_view=self,
                    draft=self.draft,
                )
            )

        await self.run(interaction, show, defer=False)


class EditLauncher(OwnedView):
    def __init__(
        self,
        controller: CharacterController,
        guild_id: int,
        owner_id: int,
        character: Character,
    ):
        super().__init__(controller, guild_id, owner_id)
        self.character = character
        self.button("Edit Character", self.edit, style=discord.ButtonStyle.primary)
        self.button("Back to Characters", self.back)

    async def edit(self, interaction: discord.Interaction) -> None:
        async def show(guild: discord.Guild) -> None:
            await interaction.response.send_modal(
                CharacterModal(
                    self.controller,
                    guild.id,
                    self.owner_id,
                    self.character,
                    source_view=self,
                )
            )

        await self.run(interaction, show, defer=False)


class CharacterPicker(OwnedView):
    def __init__(
        self,
        controller: CharacterController,
        guild_id: int,
        owner_id: int,
        characters: Sequence[Character],
        action: str,
        *,
        page: int = 0,
        removed: Character | None = None,
    ):
        super().__init__(controller, guild_id, owner_id)
        self.characters = tuple(
            sorted(characters, key=lambda entry: (not entry.is_main, entry.id))
        )
        self.action, self.removed = action, removed
        entries, self.page, pages = page_slice(self.characters, page)
        if not entries:
            raise UserError("There are no characters to select. Reopen My Characters.")
        self.select = discord.ui.Select(
            placeholder=(
                f"{'Choose your main' if action == 'main' else 'Choose a character'} •"
                f" page {self.page + 1}/{pages}"
            ),
            min_values=1,
            max_values=1,
            options=character_options(entries, default_main=action == "main"),
            row=0,
        )
        self.select.callback = self.selected
        self.add_item(self.select)
        if pages > 1:
            self.button("Previous", self.previous, row=1, disabled=self.page == 0)
            self.button("Next", self.next, row=1, disabled=self.page == pages - 1)
        self.button("Back to Characters", self.back, row=1)

    async def selected(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values", [])
        selected = values[0] if len(values) == 1 else ""
        snapshot = next(
            (entry for entry in self.characters if str(entry.id) == selected), None
        )

        async def act(guild: discord.Guild) -> None:
            if snapshot is None:
                raise UserError("Choose a character from this menu.")
            if self.action == "edit":
                await interaction.response.send_modal(
                    CharacterModal(
                        self.controller,
                        guild.id,
                        self.owner_id,
                        snapshot,
                        source_view=self,
                    )
                )
                return
            roster = await self.controller.manager.roster(
                guild, self.owner_id, reconcile=False
            )
            character = self.controller.owned(roster, selected)
            if self.action == "main":
                notice = await self.controller.manager.promote(
                    guild, self.owner_id, character.id
                )
                await self.controller.after_change(interaction, notice)
            elif self.action == "replacement":
                removed = self.controller.owned(roster, self.removed.id)
                if not removed.is_main:
                    raise UserError(
                        "Your main changed while this menu was open. Choose Remove"
                        " Character again."
                    )
                await self.controller.confirm_removal(interaction, removed, character)
            else:
                await self.controller.remove_prompt(interaction, roster, character)

        await self.run(interaction, act, defer=self.action != "edit")

    async def turn_page(self, interaction: discord.Interaction, page: int) -> None:
        async def show(guild: discord.Guild) -> None:
            roster = await self.controller.manager.roster(
                guild, self.owner_id, reconcile=False
            )
            characters = roster.characters
            removed = None
            if self.removed:
                removed = self.controller.owned(roster, self.removed.id)
                characters = tuple(
                    entry for entry in characters if entry.id != removed.id
                )
            await self.controller.send(
                interaction,
                content=(
                    "Choose your replacement main" if removed else "Choose a character"
                ),
                view=CharacterPicker(
                    self.controller,
                    guild.id,
                    self.owner_id,
                    characters,
                    self.action,
                    page=page,
                    removed=removed,
                ),
            )

        await self.run(interaction, show)

    async def previous(self, interaction: discord.Interaction) -> None:
        await self.turn_page(interaction, self.page - 1)

    async def next(self, interaction: discord.Interaction) -> None:
        await self.turn_page(interaction, self.page + 1)


class RemovalConfirmation(OwnedView):
    def __init__(
        self,
        controller: CharacterController,
        guild_id: int,
        owner_id: int,
        character: Character,
        replacement: Character | None,
    ):
        super().__init__(controller, guild_id, owner_id)
        self.character, self.replacement = character, replacement
        self.used = False
        self._lock = asyncio.Lock()
        self.button("Confirm Removal", self.confirm, style=discord.ButtonStyle.danger)
        self.button("Cancel", self.cancel)

    async def confirm(self, interaction: discord.Interaction) -> None:
        async def remove(guild: discord.Guild) -> None:
            async with self._lock:
                if self.used:
                    raise UserError(
                        "This confirmation has already been used. Open My Characters"
                        " again."
                    )
                roster = await self.controller.manager.roster(
                    guild, self.owner_id, reconcile=False
                )
                current = self.controller.owned(roster, self.character.id)
                if (
                    current.full_name,
                    current.class_id,
                    current.gameplay_role,
                    current.is_main,
                ) != (
                    self.character.full_name,
                    self.character.class_id,
                    self.character.gameplay_role,
                    self.character.is_main,
                ):
                    raise UserError(
                        "This character changed while the confirmation was open. Choose"
                        " Remove Character again."
                    )
                text = await self.controller.manager.remove(
                    guild,
                    self.owner_id,
                    current.id,
                    replacement_id=self.replacement.id if self.replacement else None,
                )
                self.used = True
                self.stop()
                await self.controller.after_change(interaction, text)

        await self.run(interaction, remove)

    async def cancel(self, interaction: discord.Interaction) -> None:
        async def cancel(guild: discord.Guild) -> None:
            async with self._lock:
                if self.used:
                    raise UserError(
                        "This confirmation has already been completed. Reopen My"
                        " Characters to view the result."
                    )
                self.used = True
                self.stop()
            await self.controller.after_change(
                interaction,
                "Removal cancelled. Your characters are unchanged.",
                selected_id=self.character.id,
            )

        await self.run(interaction, cancel)


class PurgeConfirmation(OwnedView):
    def __init__(
        self,
        controller: CharacterController,
        guild_id: int,
        owner_id: int,
        target_id: int,
    ):
        super().__init__(controller, guild_id, owner_id)
        self.target_id = target_id
        self.used = False
        self._lock = asyncio.Lock()
        self.button(
            "Delete Stored Character Data",
            self.confirm,
            style=discord.ButtonStyle.danger,
        )
        self.button("Cancel", self.cancel)

    async def confirm(self, interaction: discord.Interaction) -> None:
        async def purge(guild: discord.Guild) -> None:
            self.controller.authorize_purge(interaction)
            async with self._lock:
                if self.used:
                    raise UserError("This deletion confirmation has already been used.")
                text = await self.controller.manager.purge(
                    guild, interaction.user, self.target_id
                )
                self.used = True
                self.stop()
                await self.controller.send(
                    interaction,
                    content=text,
                    view=BackView(self.controller, guild.id, self.owner_id),
                )

        await self.run(interaction, purge)

    async def cancel(self, interaction: discord.Interaction) -> None:
        async def cancel(guild: discord.Guild) -> None:
            async with self._lock:
                if self.used:
                    raise UserError(
                        "This deletion confirmation has already been completed. Reopen"
                        " My Characters to continue."
                    )
                self.used = True
                self.stop()
            await self.controller.send(
                interaction,
                content="Deletion cancelled. Stored character data is unchanged.",
                view=BackView(self.controller, guild.id, self.owner_id),
            )

        await self.run(interaction, cancel)
