"""Persistent native components and one active, bot-owned panel per guild."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .character_ui import CharacterView
from .config import Settings
from .roles import Locks, UserError, require_bot_member, require_guild
from .storage import Panel, Store

if TYPE_CHECKING:
    from .commands import Controller

SELECT_ID = "wow:class:select:v1"
MY_CLASS_ID = "wow:class:me:v1"
CLEAR_ID = "wow:class:clear:v1"
CHANNEL_PERMISSIONS = (
    "view_channel",
    "send_messages",
    "embed_links",
    "read_message_history",
)


def missing_channel_permissions(
    channel: discord.TextChannel, member: discord.Member
) -> list[str]:
    permissions = channel.permissions_for(member)
    return [
        name.replace("_", " ").title()
        for name in CHANNEL_PERMISSIONS
        if not getattr(permissions, name)
    ]


class ClassSelect(discord.ui.Select):
    def __init__(self, controller: Controller):
        super().__init__(
            custom_id=SELECT_ID,
            placeholder=controller.settings.panel_placeholder,
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=entry.definition.name, value=entry.id)
                for entry in controller.settings.display_classes
            ],
        )
        self.controller = controller

    async def callback(self, interaction: discord.Interaction) -> None:
        # Read this interaction, not Select.values shared by concurrent dispatches.
        data = interaction.data or {}
        values = data.get("values", [])
        class_id = values[0] if len(values) == 1 else ""
        await self.controller.execute(interaction, "set", class_id=class_id, panel=True)


class ClassView(discord.ui.View):
    def __init__(self, controller: Controller, *, register_all: bool = False):
        super().__init__(timeout=None)
        self.controller = controller
        self.add_item(ClassSelect(controller))
        button = discord.ui.Button(
            label="My Class", custom_id=MY_CLASS_ID, style=discord.ButtonStyle.secondary
        )
        button.callback = self.show_class
        self.add_item(button)
        if controller.settings.allow_clear_selection or register_all:
            clear = discord.ui.Button(
                label="Clear Class",
                custom_id=CLEAR_ID,
                style=discord.ButtonStyle.secondary,
            )
            clear.callback = self.clear_class
            self.add_item(clear)

    async def show_class(self, interaction: discord.Interaction) -> None:
        await self.controller.execute(interaction, "me", panel=True)

    async def clear_class(self, interaction: discord.Interaction) -> None:
        await self.controller.execute(interaction, "clear", panel=True)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        await self.controller.handle_error(interaction, error)


class PanelManager:
    def __init__(
        self, settings: Settings, store: Store, locks: Locks, controller: Controller
    ):
        self.settings, self.store, self.locks, self.controller = (
            settings,
            store,
            locks,
            controller,
        )

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=(
                self.settings.character_panel_title
                if self.settings.enable_character_management
                else self.settings.panel_title
            ),
            description=self.settings.panel_description,
            colour=self.settings.panel_accent_color,
        )
        if self.settings.panel_footer:
            embed.set_footer(text=self.settings.panel_footer)
        return embed

    async def _channel(
        self, guild: discord.Guild, channel_id: int
    ) -> discord.TextChannel:
        require_bot_member(guild)
        channel = await guild.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
            raise UserError(
                "Choose a server text channel for the class panel; threads, forums and DMs are not supported."
            )
        missing = missing_channel_permissions(channel, guild.me)
        if missing:
            raise UserError(
                "The bot needs these channel permissions: " + ", ".join(missing) + "."
            )
        return channel

    async def setup(
        self, guild: discord.Guild, channel_id: int
    ) -> tuple[Panel, list[str]]:
        require_guild(self.settings, guild)
        async with self.locks.panel(guild.id):
            return await self._setup_locked(guild, channel_id)

    async def _setup_locked(
        self, guild: discord.Guild, channel_id: int
    ) -> tuple[Panel, list[str]]:
        channel = await self._channel(guild, channel_id)
        previous = await self.store.panel(guild.id)
        message = None
        if previous and previous.channel_id == channel.id:
            try:
                message = await channel.fetch_message(previous.message_id)
            except discord.NotFound as error:
                if error.code != 10008:
                    # A missing guild/channel is not evidence that this message
                    # was deleted. Preserve the mapping and surface that cause.
                    raise
            if message and message.author.id != self.controller.bot.user.id:
                raise UserError(
                    "The stored panel is not owned by this bot. Restore the correct database/token pair; no message was changed."
                )
        view = (
            CharacterView(self.controller)
            if self.settings.enable_character_management
            else ClassView(self.controller)
        )
        if message is not None:
            try:
                await message.edit(
                    content=None,
                    embed=self.embed(),
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.NotFound as error:
                if error.code != 10008:
                    raise
                # An administrator can delete the panel between fetch and edit.
                message = None
        if message is None:
            message = await channel.send(
                embed=self.embed(),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        panel = Panel(guild.id, channel.id, message.id)
        try:
            await self.store.save_panel(panel)
        except Exception:
            if previous is None or previous.message_id != message.id:
                # Prevent an unsaved panel from looking usable; never touch other messages.
                try:
                    await message.edit(view=None)
                except discord.HTTPException:
                    pass
            raise
        warnings = await self.retire_old(guild)
        return panel, warnings

    async def retire_old(self, guild: discord.Guild) -> list[str]:
        warnings = []
        for panel in await self.store.retired_panels(guild.id):
            try:
                channel = await guild.fetch_channel(panel.channel_id)
                if isinstance(channel, discord.TextChannel):
                    message = await channel.fetch_message(panel.message_id)
                    if message.author.id == self.controller.bot.user.id:
                        await message.edit(view=None)
                await self.store.forget_retired(panel.message_id)
            except discord.NotFound:
                await self.store.forget_retired(panel.message_id)
            except discord.HTTPException:
                warnings.append(
                    "The old panel could not be disabled visually. Its actions are already inactive; restore channel access or remove that old bot message manually."
                )
        return warnings

    async def refresh(self, guild: discord.Guild) -> list[str]:
        require_guild(self.settings, guild)
        async with self.locks.panel(guild.id):
            # Read inside the lock so startup cannot undo a simultaneous panel move.
            panel = await self.store.panel(guild.id)
            if panel:
                _, warnings = await self._setup_locked(guild, panel.channel_id)
                return warnings
            return []
