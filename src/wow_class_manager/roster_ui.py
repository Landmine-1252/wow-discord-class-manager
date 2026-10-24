"""A private, searchable guild directory with compact member detail views."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

import discord

from .character_ui import (
    PRIVATE_TIMEOUT,
    OwnedLayoutView,
    class_color,
    class_emoji,
    class_name,
    role_badge,
    safe_text,
)
from .classes import GAMEPLAY_ROLE_TAGS
from .roles import UserError
from .roster_directory import (
    KINDS,
    SEARCH_LIMIT,
    Directory,
    RosterFilters,
    character_order,
    directory_page,
)

if TYPE_CHECKING:
    from .character_ui import CharacterController

ROLE_FILTERS = {**GAMEPLAY_ROLE_TAGS, "unset": "Role not set"}


def require_roster_access(
    controller: CharacterController, interaction: discord.Interaction
) -> discord.Guild:
    """Recheck policy before every read or modal launch, including older menus."""
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
            "Manage Server is required to view this guild roster. You can still view My"
            " Characters."
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


def validate_filters(controller: CharacterController, filters: RosterFilters) -> None:
    require_enabled_filter(controller, filters.class_id)
    if filters.kind not in KINDS:
        raise UserError("Choose all characters, mains or alts.")
    if filters.gameplay_role is not None and filters.gameplay_role not in ROLE_FILTERS:
        raise UserError("Choose a gameplay role or All roles from the roster filters.")
    if filters.user_id is not None and (
        isinstance(filters.user_id, bool)
        or not isinstance(filters.user_id, int)
        or not 0 < filters.user_id < 2**64
    ):
        raise UserError("Choose a member using the member search.")
    if not isinstance(filters.query, str) or len(filters.query) > SEARCH_LIMIT:
        raise UserError(f"Keep character searches within {SEARCH_LIMIT} characters.")


def filter_label(guild: discord.Guild, filters: RosterFilters) -> str:
    labels = [KINDS[filters.kind]]
    if filters.class_id:
        labels.append(f"{class_emoji(filters.class_id)} {class_name(filters.class_id)}")
    if filters.gameplay_role:
        labels.append(ROLE_FILTERS[filters.gameplay_role])
    if filters.user_id:
        labels.append(owner_label(guild, filters.user_id))
    if filters.query:
        labels.append(f"Name: “{safe_text(filters.query, SEARCH_LIMIT)}”")
    return " · ".join(labels)


def action(
    label: str,
    callback: Callable[[discord.Interaction], Awaitable[None]],
    *,
    emoji: str | None = None,
    primary: bool = False,
    disabled: bool = False,
    custom_id: str | None = None,
) -> discord.ui.Button:
    button = discord.ui.Button(
        label=label,
        emoji=emoji,
        style=discord.ButtonStyle.primary if primary else discord.ButtonStyle.secondary,
        disabled=disabled,
        **({"custom_id": custom_id} if custom_id else {}),
    )
    button.callback = callback
    return button


async def show_guild_roster(
    controller: CharacterController,
    interaction: discord.Interaction,
    *,
    user_id: int | None = None,
    class_id: str | None = None,
    gameplay_role: str | None = None,
    kind: str = "mains",
    query: str = "",
    page: int = 0,
    filters_open: bool = False,
) -> None:
    """Use one authorized snapshot for the overview and all visible entries."""
    guild = require_roster_access(controller, interaction)
    filters = RosterFilters(
        user_id=user_id,
        class_id=class_id,
        gameplay_role=gameplay_role,
        kind=kind,
        query=query,
    )
    validate_filters(controller, filters)
    characters = await controller.manager.guild_roster(guild, interaction.user)
    directory = Directory(characters, filters)
    view_type = RosterFilterView if filters_open else GuildRosterBrowser
    await controller.send(
        interaction,
        view=view_type(
            controller, guild, interaction.user.id, directory, filters, page
        ),
    )


class DirectoryView(OwnedLayoutView):
    def __init__(
        self,
        controller: CharacterController,
        guild_id: int,
        owner_id: int,
        filters: RosterFilters,
        page: int,
    ):
        super().__init__(controller, guild_id, owner_id)
        self.filters, self.page = filters, page

    async def refresh(
        self,
        interaction: discord.Interaction,
        *,
        filters: RosterFilters | None = None,
        page: int = 0,
        filters_open: bool = False,
    ) -> None:
        # The caller already holds the shared private-message navigation lock.
        selected = filters if filters is not None else self.filters
        await show_guild_roster(
            self.controller,
            interaction,
            user_id=selected.user_id,
            class_id=selected.class_id,
            gameplay_role=selected.gameplay_role,
            kind=selected.kind,
            query=selected.query,
            page=page,
            filters_open=filters_open,
        )

    async def return_to_roster(self, interaction: discord.Interaction) -> None:
        async def show(guild: discord.Guild) -> None:
            await self.refresh(interaction, page=self.page)

        await self.run(interaction, show)

    async def reset(self, interaction: discord.Interaction) -> None:
        async def show(guild: discord.Guild) -> None:
            await self.refresh(interaction, filters=RosterFilters())

        await self.run(interaction, show)

    async def my_characters(self, interaction: discord.Interaction) -> None:
        if await self.interaction_check(interaction):
            await self.controller.open(interaction)

    async def search(self, interaction: discord.Interaction) -> None:
        async def launch(guild: discord.Guild) -> None:
            require_roster_access(self.controller, interaction)
            await interaction.response.send_modal(RosterSearchModal(self))

        await self.run(interaction, launch, defer=False)


class GuildRosterBrowser(DirectoryView):
    def __init__(
        self,
        controller: CharacterController,
        guild: discord.Guild,
        owner_id: int,
        directory: Directory,
        filters: RosterFilters,
        page: int = 0,
    ):
        validate_filters(controller, filters)
        entries, page, pages = directory_page(directory.matches, page)
        super().__init__(controller, guild.id, owner_id, filters, page)
        self.pages = pages
        roles = directory.main_roles
        overview = (
            "## Guild Roster\n"
            f"{count_label(len(directory.members), 'registered member')} · "
            f"{count_label(len(directory.characters), 'character')}\n"
            f"-# Main roles · 🛡️ {roles['tank']} Tank · 💚 {roles['healing']} Healing"
            f" · ⚔️ {roles['dps']} DPS · {roles[None]} unset"
        )
        if directory.without_main:
            overview += (
                f"\n-# {count_label(directory.without_main, 'member')} without a main"
                " · find them under All characters."
            )
        card = discord.ui.Container(
            discord.ui.TextDisplay(overview),
            discord.ui.ActionRow(
                action("Search", self.search, emoji="🔎", primary=True),
                action("Filters", self.open_filters, emoji="⚙️"),
                action("Reset", self.reset, disabled=filters == RosterFilters()),
            ),
            discord.ui.TextDisplay(f"-# {filter_label(guild, filters)}"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            accent_color=class_color(controller.settings, filters.class_id),
        )
        for character in entries:
            details = [
                class_name(character.class_id),
                owner_label(guild, character.user_id),
            ]
            if filters.kind != "mains":
                details.append("⭐ Main" if character.is_main else "Alt")
            alts = directory.alt_counts[character.user_id]
            if character.is_main and alts:
                details.append(count_label(alts, "alt"))
            card.add_item(
                discord.ui.Section(
                    f"{class_emoji(character.class_id) or '❔'} "
                    f"**{safe_text(character.full_name, 110)}** · "
                    f"{role_badge(character.gameplay_role)}\n-# {' · '.join(details)}",
                    accessory=action(
                        "View",
                        partial(self.view_member, user_id=character.user_id),
                        custom_id=f"wow:roster:member:v1:{character.id}",
                    ),
                )
            )
        if not entries:
            card.add_item(
                discord.ui.TextDisplay(
                    "**No characters found**\nTry a different name or filter, or use "
                    "**Reset** to return to mains. **My Characters** opens your roster."
                )
            )
        card.add_item(
            discord.ui.TextDisplay(
                f"-# Page {page + 1}/{pages} · "
                f"{count_label(len(directory.matches), 'result')} · A–Z"
                " · Closes after 5 minutes idle"
            )
        )
        card.add_item(
            discord.ui.ActionRow(
                action("Previous", self.previous, disabled=page == 0),
                action("Next", self.next, disabled=page == pages - 1),
                action("My Characters", self.my_characters),
                action("Done", self.done),
            )
        )
        self.add_item(card)

    async def open_filters(self, interaction: discord.Interaction) -> None:
        async def show(guild: discord.Guild) -> None:
            await self.refresh(interaction, page=self.page, filters_open=True)

        await self.run(interaction, show)

    async def turn_page(self, interaction: discord.Interaction, page: int) -> None:
        async def show(guild: discord.Guild) -> None:
            await self.refresh(interaction, page=page)

        await self.run(interaction, show)

    async def previous(self, interaction: discord.Interaction) -> None:
        await self.turn_page(interaction, self.page - 1)

    async def next(self, interaction: discord.Interaction) -> None:
        await self.turn_page(interaction, self.page + 1)

    async def view_member(
        self, interaction: discord.Interaction, *, user_id: int
    ) -> None:
        async def show(guild: discord.Guild) -> None:
            await show_member(self, interaction, user_id)

        await self.run(interaction, show)


class RosterFilterView(DirectoryView):
    def __init__(
        self,
        controller: CharacterController,
        guild: discord.Guild,
        owner_id: int,
        directory: Directory,
        filters: RosterFilters,
        page: int = 0,
    ):
        validate_filters(controller, filters)
        super().__init__(controller, guild.id, owner_id, filters, page)
        self.class_filter = discord.ui.Select(
            placeholder="Class",
            options=[
                discord.SelectOption(
                    label="All classes", value="all", default=filters.class_id is None
                ),
                *[
                    discord.SelectOption(
                        label=entry.definition.name,
                        value=entry.id,
                        emoji=entry.definition.emoji,
                        default=filters.class_id == entry.id,
                    )
                    for entry in controller.settings.display_classes
                ],
            ],
        )
        self.role_filter = discord.ui.Select(
            placeholder="Gameplay role",
            options=[
                discord.SelectOption(
                    label="All roles",
                    value="all",
                    default=filters.gameplay_role is None,
                ),
                *[
                    discord.SelectOption(
                        label=label, value=value, default=filters.gameplay_role == value
                    )
                    for value, label in ROLE_FILTERS.items()
                ],
            ],
        )
        self.kind_filter = discord.ui.Select(
            placeholder="Characters",
            options=[
                discord.SelectOption(
                    label=label, value=value, default=filters.kind == value
                )
                for value, label in KINDS.items()
            ],
        )
        self.class_filter.callback = self.change_class
        self.role_filter.callback = self.change_role
        self.kind_filter.callback = self.change_kind
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "## Filter the roster\n"
                    f"**{count_label(len(directory.matches), 'matching character')}**\n"
                    f"-# {filter_label(guild, filters)}"
                ),
                discord.ui.TextDisplay("**Class**"),
                discord.ui.ActionRow(self.class_filter),
                discord.ui.TextDisplay("**Gameplay role**"),
                discord.ui.ActionRow(self.role_filter),
                discord.ui.TextDisplay("**Characters**"),
                discord.ui.ActionRow(self.kind_filter),
                discord.ui.ActionRow(
                    action("Show Results", self.return_to_roster, primary=True),
                    action("Search / Member", self.search, emoji="🔎"),
                    action("Reset", self.reset),
                    action("Done", self.done),
                ),
                accent_color=class_color(controller.settings, filters.class_id),
            )
        )

    async def change_filter(self, interaction: discord.Interaction, field: str) -> None:
        async def show(guild: discord.Guild) -> None:
            values = (interaction.data or {}).get("values")
            if (
                not isinstance(values, list)
                or len(values) != 1
                or not isinstance(values[0], str)
            ):
                raise UserError("Choose a value from this roster's filters.")
            value = values[0]
            if value == "all" and field != "kind":
                value = None
            selected = replace(self.filters, **{field: value})
            await self.refresh(interaction, filters=selected, filters_open=True)

        await self.run(interaction, show)

    async def change_class(self, interaction: discord.Interaction) -> None:
        await self.change_filter(interaction, "class_id")

    async def change_role(self, interaction: discord.Interaction) -> None:
        await self.change_filter(interaction, "gameplay_role")

    async def change_kind(self, interaction: discord.Interaction) -> None:
        await self.change_filter(interaction, "kind")


class RosterSearchModal(discord.ui.Modal):
    def __init__(self, source: DirectoryView):
        super().__init__(title="Find a character or member", timeout=PRIVATE_TIMEOUT)
        self.source = source
        self.query = discord.ui.TextInput(
            custom_id="wow:roster:search-name:v1",
            placeholder="Full name or part of a name",
            default=source.filters.query or None,
            required=False,
            max_length=SEARCH_LIMIT,
        )
        self.member = discord.ui.UserSelect(
            custom_id="wow:roster:search-member:v1",
            placeholder="Choose a member, or leave blank for everyone",
            min_values=0,
            max_values=1,
            required=False,
            default_values=(
                [discord.Object(id=source.filters.user_id)]
                if source.filters.user_id
                else []
            ),
        )
        self.add_item(
            discord.ui.Label(
                text="Character name",
                description=(
                    "Name searches include alts and keep your class and role filters."
                ),
                component=self.query,
            )
        )
        self.add_item(discord.ui.Label(text="Discord member", component=self.member))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        query = self.query.value.strip()
        members = self.member.values

        async def search(guild: discord.Guild) -> None:
            require_roster_access(self.source.controller, interaction)
            if self.is_finished() or self.source.is_finished():
                raise UserError(
                    "This search has expired. Open Search again to continue."
                )
            if len(members) > 1:
                raise UserError("Choose one member, or clear the member search.")
            filters = replace(
                self.source.filters,
                query=query,
                user_id=members[0].id if members else None,
                kind="all" if query else self.source.filters.kind,
            )
            await self.source.refresh(interaction, filters=filters)
            self.stop()

        await self.source.run(interaction, search)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await self.source.controller.controller.handle_error(
            interaction, error, action="character"
        )


async def show_member(
    source: DirectoryView,
    interaction: discord.Interaction,
    user_id: int,
    detail_page: int = 0,
) -> None:
    guild = require_roster_access(source.controller, interaction)
    characters = await source.controller.manager.guild_roster(
        guild, interaction.user, user_id=user_id
    )
    ordered = sorted(
        characters, key=lambda entry: (not entry.is_main, *character_order(entry))
    )
    entries, detail_page, pages = directory_page(ordered, detail_page)
    view = MemberRosterView(source, user_id, detail_page, pages)
    main = next((entry for entry in ordered if entry.is_main), None)
    card = discord.ui.Container(
        discord.ui.TextDisplay(
            f"## Member Characters\n{owner_label(guild, user_id)}"
            f" · {count_label(len(ordered), 'character')}"
        ),
        discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
        accent_color=class_color(
            source.controller.settings, main.class_id if main else None
        ),
    )
    for character in entries:
        designation = "⭐ Main" if character.is_main else "Alt"
        card.add_item(
            discord.ui.TextDisplay(
                f"{class_emoji(character.class_id) or '❔'} "
                f"**{safe_text(character.full_name, 110)}** · "
                f"{role_badge(character.gameplay_role)}\n"
                f"-# {class_name(character.class_id)} · {designation}"
            )
        )
    if not entries:
        card.add_item(
            discord.ui.TextDisplay(
                "This member no longer has registered characters. "
                "Go back to refresh the guild roster."
            )
        )
    card.add_item(
        discord.ui.TextDisplay(
            f"-# Page {detail_page + 1}/{pages} · Main first, then A–Z"
            " · Closes after 5 minutes idle"
        )
    )
    if pages > 1:
        card.add_item(
            discord.ui.ActionRow(
                action("Previous", view.previous, disabled=detail_page == 0),
                action("Next", view.next, disabled=detail_page == pages - 1),
            )
        )
    card.add_item(
        discord.ui.ActionRow(
            action("Back to Roster", view.return_to_roster, emoji="↩️", primary=True),
            action("Reset", view.reset),
            action("My Characters", view.my_characters),
            action("Done", view.done),
        )
    )
    view.add_item(card)
    await source.controller.send(interaction, view=view)


class MemberRosterView(DirectoryView):
    def __init__(
        self, source: DirectoryView, user_id: int, detail_page: int, pages: int
    ):
        super().__init__(
            source.controller,
            source.guild_id,
            source.owner_id,
            source.filters,
            source.page,
        )
        self.user_id, self.detail_page, self.pages = user_id, detail_page, pages

    async def turn_page(self, interaction: discord.Interaction, page: int) -> None:
        async def show(guild: discord.Guild) -> None:
            await show_member(self, interaction, self.user_id, page)

        await self.run(interaction, show)

    async def previous(self, interaction: discord.Interaction) -> None:
        await self.turn_page(interaction, self.detail_page - 1)

    async def next(self, interaction: discord.Interaction) -> None:
        await self.turn_page(interaction, self.detail_page + 1)
