"""Regression coverage for one private message across character workflows."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import http_error, interaction
from test_character_interface import character, roster
from test_character_interface import character_controller as character_controller

from wow_class_manager.character_ui import (
    BackView,
    CharacterModal,
    CharacterPicker,
    CharacterView,
    PurgeConfirmation,
    RemovalConfirmation,
    RosterView,
)
from wow_class_manager.roster_ui import GuildRosterBrowser
from wow_class_manager.storage import Panel, StorageError


def component(guild, *, owner=456, message_id=700, ephemeral=True, admin=False):
    call = interaction(guild, guild.member(owner), admin=admin)
    call.type = discord.InteractionType.component
    call.message = SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=900),
        flags=SimpleNamespace(ephemeral=ephemeral),
    )
    return call


def dashboard(controller, guild, entries, *, message_id=700, pages=1):
    view = RosterView(controller, guild.id, 456, 0, pages, entries)
    controller.track_view(message_id, view)
    return view


async def test_private_navigation_replaces_message_and_clears_previous_layout(
    character_controller, guild
):
    ui = character_controller
    john = character(main=True)
    ui.manager.roster.return_value = roster(john)
    old = dashboard(ui, guild, [john])
    edit = component(guild)
    await ui.choose(edit, "edit")
    edit.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)
    edit.followup.send.assert_not_called()
    edit.edit_original_response.assert_awaited_once()
    edit_message = edit.edit_original_response.call_args.kwargs
    assert edit_message["content"].startswith("Edit a Character")
    assert edit_message["embed"] is None
    assert "ephemeral" not in edit_message
    assert edit_message["allowed_mentions"].everyone is False
    assert isinstance(edit_message["view"], CharacterPicker)
    assert old.is_finished()

    picker = edit_message["view"]
    back = component(guild)
    await picker.back(back)
    current = back.edit_original_response.call_args.kwargs
    assert current["content"] is None
    assert current["embed"].title == "Your Characters"
    assert isinstance(current["view"], RosterView)
    assert picker.is_finished()
    for operation in ("add", "edit", "remove", "promote", "purge"):
        getattr(ui.manager, operation).assert_not_called()


async def test_public_panel_opens_private_dashboard_without_editing_panel(
    character_controller, guild
):
    ui = character_controller
    channel = guild.channel()
    await ui.store.save_panel(Panel(guild.id, channel.id, 700))
    call = component(guild, ephemeral=False)
    call.channel_id = channel.id
    call.followup.send.return_value = SimpleNamespace(id=701, delete=AsyncMock())
    panel = CharacterView(ui.controller)
    original = panel.to_components()
    await panel.manage(call)
    call.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    call.edit_original_response.assert_not_called()
    message = call.followup.send.call_args.kwargs
    assert message["ephemeral"] and message["wait"]
    assert isinstance(message["view"], RosterView)
    assert panel.to_components() == original
    assert not panel.is_finished()


async def test_opening_modal_keeps_dashboard_usable_when_user_cancels_modal(
    character_controller, guild
):
    ui = character_controller
    old = dashboard(ui, guild, [])
    call = component(guild)
    call.response.send_modal = AsyncMock()
    await old.add(call)
    call.response.send_modal.assert_awaited_once()
    call.response.defer.assert_not_called()
    call.edit_original_response.assert_not_called()
    ui.manager.roster.assert_not_called()
    assert not old.is_finished()
    assert await old.interaction_check(component(guild))


async def test_failed_message_edit_retains_working_controls_for_retry(
    character_controller, guild
):
    ui = character_controller
    john = character(main=True)
    ui.manager.roster.return_value = roster(john)
    old = dashboard(ui, guild, [john])
    failed = component(guild)
    failed.edit_original_response.side_effect = http_error()
    await ui.choose(failed, "edit")
    assert failed.followup.send.call_args.kwargs["ephemeral"]
    assert not old.is_finished()
    retry = component(guild)
    await ui.choose(retry, "edit")
    retry.edit_original_response.assert_awaited_once()
    assert isinstance(
        retry.edit_original_response.call_args.kwargs["view"], CharacterPicker
    )
    assert old.is_finished()


@pytest.mark.parametrize("from_private_message", [False, True])
async def test_modal_submission_uses_its_source_message_when_available(
    character_controller, guild, from_private_message
):
    ui = character_controller
    old = dashboard(ui, guild, [])
    modal = CharacterModal(ui, guild.id, 456)
    modal.primary._value, modal.secondary._value = "John", "Stormwind"
    modal.class_select._values = ["paladin"]
    ui.manager.roster.return_value = roster(character(main=True))
    call = component(guild)
    call.type = discord.InteractionType.modal_submit
    if not from_private_message:
        call.message = None
    await modal.on_submit(call)
    ui.manager.add.assert_not_called()
    call.response.defer.assert_awaited_once_with(
        ephemeral=True, thinking=not from_private_message
    )
    if from_private_message:
        call.edit_original_response.assert_awaited_once()
        call.followup.send.assert_not_called()
        assert old.is_finished()
        response = call.edit_original_response.call_args.kwargs
    else:
        call.edit_original_response.assert_not_called()
        response = call.followup.send.call_args.kwargs
        assert response["ephemeral"]
        assert not old.is_finished()
    assert "Choose a gameplay role" in response["content"]
    selected = (
        component(guild) if from_private_message else interaction(guild, call.user)
    )
    selected.data = {"values": ["healing"]}
    await response["view"].selected(selected)
    ui.manager.add.assert_awaited_once_with(
        guild, 456, "John", "Stormwind", "paladin", "healing"
    )
    saved = (
        selected.edit_original_response.call_args.kwargs
        if from_private_message
        else selected.followup.send.call_args.kwargs
    )
    assert "registered as your main Paladin" in saved["content"]
    assert saved["view"].character_select.options[0].default


async def test_selection_only_browses_until_make_main_is_clicked(
    character_controller, guild
):
    ui = character_controller
    john = character(main=True)
    sam = character(2, primary="Sam", secondary="Ironforge", class_id="warrior")
    old = dashboard(ui, guild, [john, sam])
    ui.manager.roster.return_value = roster(john, sam)
    select = component(guild)
    select.data = {"values": ["2"]}
    await old.character_select.callback(select)
    ui.manager.promote.assert_not_called()
    ui.manager.roster.assert_awaited_once_with(guild, 456, reconcile=False)
    selected_view = select.edit_original_response.call_args.kwargs["view"]
    assert selected_view.selected.id == 2
    assert old.is_finished()
    ui.manager.roster.reset_mock()
    ui.manager.roster.return_value = roster(
        character(),
        character(
            2, primary="Sam", secondary="Ironforge", class_id="warrior", main=True
        ),
    )
    call = component(guild)
    await selected_view.main(call)
    ui.manager.promote.assert_awaited_once_with(guild, 456, 2)
    ui.manager.roster.assert_awaited_once_with(guild, 456, reconcile=False)
    ui.manager.add.assert_not_called()
    ui.manager.remove.assert_not_called()
    response = call.edit_original_response.call_args.kwargs
    assert "John Stormwind is now an alt" in response["content"]
    assert (
        "Sam Ironforge**\nWarrior • Role not set • Main"
        in response["embed"].description
    )
    assert (
        "John Stormwind**\nPaladin • Role not set • Alt"
        in response["embed"].description
    )
    options = response["view"].character_select.options
    assert [option.value for option in options if option.default] == ["2"]
    assert (
        "Current Main"
        in next(option for option in options if option.value == "2").description
    )
    assert selected_view.is_finished()


async def test_other_member_cannot_edit_private_message(character_controller, guild):
    ui = character_controller
    old = dashboard(ui, guild, [character(main=True)])
    call = component(guild, owner=789)
    call.data = {"values": ["1"]}
    await old.main(call)
    call.response.defer.assert_not_called()
    call.edit_original_response.assert_not_called()
    ui.manager.promote.assert_not_called()
    assert call.response.send_message.call_args.kwargs["ephemeral"]
    assert "another member" in call.response.send_message.call_args.kwargs["content"]
    assert not old.is_finished()


async def test_old_click_is_rejected_when_menu_changes_during_acknowledgement(
    character_controller, guild
):
    ui = character_controller
    old = dashboard(ui, guild, [character(main=True)])
    replacement = BackView(ui, guild.id, 456)
    call = component(guild)
    call.data = {"values": ["1"]}
    acknowledge = call.response.defer.side_effect

    async def replace_while_deferring(**kwargs):
        await acknowledge(**kwargs)
        ui.track_view(call.message.id, replacement)

    call.response.defer.side_effect = replace_while_deferring
    await old.main(call)
    ui.manager.promote.assert_not_called()
    call.edit_original_response.assert_not_called()
    assert "menu has just changed" in call.followup.send.call_args.kwargs["content"]
    assert ui._private_views[700] is replacement


async def test_concurrent_navigation_cannot_overwrite_new_page_with_old_action(
    character_controller, guild
):
    ui = character_controller
    entries = [
        character(index, primary=f"Player{index}", main=index == 1)
        for index in range(1, 31)
    ]
    old = dashboard(ui, guild, entries[:15], pages=2)
    read_started, release_read, second_acknowledged = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def delayed_roster(*args, **kwargs):
        read_started.set()
        await release_read.wait()
        return roster(*entries)

    ui.manager.roster.side_effect = delayed_roster
    next_page, edit = component(guild), component(guild)
    acknowledge = edit.response.defer.side_effect

    async def acknowledge_second(**kwargs):
        await acknowledge(**kwargs)
        second_acknowledged.set()

    edit.response.defer.side_effect = acknowledge_second
    first_task = asyncio.create_task(old.next(next_page))
    await asyncio.wait_for(read_started.wait(), 3)
    second_task = asyncio.create_task(old.remove(edit))
    try:
        await asyncio.wait_for(second_acknowledged.wait(), 3)
    finally:
        release_read.set()
    await asyncio.wait_for(asyncio.gather(first_task, second_task), 3)
    assert (
        "Page 2/2"
        in next_page.edit_original_response.call_args.kwargs["embed"].footer.text
    )
    edit.edit_original_response.assert_not_called()
    assert "menu has just changed" in edit.followup.send.call_args.kwargs["content"]
    ui.manager.roster.assert_awaited_once()


async def test_refresh_failure_after_success_keeps_confirmation_and_back_action(
    character_controller, guild
):
    ui = character_controller
    old = dashboard(ui, guild, [character(main=True)])
    ui.manager.roster.side_effect = StorageError("SQLite temporarily unavailable")
    call = component(guild)
    call.data = {"values": ["1"]}
    await old.main(call)
    ui.manager.promote.assert_awaited_once()
    response = call.edit_original_response.call_args.kwargs
    assert response["content"].startswith(ui.manager.promote.return_value)
    assert "roster could not refresh" in response["content"]
    assert isinstance(response["view"], BackView)
    call.followup.send.assert_not_called()


async def test_real_guild_browser_filter_reuses_private_message_without_deadlock(
    character_controller, guild
):
    ui = character_controller
    old = dashboard(ui, guild, [character(main=True)])
    browse = component(guild)
    await asyncio.wait_for(ui.guild_roster(browse), 1)
    browser = browse.edit_original_response.call_args.kwargs["view"]
    assert isinstance(browser, GuildRosterBrowser)
    assert old.is_finished()
    choose_class = component(guild)
    choose_class.data = {"values": ["paladin"]}
    await asyncio.wait_for(browser.change_class(choose_class), 1)
    ui.manager.guild_roster.assert_awaited_with(
        guild, choose_class.user, user_id=None, class_id="paladin", kind="all"
    )
    choose_class.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)
    choose_class.followup.send.assert_not_called()
    current = choose_class.edit_original_response.call_args.kwargs
    assert current["embed"].title == "Guild Paladin Roster"
    assert current["view"].class_id == "paladin"
    assert browser.is_finished()


async def test_removing_last_character_returns_empty_dashboard(
    character_controller, guild
):
    ui = character_controller
    john = character(main=True)
    ui.manager.roster.side_effect = [roster(john), roster()]
    confirmation = RemovalConfirmation(ui, guild.id, 456, john, None)
    ui.track_view(700, confirmation)
    call = component(guild)
    await confirmation.confirm(call)
    ui.manager.remove.assert_awaited_once_with(guild, 456, john.id, replacement_id=None)
    response = call.edit_original_response.call_args.kwargs
    assert response["content"] == "Character removed."
    assert "Start with Add Character" in response["embed"].description
    assert isinstance(response["view"], RosterView)
    assert not any(
        isinstance(item, discord.ui.Select) for item in response["view"].children
    )
    assert any(
        item.label == "Add Character" and not item.disabled
        for item in response["view"].children
    )


@pytest.mark.parametrize("purge", [False, True])
async def test_cancel_queued_after_deletion_never_claims_data_was_unchanged(
    character_controller, guild, purge
):
    ui = character_controller
    john = character(main=True)
    ui.manager.roster.return_value = roster(john)
    confirmation = (
        PurgeConfirmation(ui, guild.id, 456, 789)
        if purge
        else RemovalConfirmation(ui, guild.id, 456, john, None)
    )
    ui.track_view(700, confirmation)
    started, release, cancel_acknowledged = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    operation = ui.manager.purge if purge else ui.manager.remove

    async def delete(*args, **kwargs):
        started.set()
        await release.wait()
        ui.manager.roster.return_value = roster()
        return "Stored character data deleted." if purge else "Character removed."

    operation.side_effect = delete
    confirm, cancel = component(guild, admin=True), component(guild, admin=True)
    acknowledge = cancel.response.defer.side_effect

    async def acknowledge_cancel(**kwargs):
        await acknowledge(**kwargs)
        cancel_acknowledged.set()

    cancel.response.defer.side_effect = acknowledge_cancel
    first_task = asyncio.create_task(confirmation.confirm(confirm))
    await asyncio.wait_for(started.wait(), 3)
    second_task = asyncio.create_task(confirmation.cancel(cancel))
    try:
        await asyncio.wait_for(cancel_acknowledged.wait(), 3)
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(first_task, second_task), 3)
    operation.assert_awaited_once()
    assert (
        "deleted" in confirm.edit_original_response.call_args.kwargs["content"]
        or "removed" in confirm.edit_original_response.call_args.kwargs["content"]
    )
    cancel.edit_original_response.assert_not_called()
    error = cancel.followup.send.call_args.kwargs["content"].lower()
    assert "cancelled" not in error and "unchanged" not in error
    assert "menu has just changed" in error


@pytest.mark.parametrize("ephemeral,author", [(False, 900), (True, 901)])
async def test_deferred_update_never_edits_public_or_other_bot_message(
    character_controller, guild, ephemeral, author
):
    call = component(guild, ephemeral=ephemeral)
    call.message.author.id = author
    await call.response.defer(ephemeral=True, thinking=False)
    await character_controller.send(call, content="Private result")
    call.edit_original_response.assert_not_called()
    assert call.followup.send.call_args.kwargs["ephemeral"]
