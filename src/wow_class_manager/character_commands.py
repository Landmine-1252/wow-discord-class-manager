"""Character commands open the same private workflows as the public panel."""

from __future__ import annotations

import discord
from discord import app_commands

from .character_ui import CharacterController, class_name, safe_text


def build_character_commands(controller: CharacterController) -> app_commands.Group:
    group = app_commands.Group(
        name=controller.settings.character_command_root,
        description="Register characters, manage alts, and choose your main",
        guild_only=True,
        allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
    )

    @group.command(
        name="add", description="Register a character with two name parts and a class"
    )
    async def add(interaction: discord.Interaction) -> None:
        await controller.add(interaction)

    @group.command(
        name="list", description="Privately view your main character and alts"
    )
    async def list_characters(interaction: discord.Interaction) -> None:
        await controller.open(interaction)

    @group.command(
        name="main",
        description="Choose your main; the previous main automatically becomes an alt",
    )
    @app_commands.describe(
        character="Choose one of your registered characters, or open the selection menu"
    )
    async def main(
        interaction: discord.Interaction, character: str | None = None
    ) -> None:
        await controller.choose(interaction, "main", selected=character)

    @group.command(
        name="edit", description="Edit a character's two name parts or class"
    )
    @app_commands.describe(
        character="Choose one of your registered characters, or open the selection menu"
    )
    async def edit(
        interaction: discord.Interaction, character: str | None = None
    ) -> None:
        await controller.choose(interaction, "edit", selected=character)

    @group.command(
        name="remove", description="Choose a character to remove, with confirmation"
    )
    @app_commands.describe(
        character="Choose one of your registered characters, or open the selection menu"
    )
    async def remove(
        interaction: discord.Interaction, character: str | None = None
    ) -> None:
        await controller.choose(interaction, "remove", selected=character)

    @group.command(
        name="help", description="How to register characters and switch mains"
    )
    async def help_command(interaction: discord.Interaction) -> None:
        await controller.help(interaction)

    @group.command(
        name="purge",
        description="Delete a member's stored character data (Manage Server + Roles)",
    )
    @app_commands.describe(
        user="The owner whose character data should be deleted, including a departed user"
    )
    async def purge(interaction: discord.Interaction, user: discord.User) -> None:
        await controller.purge_prompt(interaction, user)

    async def autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        try:
            guild = controller.guard(interaction)
            roster = await controller.manager.roster(
                guild, interaction.user.id, reconcile=False
            )
            needle = current.casefold()
            entries = [
                entry
                for entry in roster.characters
                if needle in entry.full_name.casefold()
            ]
            return [
                app_commands.Choice(
                    name=f"{safe_text(entry.full_name, 65)} | {class_name(entry.class_id)} | {'Main' if entry.is_main else 'Alt'}"[
                        :100
                    ],
                    value=str(entry.id),
                )
                for entry in entries[:25]
            ]
        except Exception:
            # Autocomplete must not expose ownership, database details or secrets.
            return []

    for command in (main, edit, remove):
        command.autocomplete("character")(autocomplete)
    return group


def build_roster_command(controller: CharacterController) -> app_commands.Command:
    @app_commands.command(
        name="roster", description="View registered guild characters, mains and alts"
    )
    @app_commands.guild_only()
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.describe(
        member="Filter by Discord member",
        wow_class="Filter by an enabled class; omit to include all saved characters",
        kind="Show mains, alts or all registered characters",
    )
    @app_commands.choices(
        wow_class=[
            app_commands.Choice(name=entry.definition.name, value=entry.id)
            for entry in controller.settings.display_classes
        ],
        kind=[
            app_commands.Choice(name="All characters", value="all"),
            app_commands.Choice(name="Main characters", value="mains"),
            app_commands.Choice(name="Alternate characters", value="alts"),
        ],
    )
    async def roster(
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        wow_class: str | None = None,
        kind: str = "all",
    ) -> None:
        await controller.guild_roster(
            interaction,
            user_id=member.id if member else None,
            class_id=wow_class,
            kind=kind,
        )

    return roster
