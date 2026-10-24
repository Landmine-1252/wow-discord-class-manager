from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest
from conftest import interaction

from wow_class_manager.bot import ClassBot
from wow_class_manager.character_commands import (
    build_character_commands,
    build_roster_command,
)
from wow_class_manager.character_ui import (
    COMMUNITY_ROSTER_ID,
    MANAGE_ID,
    MY_CHARACTERS_ID,
    PAGE_SIZE,
    CharacterController,
    CharacterModal,
    CharacterPicker,
    CharacterRolePicker,
    CharacterView,
    PurgeConfirmation,
    RemovalConfirmation,
    RosterView,
    safe_text,
)
from wow_class_manager.commands import Controller
from wow_class_manager.logging_setup import Redactor
from wow_class_manager.roles import UserError
from wow_class_manager.storage import Panel


def character(
    number=1,
    *,
    owner=456,
    guild=123,
    primary="John",
    secondary="Stormwind",
    class_id="paladin",
    gameplay_role=None,
    main=False,
    incomplete=False,
):
    return SimpleNamespace(
        id=number,
        user_id=owner,
        guild_id=guild,
        primary_name=primary,
        secondary_name=secondary,
        full_name=f"{primary} {secondary}".strip(),
        class_id=class_id,
        gameplay_role=gameplay_role,
        is_main=main,
        needs_completion=incomplete,
        updated_at="2026-10-23T00:00:00+00:00",
    )


def roster(*characters):
    main = next((entry for entry in characters if entry.is_main), None)
    return SimpleNamespace(
        characters=characters, main=main, main_character_id=main.id if main else None
    )


@pytest.fixture
def character_controller(settings, store, locks):
    manager = SimpleNamespace(
        roster=AsyncMock(return_value=roster()),
        add=AsyncMock(
            return_value="John Stormwind has been registered as your main Paladin."
        ),
        edit=AsyncMock(return_value="Your character was updated."),
        promote=AsyncMock(
            return_value="Your main is now Sam Ironforge (Warrior). John Stormwind is now an alt."
        ),
        remove=AsyncMock(return_value="Character removed."),
        purge=AsyncMock(
            return_value="Stored character data deleted. Discord roles are unchanged."
        ),
        guild_roster=AsyncMock(return_value=[]),
    )
    bot = SimpleNamespace(
        settings=settings,
        store=store,
        locks=locks,
        characters=manager,
        user=SimpleNamespace(id=900),
        redact=Redactor(),
        members=SimpleNamespace(
            current=AsyncMock(return_value="Your current class: Paladin.")
        ),
        notifier=SimpleNamespace(notify=lambda *args: None),
    )
    root = Controller(bot)
    ui = CharacterController(root)
    root.characters_ui = ui
    return ui


async def test_public_character_view_is_persistent_and_neutral(character_controller):
    view = CharacterView(character_controller.controller)
    assert view.is_persistent()
    assert [(item.label, item.custom_id) for item in view.children] == [
        ("My Characters", MANAGE_ID),
        ("Community Roster", COMMUNITY_ROSTER_ID),
    ]
    assert not any(isinstance(item, discord.ui.Select) for item in view.children)
    registered = CharacterView(character_controller.controller, register_all=True)
    assert MY_CHARACTERS_ID in {item.custom_id for item in registered.children}


@pytest.mark.parametrize("action", ["main", "edit", "remove"])
async def test_empty_character_actions_offer_working_add_button(
    character_controller, guild, action
):
    call = interaction(guild, guild.member())
    await character_controller.choose(call, action)
    reply = call.followup.send.call_args.kwargs
    assert reply["ephemeral"]
    assert "first character" in reply["content"]
    assert reply["embed"].title == "Your Characters"
    add = next(item for item in reply["view"].children if item.label == "Add Character")
    assert not add.disabled
    click = interaction(guild, call.user)
    click.response.send_modal = AsyncMock()
    await add.callback(click)
    modal = click.response.send_modal.call_args.args[0]
    assert isinstance(modal, CharacterModal)
    for operation in ("add", "edit", "remove", "promote"):
        getattr(character_controller.manager, operation).assert_not_called()


async def test_partial_guild_character_workflow_explains_installation_before_api(
    character_controller, guild
):
    call = interaction(guild, guild.member())
    guild.me = None
    await character_controller.open(call)
    reply = call.response.send_message.call_args.kwargs
    assert reply["ephemeral"] and "Guild Install" in reply["content"]
    character_controller.manager.roster.assert_not_called()
    character_controller.bot.members.current.assert_not_called()
    guild.fetch_member.assert_not_called()


@pytest.mark.parametrize("visibility,enabled", [("disabled", True), ("members", False)])
async def test_roster_button_hidden_but_old_callback_can_still_be_registered(
    character_controller, visibility, enabled
):
    root = character_controller.controller
    root.settings = replace(
        root.settings, roster_visibility=visibility, enable_guild_roster=enabled
    )
    published = CharacterView(root)
    registered = CharacterView(root, register_all=True)
    assert COMMUNITY_ROSTER_ID not in {item.custom_id for item in published.children}
    assert COMMUNITY_ROSTER_ID in {item.custom_id for item in registered.children}
    assert registered.is_persistent()


async def test_registration_modal_has_separate_names_and_native_class_select(
    character_controller,
):
    modal = CharacterModal(character_controller, 123, 456)
    assert [item.text for item in modal.children] == [
        "First Name",
        "Secondary Name",
        "Class",
    ]
    assert modal.primary.custom_id == "primary_name"
    assert modal.secondary.custom_id == "secondary_name"
    assert modal.primary.required and modal.secondary.required
    assert modal.class_select.min_values == modal.class_select.max_values == 1
    assert len(modal.class_select.options) == 9
    data = modal.to_dict()
    assert [item["type"] for item in data["components"]] == [18, 18, 18]
    inputs = [item["component"] for item in data["components"][:2]]
    assert [item["custom_id"] for item in inputs] == ["primary_name", "secondary_name"]
    assert all(item["type"] == 4 and item["style"] == 1 for item in inputs)
    assert data["components"][2]["component"]["type"] == 3


async def test_modal_registration_defers_and_confirms_privately(
    character_controller, guild
):
    modal = CharacterModal(character_controller, guild.id, 456)
    modal.primary._value = "John"
    modal.secondary._value = "Stormwind"
    modal.class_select._values = ["paladin"]
    call = interaction(guild, guild.member())
    await modal.on_submit(call)
    picker = call.followup.send.call_args.kwargs["view"]
    assert isinstance(picker, CharacterRolePicker)
    character_controller.manager.add.assert_not_called()
    selected = interaction(guild, guild.member())
    selected.data = {"values": ["healing"]}
    await picker.selected(selected)
    character_controller.manager.add.assert_awaited_once_with(
        guild, 456, "John", "Stormwind", "paladin", "healing"
    )
    call.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert call.followup.send.call_args.kwargs["ephemeral"]
    assert call.followup.send.call_args.kwargs["allowed_mentions"].everyone is False


async def test_modal_cannot_modify_another_members_roster(character_controller, guild):
    modal = CharacterModal(character_controller, guild.id, 456)
    call = interaction(guild, guild.member(789))
    await modal.on_submit(call)
    character_controller.manager.add.assert_not_called()
    assert "another member" in call.response.send_message.call_args.kwargs["content"]


async def test_view_cannot_cross_guild_or_owner(character_controller, guild):
    picker = CharacterPicker(character_controller, guild.id, 456, [character()], "main")
    wrong_owner = interaction(guild, guild.member(789))
    wrong_owner.data = {"values": ["1"]}
    await picker.selected(wrong_owner)
    character_controller.manager.promote.assert_not_called()
    wrong_guild = interaction(guild, guild.member(456))
    picker.guild_id = 999
    assert not await picker.interaction_check(wrong_guild)


async def test_edit_modal_preserves_id_and_both_independent_fields(
    character_controller, guild
):
    existing = character(main=True)
    modal = CharacterModal(character_controller, guild.id, 456, existing)
    modal.primary._value, modal.secondary._value = "John", "Ironforge"
    modal.class_select._values = ["paladin"]
    call = interaction(guild, guild.member())
    await modal.on_submit(call)
    picker = call.followup.send.call_args.kwargs["view"]
    selected = interaction(guild, guild.member())
    selected.data = {"values": ["tank"]}
    await picker.selected(selected)
    character_controller.manager.edit.assert_awaited_once_with(
        guild,
        456,
        existing.id,
        "John",
        "Ironforge",
        "paladin",
        "tank",
        expected_updated_at=existing.updated_at,
    )
    character_controller.manager.add.assert_not_called()


async def test_edit_picker_opens_modal_before_storage_or_network_reads(
    character_controller, guild
):
    existing = character(42, main=True)
    picker = CharacterPicker(character_controller, guild.id, 456, [existing], "edit")
    call = interaction(guild, guild.member())
    call.data = {"values": ["42"]}
    call.response.send_modal = AsyncMock()
    await picker.selected(call)
    call.response.send_modal.assert_awaited_once()
    call.response.defer.assert_not_called()
    character_controller.manager.roster.assert_not_called()
    character_controller.manager.edit.assert_not_called()


async def test_class_edit_disabled_hides_class_select(character_controller):
    character_controller.settings = replace(
        character_controller.settings, allow_character_class_edit=False
    )
    modal = CharacterModal(character_controller, 123, 456, character())
    assert modal.class_select is None
    assert len(modal.children) == 2


async def test_legacy_names_are_not_truncated_or_invented_in_modal(
    character_controller,
):
    old = character(
        primary="Legacy unsplit name that is too long",
        secondary="",
        class_id="x" * 300,
        incomplete=True,
    )
    modal = CharacterModal(character_controller, 123, 456, old)
    assert not modal.primary.default and not modal.secondary.default
    assert old.primary_name == "Legacy unsplit name that is too long"
    assert old.secondary_name == ""
    assert "preserved" in modal.children[0].description
    assert all(len(option.value) <= 100 for option in modal.class_select.options)


async def test_disabled_class_can_be_kept_for_rename_without_being_a_choice(
    character_controller,
    guild,
):
    old = character(class_id="evoker")
    modal = CharacterModal(character_controller, 123, 456, old)
    assert "evoker" not in {entry.value for entry in modal.class_select.options}
    assert not modal.class_select.required
    assert modal.class_select.min_values == 0
    modal.primary._value, modal.secondary._value = "John", "Ironforge"
    modal.class_select._values = []
    call = interaction(guild, guild.member())
    await modal.on_submit(call)
    picker = call.followup.send.call_args.kwargs["view"]
    selected = interaction(guild, guild.member())
    selected.data = {"values": ["healing"]}
    await picker.selected(selected)
    character_controller.manager.edit.assert_awaited_once_with(
        guild,
        456,
        old.id,
        "John",
        "Ironforge",
        "evoker",
        "healing",
        expected_updated_at=old.updated_at,
    )
    new_modal = CharacterModal(character_controller, 123, 456)
    assert "evoker" not in {entry.value for entry in new_modal.class_select.options}
    assert new_modal.class_select.required and new_modal.class_select.min_values == 1


async def test_roster_and_picker_paginate_more_than_25_characters(
    character_controller, guild
):
    entries = [
        character(number, secondary=f"Family{number}", main=number == 1)
        for number in range(1, 52)
    ]
    character_controller.manager.roster.return_value = roster(*entries)
    call = interaction(guild, guild.member())
    await character_controller.open(call, page=1)
    message = call.followup.send.call_args.kwargs
    assert "Page 2/4" in message["embed"].footer.text
    assert len(message["embed"].description) <= 4096
    assert (
        "Family16" in message["embed"].description
        and "Family31" not in message["embed"].description
    )
    picker = CharacterPicker(
        character_controller, guild.id, 456, entries, "main", page=2
    )
    assert len(picker.select.options) == 15
    assert len(picker.select.options) <= 25
    assert picker.select.options[0].value == "31"
    assert PAGE_SIZE == 15


async def test_selecting_main_is_one_service_operation(character_controller, guild):
    first, second = character(main=True), character(
        2, primary="Sam", secondary="Ironforge", class_id="warrior"
    )
    character_controller.manager.roster.return_value = roster(first, second)
    picker = CharacterPicker(
        character_controller, guild.id, 456, [first, second], "main"
    )
    call = interaction(guild, guild.member())
    call.data = {"values": [str(second.id)]}
    await picker.selected(call)
    character_controller.manager.promote.assert_awaited_once_with(guild, 456, second.id)
    character_controller.manager.remove.assert_not_called()
    character_controller.manager.add.assert_not_called()


async def test_removing_alt_requires_confirmation(character_controller, guild):
    main, alt = character(main=True), character(2, primary="Sam", secondary="Ironforge")
    character_controller.manager.roster.return_value = roster(main, alt)
    call = interaction(guild, guild.member())
    await character_controller.choose(call, "remove", selected=str(alt.id))
    confirmation = call.followup.send.call_args.kwargs["view"]
    assert isinstance(confirmation, RemovalConfirmation)
    character_controller.manager.remove.assert_not_called()
    confirm_call = interaction(guild, guild.members[456])
    await confirmation.confirm(confirm_call)
    character_controller.manager.remove.assert_awaited_once_with(
        guild, 456, alt.id, replacement_id=None
    )


async def test_removing_main_selects_replacement_then_confirms(
    character_controller, guild
):
    main, alt = character(main=True), character(2, primary="Sam", secondary="Ironforge")
    character_controller.manager.roster.return_value = roster(main, alt)
    call = interaction(guild, guild.member())
    await character_controller.choose(call, "remove", selected=str(main.id))
    picker = call.followup.send.call_args.kwargs["view"]
    assert isinstance(picker, CharacterPicker)
    assert [option.value for option in picker.select.options] == [str(alt.id)]
    pick = interaction(guild, guild.members[456])
    pick.data = {"values": [str(alt.id)]}
    await picker.selected(pick)
    confirmation = pick.followup.send.call_args.kwargs["view"]
    assert isinstance(confirmation, RemovalConfirmation)
    character_controller.manager.promote.assert_not_called()
    character_controller.manager.remove.assert_not_called()
    await confirmation.confirm(interaction(guild, guild.members[456]))
    character_controller.manager.remove.assert_awaited_once_with(
        guild, 456, main.id, replacement_id=alt.id
    )


async def test_removing_last_main_respects_clear_setting(character_controller, guild):
    main = character(main=True)
    character_controller.manager.roster.return_value = roster(main)
    call = interaction(guild, guild.member())
    await character_controller.choose(call, "remove", selected=str(main.id))
    assert "disabled" in call.followup.send.call_args.kwargs["content"]
    character_controller.manager.remove.assert_not_called()
    character_controller.settings = replace(
        character_controller.settings, allow_clear_selection=True
    )
    allowed = interaction(guild, guild.members[456])
    await character_controller.choose(allowed, "remove", selected=str(main.id))
    assert isinstance(
        allowed.followup.send.call_args.kwargs["view"], RemovalConfirmation
    )


async def test_stale_removal_confirmation_rejected(character_controller, guild):
    original = character(main=False)
    confirmation = RemovalConfirmation(
        character_controller, guild.id, 456, original, None
    )
    character_controller.manager.roster.return_value = roster(character(main=True))
    call = interaction(guild, guild.member())
    await confirmation.confirm(call)
    character_controller.manager.remove.assert_not_called()
    assert "changed" in call.followup.send.call_args.kwargs["content"]


async def test_confirmation_cannot_be_replayed(character_controller, guild):
    alt = character()
    character_controller.manager.roster.return_value = roster(alt)
    confirmation = RemovalConfirmation(character_controller, guild.id, 456, alt, None)
    await confirmation.confirm(interaction(guild, guild.member()))
    await confirmation.confirm(interaction(guild, guild.members[456]))
    assert character_controller.manager.remove.await_count == 1


async def test_legacy_class_only_member_is_guided_without_fake_character(
    character_controller, guild
):
    call = interaction(guild, guild.member())
    await character_controller.open(call)
    description = call.followup.send.call_args.kwargs["embed"].description
    assert "Paladin" in description and "Register your main character" in description
    character_controller.manager.add.assert_not_called()


async def test_reconciliation_failure_does_not_hide_editing_or_main_switch(
    character_controller, guild
):
    stored = roster(character(main=True, class_id="evoker"))
    character_controller.manager.roster.side_effect = [
        UserError("Your main's class is disabled. Choose another main."),
        stored,
    ]
    call = interaction(guild, guild.member())
    await character_controller.open(call)
    result = call.followup.send.call_args.kwargs
    assert "disabled" in result["embed"].description
    assert isinstance(result["view"], RosterView)
    assert isinstance(result["view"].character_select, discord.ui.Select)
    assert not result["view"].character_select.disabled


@pytest.mark.parametrize(
    "failure",
    [
        discord.HTTPException(
            SimpleNamespace(status=503, reason="unavailable"),
            "private transport error",
        ),
        aiohttp.ServerDisconnectedError("private transport error"),
        TimeoutError("private transport error"),
    ],
)
async def test_discord_outage_keeps_saved_roster_available_without_error_details(
    character_controller, guild, failure
):
    character_controller.manager.roster.side_effect = [
        failure,
        roster(character(main=True)),
    ]
    call = interaction(guild, guild.member())
    await character_controller.open(call)
    result = call.followup.send.call_args.kwargs
    assert "John Stormwind" in result["embed"].description
    assert "saved roster is available" in result["embed"].description
    assert "private transport error" not in result["embed"].description
    assert isinstance(result["view"], RosterView)
    character_controller.manager.roster.assert_awaited_with(guild, 456, reconcile=False)


async def test_public_panel_active_message_checked_and_unchanged(
    character_controller, guild, store
):
    channel = guild.channel()
    message = await channel.send(view=CharacterView(character_controller.controller))
    await store.save_panel(Panel(guild.id, channel.id, message.id))
    call = interaction(guild, guild.member(), channel)
    call.message = message
    await character_controller.open(call, panel=True)
    message.edit.assert_not_called()
    assert call.followup.send.call_args.kwargs["ephemeral"]
    call.message = SimpleNamespace(id=999, author=SimpleNamespace(id=900))
    await character_controller.open(call, panel=True)
    assert "no longer active" in call.followup.send.call_args.kwargs["content"]


@pytest.mark.parametrize(
    "visibility,enabled", [("disabled", True), ("members", False), ("admins", True)]
)
async def test_guild_roster_visibility_enforced(
    character_controller, guild, visibility, enabled
):
    character_controller.settings = replace(
        character_controller.settings,
        roster_visibility=visibility,
        enable_guild_roster=enabled,
    )
    call = interaction(guild, guild.member())
    await character_controller.guild_roster(call)
    character_controller.manager.guild_roster.assert_not_called()
    assert call.followup.send.call_args.kwargs["ephemeral"]


async def test_guild_roster_uses_filters_and_pagination(character_controller, guild):
    entries = [
        character(
            number,
            primary="Sam",
            secondary=f"Family{number}",
            class_id="warrior",
            main=number == 1,
        )
        for number in range(1, 32)
    ]
    character_controller.manager.guild_roster.return_value = entries
    call = interaction(guild, guild.member())
    await character_controller.guild_roster(
        call, class_id="warrior", kind="alts", user_id=456, page=1
    )
    character_controller.manager.guild_roster.assert_awaited_once_with(
        guild, call.user, user_id=456, class_id="warrior", kind="alts"
    )
    sent = call.followup.send.call_args.kwargs
    assert "Page 2/3" in sent["embed"].footer.text
    assert "Sam Family16" in sent["embed"].description
    assert "Owned by <@456>" in sent["embed"].description
    assert sent["allowed_mentions"].everyone is False


async def test_admin_data_deletion_requires_fresh_authorization_and_confirmation(
    character_controller, guild
):
    call = interaction(guild, guild.member(), admin=True)
    await character_controller.purge_prompt(call, SimpleNamespace(id=999))
    sent = call.followup.send.call_args.kwargs
    assert "roles remain unchanged" in sent["content"]
    assert isinstance(sent["view"], PurgeConfirmation)
    character_controller.manager.purge.assert_not_called()
    unauthorized = interaction(guild, guild.members[456])
    await sent["view"].confirm(unauthorized)
    character_controller.manager.purge.assert_not_called()
    authorized = interaction(guild, guild.members[456], admin=True)
    await sent["view"].confirm(authorized)
    character_controller.manager.purge.assert_awaited_once_with(
        guild, authorized.user, 999
    )


def test_safe_legacy_display():
    shown = safe_text("@everyone **unsafe**\n\ud800\u200d")
    assert (
        "@everyone" not in shown
        and "\n" not in shown
        and "\ud800" not in shown
        and "\u200d" not in shown
    )
    assert "\\*" in shown


def test_command_builders_and_autocomplete_registration(character_controller):
    commands = build_character_commands(character_controller)
    assert commands.name == "character"
    assert {command.name for command in commands.commands} == {
        "add",
        "list",
        "main",
        "edit",
        "remove",
        "help",
        "purge",
    }
    assert commands.get_command("main").parameters[0].autocomplete
    roster_command = build_roster_command(character_controller)
    assert roster_command.name == "roster"
    assert {parameter.name for parameter in roster_command.parameters} == {
        "member",
        "wow_class",
        "kind",
    }


async def test_modal_to_main_switch_to_replacement_removal_with_real_storage(
    settings, store, guild
):
    from conftest import FakeRole

    bot = ClassBot(settings, store, Redactor())
    await bot.roles.reconcile(guild)
    unrelated = FakeRole(77777, "Guild Rank")
    guild.roles.append(unrelated)
    member = guild.member(roles=[unrelated])
    ui = bot.controller.characters_ui

    async def register(primary, secondary, class_id):
        modal = CharacterModal(ui, guild.id, member.id)
        modal.primary._value, modal.secondary._value = primary, secondary
        modal.class_select._values = [class_id]
        call = interaction(guild, member)
        await modal.on_submit(call)
        picker = call.followup.send.call_args.kwargs["view"]
        selected = interaction(guild, member)
        selected.data = {"values": ["tank"]}
        await picker.selected(selected)
        return selected.followup.send.call_args.kwargs["content"]

    assert "registered as your main Paladin" in await register(
        "John", "Stormwind", "paladin"
    )
    assert "Warrior alt" in await register("Sam", "Ironforge", "warrior")
    saved = await bot.characters.roster(guild, member.id, reconcile=False)
    john, sam = saved.characters
    assert john.gameplay_role == sam.gameplay_role == "tank"
    assert (john.primary_name, john.secondary_name, john.is_main) == (
        "John",
        "Stormwind",
        True,
    )
    assert not sam.is_main
    mapping = await store.roles(guild.id)
    tags = await store.gameplay_roles(guild.id)
    assert {entry.id for entry in member.roles} == {
        unrelated.id,
        mapping["paladin"],
        tags["tank"],
    }

    change = CharacterPicker(ui, guild.id, member.id, saved.characters, "main")
    choose = interaction(guild, member)
    choose.data = {"values": [str(sam.id)]}
    await change.selected(choose)
    assert (
        "John Stormwind is now an alt"
        in choose.followup.send.call_args.kwargs["content"]
    )
    switched = await bot.characters.roster(guild, member.id, reconcile=False)
    assert switched.main.id == sam.id and len(switched.characters) == 2
    assert {entry.id for entry in member.roles} == {
        unrelated.id,
        mapping["warrior"],
        tags["tank"],
    }

    remove = interaction(guild, member)
    await ui.choose(remove, "remove", selected=str(sam.id))
    replacement = remove.followup.send.call_args.kwargs["view"]
    replacement_call = interaction(guild, member)
    replacement_call.data = {"values": [str(john.id)]}
    await replacement.selected(replacement_call)
    confirmation = replacement_call.followup.send.call_args.kwargs["view"]
    assert (
        len((await bot.characters.roster(guild, member.id, reconcile=False)).characters)
        == 2
    )
    await confirmation.confirm(interaction(guild, member))
    final = await bot.characters.roster(guild, member.id, reconcile=False)
    assert len(final.characters) == 1 and final.main.id == john.id
    assert {entry.id for entry in member.roles} == {
        unrelated.id,
        mapping["paladin"],
        tags["tank"],
    }
    assert await bot.characters.records.pending(guild.id, member.id) is None


async def test_maximum_name_lengths_fit_roster_and_guild_embed(
    character_controller, guild
):
    entries = [
        character(
            index,
            primary="A" * 50,
            secondary="B" * 49 + chr(65 + index),
            main=index == 1,
            class_id="death_knight",
        )
        for index in range(1, PAGE_SIZE + 1)
    ]
    stored = roster(*entries)
    character_controller.manager.roster.return_value = stored
    call = interaction(guild, guild.member())
    await character_controller.open(call)
    description = call.followup.send.call_args.kwargs["embed"].description
    assert len(description) <= 4096
    assert all(entry.full_name in description for entry in entries)
    picker = CharacterPicker(character_controller, guild.id, 456, entries, "main")
    assert all(
        len(option.label) <= 100 and len(option.description) <= 100
        for option in picker.select.options
    )
    assert entries[0].secondary_name in picker.select.options[0].description
    character_controller.manager.guild_roster.return_value = entries
    guild.get_member = lambda user_id: SimpleNamespace(display_name="*" * 100)
    guild_call = interaction(guild, guild.members[456])
    await character_controller.guild_roster(guild_call)
    guild_description = guild_call.followup.send.call_args.kwargs["embed"].description
    assert len(guild_description) <= 4096
    assert all(entry.full_name in guild_description for entry in entries)


async def test_legacy_roster_page_bounds_include_every_entry_and_warning(
    character_controller, guild
):
    entries = [
        character(
            index,
            primary=f"Legacy{index:02d}" + "*" * 500,
            secondary="",
            incomplete=True,
            class_id="unknown_legacy_class",
        )
        for index in range(1, PAGE_SIZE + 1)
    ]
    call = interaction(guild, guild.member())
    await character_controller.send_roster(
        call,
        roster(*entries),
        warning="*" * 1000,
    )
    description = call.response.send_message.call_args.kwargs["embed"].description
    assert len(description) <= 4096
    assert all(f"Legacy{index:02d}" in description for index in range(1, PAGE_SIZE + 1))
    assert "Use Edit" in description
