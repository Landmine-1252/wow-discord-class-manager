"""Private, owner-bound character workflows behind a persistent public panel."""

from __future__ import annotations

import asyncio
import math
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

import aiohttp
import discord

from .classes import REGISTRY
from .roles import UserError, require_bot_member, require_guild
from .storage import StorageError

if TYPE_CHECKING:
    from .character_store import Character, Roster
    from .commands import Controller

MANAGE_ID = "wow:character:manage:v1"
MY_CHARACTERS_ID = "wow:character:list:v1"
COMMUNITY_ROSTER_ID = "wow:character:roster:v1"
PAGE_SIZE = 15
PRIVATE_TIMEOUT = 600


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
            default=default_main and entry.is_main,
            description=(
                f"{class_name(entry.class_id)} | {'Current Main' if entry.is_main else 'Alt'}"
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
            label="Manage Characters",
            custom_id=MANAGE_ID,
            style=discord.ButtonStyle.primary,
        )
        manage.callback = self.manage
        self.add_item(manage)
        mine = discord.ui.Button(
            label="My Characters",
            custom_id=MY_CHARACTERS_ID,
            style=discord.ButtonStyle.secondary,
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
            )
            browse.callback = self.browse
            self.add_item(browse)

    async def manage(self, interaction: discord.Interaction) -> None:
        await self.controller.characters_ui.open(interaction, manage=True, panel=True)

    async def mine(self, interaction: discord.Interaction) -> None:
        await self.controller.characters_ui.open(interaction, manage=False, panel=True)

    async def browse(self, interaction: discord.Interaction) -> None:
        await self.controller.characters_ui.guild_roster(interaction, panel=True)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        await self.controller.handle_error(interaction, error)


class CharacterController:
    def __init__(self, controller: Controller):
        self.controller = controller
        self.settings, self.bot, self.store = (
            controller.settings,
            controller.bot,
            controller.store,
        )
        self.manager = self.bot.characters
        self._navigation_locks: WeakValueDictionary[int, asyncio.Lock] = (
            WeakValueDictionary()
        )
        self._private_views: WeakValueDictionary[int, OwnedView] = WeakValueDictionary()

    def private_source(self, interaction: discord.Interaction) -> bool:
        message = interaction.message
        return (
            interaction.type
            in (discord.InteractionType.component, discord.InteractionType.modal_submit)
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
                "This private character menu belongs to another member or server. Open your own menu."
            )
        require_bot_member(guild)
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
                        "This panel is no longer active. Open the current character panel or use the character commands."
                    )
            operation = callback.__name__
            if update:
                lock = self._navigation_locks.setdefault(message_id, asyncio.Lock())
                async with lock:
                    if (
                        previous is not None
                        and self._private_views.get(message_id) is not previous
                    ):
                        raise UserError(
                            "This menu has just changed. Use the current controls to continue."
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
            self.track_view(interaction.message.id, view)
        elif interaction.response.is_done():
            message = await interaction.followup.send(
                **kwargs, ephemeral=True, wait=True
            )
            if message is not None and isinstance(message.id, int):
                self.track_view(message.id, view)
        else:
            await interaction.response.send_message(**kwargs, ephemeral=True)

    async def after_change(
        self, interaction: discord.Interaction, notice: str, *, page: int = 0
    ) -> None:
        """Show confirmed success alongside fresh controls without another role write."""
        try:
            roster = await self.manager.roster(
                interaction.guild, interaction.user.id, reconcile=False
            )
        except StorageError:
            await self.send(
                interaction,
                content=notice
                + " Your roster could not refresh. Reopen Manage Characters to view it.",
                view=BackView(self, interaction.guild_id, interaction.user.id),
            )
            return
        await self.send_roster(
            interaction, roster, manage=True, page=page, notice=notice
        )

    async def open(
        self,
        interaction: discord.Interaction,
        *,
        manage: bool = True,
        panel: bool = False,
        page: int = 0,
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
                    else "Discord could not confirm your main's class role. Your saved roster is available; reopen this menu to retry synchronization."
                )
            if not roster.characters:
                try:
                    legacy = await self.bot.members.current(guild, interaction.user.id)
                    warning = (
                        legacy
                        + " Register your main character to begin managing your characters and alts."
                    )
                except (
                    discord.DiscordException,
                    aiohttp.ClientError,
                    OSError,
                    TimeoutError,
                ):
                    warning = "Register your main character to begin. Existing class roles remain until registration succeeds."
            await self.send_roster(
                interaction, roster, manage=manage, page=page, warning=warning
            )

        await self.execute(interaction, display, panel=panel)

    async def send_roster(
        self,
        interaction: discord.Interaction,
        roster: Roster,
        *,
        manage: bool,
        page: int = 0,
        warning: str = "",
        notice: str | None = None,
    ) -> None:
        ordered = sorted(
            roster.characters, key=lambda char: (not char.is_main, char.id)
        )
        entries, page, pages = page_slice(ordered, page)
        lines = ["Share what you plan to play. You can change your main anytime."]
        if warning:
            lines.append(safe_text(warning, 700) + "\n")
        for character in entries:
            designation = "Main" if character.is_main else "Alt"
            completion = " • Name incomplete" if character.needs_completion else ""
            lines.append(
                f"**{safe_text(character.full_name, 110)}**\n{class_name(character.class_id)} • {designation}{completion}"
            )
        if not entries:
            lines.append(
                "Start with Add Character below. Enter both name parts and pick a class from the dropdown. Your first character becomes your main; later characters become alts. Planned names are welcome."
            )
        elif roster.main is None:
            lines.append(
                "\nNo main is designated. Complete any unfinished name, then choose your main from the dropdown."
            )
        if any(character.needs_completion for character in entries):
            lines.append(
                "Use Edit Character to complete each unfinished two-part name."
            )
        embed = discord.Embed(
            title="Your Characters",
            description="\n\n".join(lines),
            colour=self.settings.panel_accent_color,
        )
        embed.set_footer(
            text=f"Page {page + 1}/{pages} • {len(ordered)} characters • Plans can change • Menus last 10 minutes"
        )
        if roster.main is not None and page > 0:
            embed.add_field(
                name="Current Main",
                value=f"{safe_text(roster.main.full_name, 110)} • {class_name(roster.main.class_id)}",
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
                manage,
                page,
                pages,
                entries,
            ),
        )

    async def add(self, interaction: discord.Interaction) -> None:
        async def show(guild: discord.Guild) -> None:
            await interaction.response.send_modal(
                CharacterModal(self, guild.id, interaction.user.id)
            )

        await self.execute(interaction, show, defer=False)

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
                    manage=True,
                    notice="Register your first character below. It will automatically become your main.",
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
                "That character is no longer in your roster. Reopen the menu and select one of your characters."
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
                content=f"Choose a replacement main before removing **{safe_text(character.full_name)}**. No character changes until you confirm.",
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
                "Removing your last main is disabled. Ask the operator about ALLOW_CLEAR_SELECTION, or register a replacement character first."
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
            text += f" **{safe_text(replacement.full_name)}** will become your main before the old main is removed."
        elif character.is_main:
            text += " This clears your main and removes its managed class role."
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
                content=f"Use `/{root} add` or Manage Characters to enter a Primary Name, Secondary Name, and class. Your first character becomes main; later characters become alts.\n`/{root} main` switches mains in one step and automatically makes the previous main an alt. Only your main sets your Discord class role.\n`/{root} list`, `/{root} edit` and `/{root} remove` show, change or remove your registered characters. Removal always asks for confirmation.\nNames and ownership are self-reported; registration does not verify game availability or ownership.",
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
                content=f"Permanently delete all stored characters for <@{target.id}> in this server? This cannot be undone. Discord roles remain unchanged as a legacy class-only selection. This also works for a member who has left the server.",
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
                "Manage Server and Manage Roles are required to delete another member's stored character data."
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
                    "This menu has been replaced or expired. Use the current controls or reopen Manage Characters."
                )
            return True
        except UserError as error:
            await self.controller.controller.handle_error(interaction, error)
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
        await self.controller.controller.handle_error(interaction, error)

    def button(
        self,
        label: str,
        callback: Callable[[discord.Interaction], Awaitable[None]],
        *,
        row: int = 0,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        disabled: bool = False,
    ) -> None:
        button = discord.ui.Button(label=label, row=row, style=style, disabled=disabled)
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
        manage: bool,
        page: int,
        pages: int,
        characters: Sequence[Character],
    ):
        super().__init__(controller, guild_id, owner_id)
        self.manage, self.page = manage, page
        if manage:
            if characters:
                self.main_select = discord.ui.Select(
                    placeholder="Change Main • choose a character...",
                    min_values=1,
                    max_values=1,
                    options=character_options(characters, default_main=True),
                    row=0,
                )
                self.main_select.callback = self.main
                self.add_item(self.main_select)
            self.characters = tuple(characters)
            self.button(
                "Add Character", self.add, row=1, style=discord.ButtonStyle.primary
            )
            self.button("Edit Character", self.edit, row=1, disabled=not characters)
            self.button("Remove Character", self.remove, row=1, disabled=not characters)
        else:
            self.button(
                "Manage Characters",
                self.manage_characters,
                style=discord.ButtonStyle.primary,
            )
        if pages > 1:
            self.button("Previous", self.previous, row=2, disabled=page == 0)
            self.button("Next", self.next, row=2, disabled=page == pages - 1)
        self.button("Refresh", self.refresh, row=2)
        if (
            controller.settings.enable_guild_roster
            and controller.settings.roster_visibility != "disabled"
        ):
            self.button("Community Roster", self.browse, row=2)

    async def add(self, interaction: discord.Interaction) -> None:
        async def show(guild: discord.Guild) -> None:
            await interaction.response.send_modal(
                CharacterModal(self.controller, guild.id, self.owner_id)
            )

        await self.run(interaction, show, defer=False)

    async def main(self, interaction: discord.Interaction) -> None:
        async def promote(guild: discord.Guild) -> None:
            values = (interaction.data or {}).get("values", [])
            selected = values[0] if len(values) == 1 else ""
            if selected not in {str(entry.id) for entry in self.characters}:
                raise UserError("Choose a character from this page's dropdown.")
            notice = await self.controller.manager.promote(
                guild, self.owner_id, int(selected)
            )
            await self.controller.after_change(interaction, notice)

        await self.run(interaction, promote)

    async def refresh(self, interaction: discord.Interaction) -> None:
        if await self.interaction_check(interaction):
            await self.controller.open(interaction, manage=self.manage, page=self.page)

    async def browse(self, interaction: discord.Interaction) -> None:
        if await self.interaction_check(interaction):
            await self.controller.guild_roster(interaction)

    async def edit(self, interaction: discord.Interaction) -> None:
        if await self.interaction_check(interaction):
            await self.controller.choose(interaction, "edit")

    async def remove(self, interaction: discord.Interaction) -> None:
        if await self.interaction_check(interaction):
            await self.controller.choose(interaction, "remove")

    async def manage_characters(self, interaction: discord.Interaction) -> None:
        if await self.interaction_check(interaction):
            await self.controller.open(interaction)

    async def turn_page(self, interaction: discord.Interaction, page: int) -> None:
        async def show(guild: discord.Guild) -> None:
            roster = await self.controller.manager.roster(
                guild, self.owner_id, reconcile=False
            )
            await self.controller.send_roster(
                interaction, roster, manage=self.manage, page=page
            )

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
                text="Primary Name",
                component=self.primary,
                description=(
                    "Original legacy data is preserved. Enter the primary part here."
                    if character and character.needs_completion
                    else None
                ),
            )
        )
        self.add_item(discord.ui.Label(text="Secondary Name", component=self.secondary))
        self.class_select: discord.ui.Select | None = None
        if character is None or settings.allow_character_class_edit:
            options = [
                discord.SelectOption(
                    label=entry.definition.name,
                    value=entry.id,
                    default=character is not None and character.class_id == entry.id,
                )
                for entry in settings.display_classes
            ]
            if (
                character
                and character.class_id in REGISTRY
                and character.class_id not in {entry.id for entry in settings.classes}
            ):
                options.append(
                    discord.SelectOption(
                        label=class_name(character.class_id) + " (disabled)",
                        value=character.class_id,
                        default=True,
                    )
                )
            options.sort(key=lambda option: option.label.casefold())
            self.class_select = discord.ui.Select(
                custom_id="class_id",
                placeholder="Choose the character's class",
                options=options,
                min_values=1,
                max_values=1,
                required=True,
            )
            self.add_item(discord.ui.Label(text="Class", component=self.class_select))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        primary, secondary = self.primary.value, self.secondary.value
        values = self.class_select.values if self.class_select is not None else []
        class_id = (
            values[0]
            if len(values) == 1
            else (
                self.character.class_id
                if self.character and self.class_select is None
                else ""
            )
        )

        async def save(guild: discord.Guild) -> None:
            if self.character:
                text = await self.controller.manager.edit(
                    guild,
                    self.owner_id,
                    self.character.id,
                    primary,
                    secondary,
                    class_id,
                    expected_updated_at=self.character.updated_at,
                )
            else:
                text = await self.controller.manager.add(
                    guild, self.owner_id, primary, secondary, class_id
                )
            await self.controller.after_change(interaction, text)

        await self.controller.execute(
            interaction, save, owner_id=self.owner_id, guild_id=self.guild_id
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await self.controller.controller.handle_error(interaction, error)


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
                CharacterModal(self.controller, guild.id, self.owner_id, self.character)
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
            raise UserError(
                "There are no characters to select. Reopen Manage Characters."
            )
        self.select = discord.ui.Select(
            placeholder=f"{'Choose your main' if action == 'main' else 'Choose a character'} • page {self.page + 1}/{pages}",
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
                    CharacterModal(self.controller, guild.id, self.owner_id, snapshot)
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
                        "Your main changed while this menu was open. Choose Remove Character again."
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
                        "This confirmation has already been used. Open Manage Characters again."
                    )
                roster = await self.controller.manager.roster(
                    guild, self.owner_id, reconcile=False
                )
                current = self.controller.owned(roster, self.character.id)
                if (current.full_name, current.class_id, current.is_main) != (
                    self.character.full_name,
                    self.character.class_id,
                    self.character.is_main,
                ):
                    raise UserError(
                        "This character changed while the confirmation was open. Choose Remove Character again."
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
                        "This confirmation has already been completed. Reopen Manage Characters to view the result."
                    )
                self.used = True
                self.stop()
            await self.controller.after_change(
                interaction, "Removal cancelled. Your characters are unchanged."
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
                        "This deletion confirmation has already been completed. Reopen Manage Characters to continue."
                    )
                self.used = True
                self.stop()
            await self.controller.send(
                interaction,
                content="Deletion cancelled. Stored character data is unchanged.",
                view=BackView(self.controller, guild.id, self.owner_id),
            )

        await self.run(interaction, cancel)
