"""A private guild roster browser with independent, composable filters."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .character_ui import OwnedView, class_name, page_slice, safe_text
from .roles import UserError

if TYPE_CHECKING:
    from .character_ui import CharacterController

KINDS = {
    "all": "All characters",
    "mains": "Main characters",
    "alts": "Alternate characters",
}


def require_roster_access(
    controller: CharacterController, interaction: discord.Interaction
) -> discord.Guild:
    """Recheck policy before reading a roster, including from an older menu."""
    guild = controller.guard(interaction)
    if (
        not controller.settings.enable_guild_roster
        or controller.settings.roster_visibility == "disabled"
    ):
        raise UserError(
            "The guild roster is disabled. You can still privately view My Characters."
        )
    if (
        controller.settings.roster_visibility == "admins"
        and not interaction.permissions.manage_guild
    ):
        raise UserError(
            "Manage Server is required to view this guild roster. You can still view My Characters."
        )
    return guild


def owner_label(guild: discord.Guild, user_id: int) -> str:
    """Use available display names without scanning or fetching guild members."""
    cached = guild.get_member(user_id) if hasattr(guild, "get_member") else None
    return safe_text(cached.display_name, 40) if cached else f"<@{user_id}>"


def count_label(count: int, singular: str) -> str:
    return f"{count} {singular}{'' if count == 1 else 's'}"


def require_enabled_filter(
    controller: CharacterController, class_id: str | None
) -> None:
    if class_id is not None and class_id not in {
        entry.id for entry in controller.settings.classes
    }:
        raise UserError(
            "Choose an enabled class or All classes from the roster filters. "
            "Saved characters from disabled classes remain visible under All classes."
        )


async def show_guild_roster(
    controller: CharacterController,
    interaction: discord.Interaction,
    *,
    user_id: int | None = None,
    class_id: str | None = None,
    kind: str = "all",
    page: int = 0,
) -> None:
    """Render inside the controller's deferred, tracked interaction execution."""
    guild = require_roster_access(controller, interaction)
    require_enabled_filter(controller, class_id)
    if kind not in KINDS:
        raise UserError("Choose all characters, mains or alts.")
    if user_id is not None and (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or not 0 < user_id < 2**64
    ):
        raise UserError("Choose a member using the member filter.")

    characters = await controller.manager.guild_roster(
        guild, interaction.user, user_id=user_id, class_id=class_id, kind=kind
    )
    entries, selected_page, pages = page_slice(characters, page)
    member_filter = owner_label(guild, user_id) if user_id else "All members"
    class_filter = class_name(class_id) if class_id else "All classes"
    mains = sum(character.is_main for character in characters)
    alts = len(characters) - mains
    members = len({character.user_id for character in characters})
    lines = [
        f"**Filters:** {class_filter} • {KINDS[kind]} • {member_filter}",
        f"**{count_label(len(characters), 'character')}** • {count_label(mains, 'main')} • {count_label(alts, 'alt')} • {count_label(members, 'member')}",
    ]
    for character in entries:
        designation = "Main" if character.is_main else "Alt"
        lines.append(
            f"**{safe_text(character.full_name, 110)}** | {class_name(character.class_id)} | {designation}\n"
            f"Owned by {owner_label(guild, character.user_id)}"
        )
    if not entries:
        lines.append(
            "No registered characters match these filters. Change a filter or use **Reset Filters** to browse the whole roster. "
            "Choose **My Characters** to view or register your own characters."
        )
    embed = discord.Embed(
        title=f"Guild {class_filter} Roster" if class_id else "Guild Character Roster",
        description="\n\n".join(lines),
        colour=controller.settings.panel_accent_color,
    )
    embed.set_footer(
        text=f"Page {selected_page + 1}/{pages} • {len(characters)} characters • Self-reported names and ownership"
    )
    await controller.send(
        interaction,
        embed=embed,
        view=GuildRosterBrowser(
            controller,
            guild.id,
            interaction.user.id,
            user_id=user_id,
            class_id=class_id,
            kind=kind,
            page=selected_page,
            pages=pages,
        ),
    )


class GuildRosterBrowser(OwnedView):
    """Filters belong to one private menu, never to a guild or public panel."""

    def __init__(
        self,
        controller: CharacterController,
        guild_id: int,
        owner_id: int,
        *,
        user_id: int | None = None,
        class_id: str | None = None,
        kind: str = "all",
        page: int = 0,
        pages: int = 1,
    ):
        super().__init__(controller, guild_id, owner_id)
        require_enabled_filter(controller, class_id)
        self.user_id, self.class_id, self.kind, self.page = (
            user_id,
            class_id,
            kind,
            page,
        )
        self.class_filter = discord.ui.Select(
            placeholder="Filter by class",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="All classes", value="all", default=class_id is None
                ),
                *[
                    discord.SelectOption(
                        label=entry.definition.name,
                        value=entry.id,
                        default=class_id == entry.id,
                    )
                    for entry in controller.settings.display_classes
                ],
            ],
            row=0,
        )
        self.class_filter.callback = self.change_class
        self.add_item(self.class_filter)
        self.kind_filter = discord.ui.Select(
            placeholder="Filter by main or alt",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=value, default=kind == value)
                for value, label in KINDS.items()
            ],
            row=1,
        )
        self.kind_filter.callback = self.change_kind
        self.add_item(self.kind_filter)
        self.member_filter = discord.ui.UserSelect(
            placeholder="Filter by member • clear to show all",
            min_values=0,
            max_values=1,
            default_values=[discord.Object(id=user_id)] if user_id else [],
            row=2,
        )
        self.member_filter.callback = self.change_member
        self.add_item(self.member_filter)
        self.button("Previous", self.previous, row=3, disabled=page == 0)
        self.button("Next", self.next, row=3, disabled=page == pages - 1)
        self.button("Reset Filters", self.reset, row=3)
        self.button("My Characters", self.my_characters, row=3)

    @staticmethod
    def values(
        interaction: discord.Interaction, *, optional: bool = False
    ) -> list[str]:
        # Read this interaction's values; a reused Select's mutable state must
        # not let concurrent clicks borrow another request's filter selection.
        values = (interaction.data or {}).get("values")
        if (
            not isinstance(values, list)
            or len(values) > 1
            or (not optional and not values)
            or not all(isinstance(value, str) for value in values)
        ):
            raise UserError("Choose a value from this roster's filters.")
        return values

    async def change_class(self, interaction: discord.Interaction) -> None:
        async def update(guild: discord.Guild) -> None:
            selected = self.values(interaction)[0]
            require_enabled_filter(
                self.controller, None if selected == "all" else selected
            )
            await self.refresh(
                interaction,
                user_id=self.user_id,
                class_id=None if selected == "all" else selected,
                kind=self.kind,
            )

        await self.run(interaction, update)

    async def change_kind(self, interaction: discord.Interaction) -> None:
        async def update(guild: discord.Guild) -> None:
            selected = self.values(interaction)[0]
            if selected not in KINDS:
                raise UserError("Choose all characters, mains or alts.")
            await self.refresh(
                interaction,
                user_id=self.user_id,
                class_id=self.class_id,
                kind=selected,
            )

        await self.run(interaction, update)

    async def change_member(self, interaction: discord.Interaction) -> None:
        async def update(guild: discord.Guild) -> None:
            values = self.values(interaction, optional=True)
            selected = values[0] if values else None
            if selected is not None and (
                not selected.isascii()
                or not selected.isdecimal()
                or len(selected) > 20
                or not 0 < int(selected) < 2**64
            ):
                raise UserError("Choose a member using the member filter.")
            await self.refresh(
                interaction,
                user_id=int(selected) if selected else None,
                class_id=self.class_id,
                kind=self.kind,
            )

        await self.run(interaction, update)

    async def refresh(
        self,
        interaction: discord.Interaction,
        *,
        user_id: int | None,
        class_id: str | None,
        kind: str,
        page: int = 0,
    ) -> None:
        # OwnedView.run already holds the private-message navigation lock.
        # Render directly rather than entering controller.execute a second time.
        await show_guild_roster(
            self.controller,
            interaction,
            user_id=user_id,
            class_id=class_id,
            kind=kind,
            page=page,
        )

    async def turn_page(self, interaction: discord.Interaction, page: int) -> None:
        async def update(guild: discord.Guild) -> None:
            await self.refresh(
                interaction,
                user_id=self.user_id,
                class_id=self.class_id,
                kind=self.kind,
                page=page,
            )

        await self.run(interaction, update)

    async def previous(self, interaction: discord.Interaction) -> None:
        await self.turn_page(interaction, self.page - 1)

    async def next(self, interaction: discord.Interaction) -> None:
        await self.turn_page(interaction, self.page + 1)

    async def reset(self, interaction: discord.Interaction) -> None:
        async def update(guild: discord.Guild) -> None:
            await self.refresh(interaction, user_id=None, class_id=None, kind="all")

        await self.run(interaction, update)

    async def my_characters(self, interaction: discord.Interaction) -> None:
        # open owns execution and the lock; only check this menu's owner here.
        # Own characters remain available when guild browsing is restricted.
        if await self.interaction_check(interaction):
            await self.controller.open(interaction, manage=False)
