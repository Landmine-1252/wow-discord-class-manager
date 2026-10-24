"""Runtime authorization and a single configurable slash-command group."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from . import diagnostics
from .character_ui import CharacterController
from .config import SUBCOMMANDS
from .roles import UserError, api_problem, require_bot_member, require_guild
from .storage import StorageError
from .ui import PanelManager

if TYPE_CHECKING:
    from .bot import ClassBot

log = logging.getLogger(__name__)


async def respond(
    interaction: discord.Interaction, message: str, *, delete_after: float | None = None
) -> None:
    # Discord messages are limited to 2000 characters; diagnostics may need more.
    for start in range(0, len(message), 1900):
        kwargs = {
            "content": message[start : start + 1900],
            "ephemeral": True,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if interaction.response.is_done():
            if delete_after is None:
                await interaction.followup.send(**kwargs)
            else:
                reply = await interaction.followup.send(**kwargs, wait=True)
                await reply.delete(delay=delete_after)
        else:
            if delete_after is not None:
                kwargs["delete_after"] = delete_after
            await interaction.response.send_message(**kwargs)


def authorize(interaction: discord.Interaction, *, roles: bool = False) -> None:
    if not interaction.permissions.manage_guild:
        raise UserError("You need Manage Server to use this administrative command.")
    if roles and not interaction.permissions.manage_roles:
        raise UserError(
            "You also need Manage Roles to set up or synchronize class roles."
        )


class Controller:
    def __init__(self, bot: ClassBot):
        self.bot = bot
        self.settings, self.store = bot.settings, bot.store
        self.panels = PanelManager(self.settings, self.store, bot.locks, self)
        self.active: set[asyncio.Task] = set()
        self.stopping = False
        self.characters_ui = CharacterController(self)

    async def execute(
        self,
        interaction: discord.Interaction,
        action: str,
        *,
        class_id: str | None = None,
        channel: discord.TextChannel | None = None,
        panel: bool = False,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self.active.add(task)
        operation = "acknowledge_interaction"
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
            operation = "validate_context"
            if self.stopping:
                raise UserError("The bot is restarting. Please try again shortly.")
            guild = require_guild(self.settings, interaction.guild)
            if (
                not isinstance(interaction.user, discord.Member)
                or interaction.user.guild.id != guild.id
                or interaction.user.bot
            ):
                raise UserError("This action requires your membership in this server.")
            if panel:
                saved = await self.store.panel(guild.id)
                if (
                    not saved
                    or not interaction.message
                    or interaction.message.id != saved.message_id
                    or interaction.channel_id != saved.channel_id
                    or interaction.message.author.id != self.bot.user.id
                ):
                    raise UserError(
                        "This panel is no longer active. Use the current class panel or"
                        " the class slash command."
                    )
            if action in {"setup", "sync", "status"}:
                authorize(interaction, roles=action in {"setup", "sync"})
            if action not in {"status", "help"}:
                require_bot_member(guild, client=interaction.client)
            if action == "setup":
                target = channel or interaction.channel
                if (
                    not isinstance(target, discord.TextChannel)
                    or target.guild.id != guild.id
                ):
                    raise UserError(
                        "Choose a text channel in this server for the panel."
                    )
                operation = "validate_invoker_channel_permissions"
                if target.id == interaction.channel_id:
                    # Discord resolves current-channel permissions in the
                    # interaction. Partial/stale channel caches can disagree.
                    permissions = interaction.permissions
                else:
                    operation = "fetch_target_channel"
                    target = await guild.fetch_channel(target.id)
                    if (
                        not isinstance(target, discord.TextChannel)
                        or target.guild.id != guild.id
                    ):
                        raise UserError(
                            "Choose a text channel in this server for the panel."
                        )
                    permissions = target.permissions_for(interaction.user)
                    operation = "validate_invoker_channel_permissions"
                missing = [
                    label
                    for flag, label in (
                        ("view_channel", "View Channel"),
                        ("send_messages", "Send Messages"),
                    )
                    if not getattr(permissions, flag)
                ]
                if missing:
                    raise UserError(
                        "Your account needs these permissions in the selected channel: "
                        + ", ".join(missing)
                        + ". Ask a server administrator to check your channel"
                        " permissions, or choose a channel where you can post.",
                        code="invoker_channel_permissions",
                    )
                previous = await self.store.panel(guild.id)
                if previous and previous.channel_id != target.id and channel is None:
                    raise UserError(
                        "A panel already exists in another channel. To move it, run"
                        f" {self.settings.command('setup')} with the channel option"
                        " explicitly selected."
                    )
                # Validate channel permissions before creating any roles.
                operation = "fetch_setup_channel"
                await self.panels._channel(guild, target.id)
                operation = "reconcile_roles"
                result = await self.bot.roles.reconcile(guild)
                operation = "publish_panel"
                saved, warnings = await self.panels.setup(guild, target.id)
                result.warnings.extend(warnings)
                text = (
                    "Class panel ready:"
                    f" https://discord.com/channels/{guild.id}/"
                    f"{saved.channel_id}/{saved.message_id}\n" + result.summary()
                )
                self.bot.notifier.notify(
                    f"setup:{guild.id}",
                    "Class panel setup completed in guild"
                    f" {guild.id}.\n{result.summary()}",
                )
            elif action == "sync":
                operation = "reconcile_roles"
                result = await self.bot.roles.reconcile(guild)
                text = result.summary()
                if result.warnings:
                    self.bot.notifier.notify(
                        f"roles:{guild.id}",
                        f"Role sync needs attention in guild {guild.id}.\n{text}",
                    )
            elif action == "status":
                operation = "guild_diagnostics"
                text = await diagnostics.status(self.settings, self.store, guild)
            elif action == "set":
                operation = "change_class"
                text = await self.bot.members.change(
                    guild, interaction.user.id, class_id if class_id is not None else ""
                )
            elif action == "clear":
                operation = "clear_class"
                text = await self.bot.members.change(guild, interaction.user.id, None)
            elif action == "me":
                operation = "read_member_class"
                roster = (
                    await self.bot.characters.roster(guild, interaction.user.id)
                    if self.settings.enable_character_management
                    else None
                )
                if roster and roster.main:
                    from .character_ui import class_name, safe_text

                    text = (
                        f"Your main character: {safe_text(roster.main.full_name)}"
                        f" ({class_name(roster.main.class_id)})."
                    )
                else:
                    text = await self.bot.members.current(guild, interaction.user.id)
            else:
                text = (
                    "Choose your primary class in the dropdown, or use"
                    f" {self.settings.command('set')}. Change it any time; unrelated"
                    f" roles are kept.\n{self.settings.command('me')} shows your class."
                )
                if self.settings.enable_character_management:
                    text = (
                        "Use Manage Characters or"
                        f" /{self.settings.character_command_root} add to register your"
                        " main and alts. Choosing another main automatically makes the"
                        f" old main an alt. /{self.settings.character_command_root}"
                        " list shows your roster.\nMembers without registered"
                        f" characters can still use {self.settings.command('set')}."
                    )
                if self.settings.allow_clear_selection:
                    text += (
                        f"\n{self.settings.command('clear')} clears a legacy class-only"
                        " selection. Registered characters use Remove Character with a"
                        " replacement main, or remove the last character when clearing"
                        " is enabled."
                    )
                text += (
                    f"\nOfficers: {self.settings.command('setup')} publishes the panel,"
                    f" {self.settings.command('sync')} reconciles roles, and"
                    f" {self.settings.command('status')} checks permissions and colors."
                )
            operation = "send_confirmation"
            await respond(interaction, self.bot.redact(text))
            if action in {"setup", "sync"}:
                check = getattr(self.bot, "check_guild", None)
                if check is not None:
                    try:
                        await check(guild)
                    except Exception as error:
                        # Setup already succeeded. A separate diagnostic failure
                        # must not turn its confirmed response into a failure.
                        log.warning(
                            "guild_diagnostics_failed guild_id=%s error_type=%s",
                            guild.id,
                            type(error).__name__,
                        )
        except Exception as error:
            await self.handle_error(
                interaction, error, action=action, operation=operation
            )
        finally:
            if task is not None:
                self.active.discard(task)

    async def handle_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        *,
        action: str = "interaction",
        operation: str = "dispatch",
        respond_with: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        error = getattr(error, "original", error)
        if isinstance(error, (UserError, StorageError)):
            message = str(error)
        elif isinstance(error, app_commands.TransformerError):
            operation = (
                "resolve_channel"
                if error.type == discord.AppCommandOptionType.channel
                else "resolve_option"
            )
            command = interaction.command
            if isinstance(command, app_commands.Command):
                action = command.qualified_name
            try:
                guild = require_guild(self.settings, interaction.guild)
                require_bot_member(guild, client=interaction.client)
            except UserError as context_error:
                message = str(context_error)
            else:
                if error.type == discord.AppCommandOptionType.channel:
                    message = (
                        "The selected channel could not be resolved. Choose an existing"
                        " server text channel the bot can view, or run setup directly"
                        " in that channel without the channel option."
                    )
                else:
                    message = (
                        "The selected command option could not be resolved. Reopen the"
                        " command and select it again."
                    )
        elif isinstance(error, discord.HTTPException):
            message = api_problem(error)
        else:
            message = (
                "The request could not be completed. Please try again; ask an officer"
                " to check the bot logs if it persists."
            )
        if isinstance(error, UserError):
            log.info(
                "interaction_rejected guild_id=%s action=%s operation=%s reason=%s",
                interaction.guild_id,
                action,
                operation,
                error.code,
            )
        else:
            log.error(
                "interaction_failed guild_id=%s action=%s operation=%s error_type=%s"
                " http_status=%s discord_code=%s",
                interaction.guild_id,
                action,
                operation,
                type(error).__name__,
                error.status if isinstance(error, discord.HTTPException) else None,
                error.code if isinstance(error, discord.HTTPException) else None,
            )
            self.bot.notifier.notify(
                f"error:{interaction.guild_id}",
                f"Class manager request failed in guild {interaction.guild_id}:"
                f" {type(error).__name__}. Check bot logs and status.",
            )
        try:
            if respond_with is not None:
                await respond_with(self.bot.redact(message))
            else:
                await respond(
                    interaction,
                    self.bot.redact(message),
                    delete_after=60 if action == "character" else None,
                )
        except discord.HTTPException as response_error:
            log.warning(
                "interaction_response_unavailable guild_id=%s action=%s http_status=%s"
                " discord_code=%s",
                interaction.guild_id,
                action,
                response_error.status,
                response_error.code,
            )


def build_commands(controller: Controller) -> app_commands.Group:
    settings = controller.settings
    group = app_commands.Group(
        name=settings.root_command,
        description="Choose and manage World of Warcraft class roles",
        guild_only=True,
        allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
    )

    async def setup(
        interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        await controller.execute(interaction, "setup", channel=channel)

    async def sync(interaction: discord.Interaction) -> None:
        await controller.execute(interaction, "sync")

    async def status(interaction: discord.Interaction) -> None:
        await controller.execute(interaction, "status")

    async def select(interaction: discord.Interaction, wow_class: str) -> None:
        await controller.execute(interaction, "set", class_id=wow_class)

    async def me(interaction: discord.Interaction) -> None:
        await controller.execute(interaction, "me")

    async def clear(interaction: discord.Interaction) -> None:
        await controller.execute(interaction, "clear")

    async def help_command(interaction: discord.Interaction) -> None:
        await controller.execute(interaction, "help")

    callbacks = (setup, sync, status, select, me, clear, help_command)
    descriptions = (
        "Create or update the class panel (Manage Server + Manage Roles)",
        "Synchronize class roles (Manage Server + Manage Roles)",
        "Show diagnostics (Manage Server)",
        "Choose your primary class",
        "Show your current class",
        "Clear your class if enabled",
        "How to use class selection",
    )
    for original, name, callback, description in zip(
        SUBCOMMANDS, settings.command_names, callbacks, descriptions, strict=True
    ):
        command = app_commands.Command(
            name=name, description=description, callback=callback
        )
        if original == "set":
            command = app_commands.choices(
                wow_class=[
                    app_commands.Choice(name=entry.definition.name, value=entry.id)
                    for entry in settings.display_classes
                ]
            )(command)
        group.add_command(command)
    return group
