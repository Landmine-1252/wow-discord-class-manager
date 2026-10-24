"""Private menu lifecycle, selection, and concurrent cleanup regressions."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import http_error, interaction
from test_character_interface import character, roster
from test_character_interface import character_controller as character_controller
from test_character_navigation import component, dashboard

from wow_class_manager.character_ui import (
    PRIVATE_TIMEOUT,
    BackView,
    CharacterModal,
    RemovalConfirmation,
    RetryCharacterView,
)
from wow_class_manager.roles import UserError


def button(view, label):
    return next(item for item in view.children if getattr(item, "label", None) == label)


async def open_menu(ui, guild, *, message_id=700, owner=456, channel=555):
    call = interaction(guild, guild.member(owner))
    call.channel_id = channel
    message = SimpleNamespace(id=message_id, delete=AsyncMock())
    call.followup.send.return_value = message
    await ui.open(call)
    return call.followup.send.call_args.kwargs["view"], message


async def test_selected_character_edits_directly_without_an_extra_picker(
    character_controller, guild
):
    ui = character_controller
    sam = character(2, primary="Sam", secondary="Ironforge")
    view = dashboard(ui, guild, [sam])
    call = component(guild)
    call.response.send_modal = AsyncMock()
    await button(view, "Edit").callback(call)
    modal = call.response.send_modal.call_args.args[0]
    assert isinstance(modal, CharacterModal)
    assert modal.character.id == sam.id
    assert modal.primary.default == "Sam"
    assert modal.source_view is view
    call.response.defer.assert_not_called()
    ui.manager.roster.assert_not_called()
    assert not view.is_finished()


async def test_selected_alt_removal_goes_directly_to_confirmation(
    character_controller, guild
):
    ui = character_controller
    main, alt = character(main=True), character(2)
    ui.manager.roster.return_value = roster(main, alt)
    view = dashboard(ui, guild, [alt])
    call = component(guild)
    await view.remove(call)
    confirmation = call.edit_original_response.call_args.kwargs["view"]
    assert isinstance(confirmation, RemovalConfirmation)
    assert confirmation.character.id == alt.id
    ui.manager.remove.assert_not_called()


@pytest.mark.parametrize("allow_clear", [False, True])
async def test_last_character_policy_is_visible_before_clicking_remove(
    character_controller, guild, allow_clear
):
    ui = character_controller
    ui.settings = replace(ui.settings, allow_clear_selection=allow_clear)
    ui.manager.roster.return_value = roster(character(main=True))
    call = interaction(guild, guild.member())
    await ui.open(call)
    reply = call.followup.send.call_args.kwargs
    assert button(reply["view"], "Remove").disabled is not allow_clear
    assert button(reply["view"], "Make Main").disabled
    detail = reply["embed"].fields[0].value
    assert ("requires a main" in detail) is not allow_clear
    assert "ALLOW_CLEAR_SELECTION" not in detail


async def test_paging_and_refresh_preserve_selection_without_changing_roles(
    character_controller, guild
):
    ui = character_controller
    entries = [character(index, main=index == 1) for index in range(1, 32)]
    ui.manager.roster.return_value = roster(*entries)
    call = component(guild)
    await ui.send_roster(call, roster(*entries), selected_id=27)
    view = call.response.send_message.call_args.kwargs["view"]
    assert view.page == 1 and view.selected.id == 27
    refresh = component(guild)
    await view.refresh(refresh)
    current = refresh.edit_original_response.call_args.kwargs["view"]
    assert current.page == 1 and current.selected.id == 27
    assert [
        option.value for option in current.character_select.options if option.default
    ] == ["27"]
    ui.manager.promote.assert_not_called()


async def test_done_deletes_only_own_private_menu_without_a_new_reply(
    character_controller, guild
):
    ui = character_controller
    view, message = await open_menu(ui, guild)
    call = component(guild)
    call.channel_id = 555
    await button(view, "Done").callback(call)
    call.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)
    call.delete_original_response.assert_awaited_once()
    call.followup.send.assert_not_called()
    message.delete.assert_not_called()
    assert view.is_finished()
    assert not ui._private_views and not ui._private_sessions
    ui.manager.remove.assert_not_called()


@pytest.mark.parametrize("owner,ephemeral", [(789, True), (456, False)])
async def test_done_cannot_delete_another_members_menu_or_public_panel(
    character_controller, guild, owner, ephemeral
):
    view, message = await open_menu(character_controller, guild)
    call = component(guild, owner=owner, ephemeral=ephemeral)
    await view.done(call)
    call.delete_original_response.assert_not_called()
    message.delete.assert_not_called()
    assert not view.is_finished()


async def test_idle_timeout_deletes_menu_and_releases_tracking(
    character_controller, guild
):
    ui = character_controller
    view, message = await open_menu(ui, guild)
    assert view.timeout == PRIVATE_TIMEOUT == 300
    await view.on_timeout()
    message.delete.assert_awaited_once()
    assert view.is_finished()
    assert not ui._private_views and not ui._private_sessions


async def test_reopening_removes_old_menu_but_its_timeout_cannot_delete_new_menu(
    character_controller, guild
):
    ui = character_controller
    old, first = await open_menu(ui, guild)
    current, second = await open_menu(ui, guild, message_id=701)
    first.delete.assert_awaited_once()
    assert old.is_finished()
    assert ui._private_views[701] is current
    assert 700 not in ui._private_views
    await old.on_timeout()
    first.delete.assert_awaited_once()
    second.delete.assert_not_called()
    assert not current.is_finished()


@pytest.mark.parametrize("owner,channel", [(789, 555), (456, 556)])
async def test_reopening_does_not_close_another_owner_or_channels_menu(
    character_controller, guild, owner, channel
):
    ui = character_controller
    old, first = await open_menu(ui, guild)
    current, second = await open_menu(
        ui, guild, message_id=701, owner=owner, channel=channel
    )
    first.delete.assert_not_called()
    second.delete.assert_not_called()
    assert not old.is_finished() and not current.is_finished()
    assert len(ui._private_sessions) == 2


async def test_modal_launch_renews_cleanup_token_without_creating_a_message(
    character_controller, guild
):
    ui = character_controller
    view, first = await open_menu(ui, guild)
    call = component(guild)
    call.channel_id = 555
    call.response.send_modal = AsyncMock()
    await view.add(call)
    await view.on_timeout()
    call.followup.delete_message.assert_awaited_once_with(700)
    first.delete.assert_not_called()
    call.followup.send.assert_not_called()


@pytest.mark.parametrize(
    "failure", [http_error(discord.NotFound), http_error(discord.HTTPException)]
)
async def test_cleanup_failure_does_not_break_the_new_menu_or_log_payloads(
    character_controller, guild, failure, caplog
):
    ui = character_controller
    old, message = await open_menu(ui, guild)
    message.delete.side_effect = failure
    current, _ = await open_menu(ui, guild, message_id=701)
    assert old.is_finished() and not current.is_finished()
    assert ui._private_views[701] is current
    assert str(failure) not in caplog.text


async def test_timeout_waits_for_navigation_and_cannot_remove_its_result(
    character_controller, guild
):
    ui = character_controller
    old, message = await open_menu(ui, guild)
    started, release = asyncio.Event(), asyncio.Event()

    async def read(*args, **kwargs):
        started.set()
        await release.wait()
        return roster()

    ui.manager.roster.side_effect = read
    call = component(guild)
    call.channel_id = 555
    navigate = asyncio.create_task(old.refresh(call))
    await asyncio.wait_for(started.wait(), 2)
    timeout = asyncio.create_task(old.on_timeout())
    release.set()
    await asyncio.wait_for(asyncio.gather(navigate, timeout), 2)
    current = call.edit_original_response.call_args.kwargs["view"]
    assert old.is_finished() and not current.is_finished()
    message.delete.assert_not_called()
    call.delete_original_response.assert_not_called()
    call.followup.delete_message.assert_not_called()
    await current.on_timeout()
    call.delete_original_response.assert_awaited_once()


async def test_submission_after_done_cannot_save_from_the_closed_modal(
    character_controller, guild
):
    ui = character_controller
    view, _ = await open_menu(ui, guild)
    modal = CharacterModal(ui, guild.id, 456, source_view=view)
    modal.primary._value, modal.secondary._value = "John", "Stormwind"
    modal.class_select._values = ["paladin"]
    done = component(guild)
    done.channel_id = 555
    await view.done(done)
    call = component(guild)
    call.type = discord.InteractionType.modal_submit
    call.channel_id = 555
    await modal.on_submit(call)
    ui.manager.add.assert_not_called()
    assert "closed" in call.followup.send.call_args.kwargs["content"]
    call.followup.send.return_value.delete.assert_awaited_once_with(delay=60)


async def test_initial_private_response_also_gets_done_and_idle_cleanup(
    character_controller, guild
):
    ui = character_controller
    call = interaction(guild, guild.member())
    call.response.send_message.return_value = SimpleNamespace(message_id=700)
    view = BackView(ui, guild.id, 456)
    await ui.send(call, content="Your characters", view=view)
    await view.on_timeout()
    call.delete_original_response.assert_awaited_once()


async def test_invalid_form_keeps_draft_and_retries_in_the_same_message(
    character_controller, guild
):
    ui = character_controller
    old = dashboard(ui, guild, [])
    ui.manager.add.side_effect = UserError("First Name must contain letters only.")
    modal = CharacterModal(ui, guild.id, 456, source_view=old)
    modal.primary._value, modal.secondary._value = "J0hn", "Stormwind"
    modal.class_select._values = ["paladin"]
    submit = component(guild)
    submit.type = discord.InteractionType.modal_submit
    await modal.on_submit(submit)
    submit.followup.send.assert_not_called()
    response = submit.edit_original_response.call_args.kwargs
    assert "First Name" in response["content"]
    retry = response["view"]
    assert isinstance(retry, RetryCharacterView)
    assert old.is_finished()
    click = component(guild)
    click.response.send_modal = AsyncMock()
    await retry.retry(click)
    reopened = click.response.send_modal.call_args.args[0]
    assert reopened.primary.default == "J0hn"
    assert reopened.secondary.default == "Stormwind"
    assert [
        option.value for option in reopened.class_select.options if option.default
    ] == ["paladin"]
    reopened.primary._value, reopened.secondary._value = "John", "Stormwind"
    reopened.class_select._values = ["paladin"]
    ui.manager.add.side_effect = None
    ui.manager.roster.return_value = roster(character(main=True))
    fixed = component(guild)
    fixed.type = discord.InteractionType.modal_submit
    await reopened.on_submit(fixed)
    ui.manager.add.assert_awaited_with(guild, 456, "John", "Stormwind", "paladin")
    assert fixed.edit_original_response.call_args.kwargs["view"].selected.id == 1
    assert retry.is_finished()


async def test_new_alt_is_selected_on_its_page_after_registration(
    character_controller, guild
):
    ui = character_controller
    entries = [
        character(index, primary=f"Player{index}", main=index == 1)
        for index in range(1, 17)
    ]
    added = character(17, primary="Sám", secondary="Ironforge", class_id="warrior")
    ui.manager.roster.return_value = roster(*entries, added)
    modal = CharacterModal(ui, guild.id, 456)
    modal.primary._value, modal.secondary._value = "Sa\u0301m", "Ironforge"
    modal.class_select._values = ["warrior"]
    call = interaction(guild, guild.member())
    await modal.on_submit(call)
    view = call.followup.send.call_args.kwargs["view"]
    assert view.page == 1 and view.selected.id == 17
    assert not button(view, "Make Main").disabled


async def test_concurrent_openings_leave_only_the_latest_menu_active(
    character_controller, guild
):
    ui = character_controller
    started, release = asyncio.Event(), asyncio.Event()

    async def read(*args, **kwargs):
        started.set()
        await release.wait()
        return roster()

    ui.manager.roster.side_effect = read
    first = asyncio.create_task(open_menu(ui, guild))
    await asyncio.wait_for(started.wait(), 2)
    second = asyncio.create_task(open_menu(ui, guild, message_id=701))
    release.set()
    (old, old_message), (current, new_message) = await asyncio.wait_for(
        asyncio.gather(first, second), 2
    )
    old_message.delete.assert_awaited_once()
    new_message.delete.assert_not_called()
    assert old.is_finished() and not current.is_finished()
    assert list(ui._private_views) == [701]


async def test_empty_dashboard_has_one_clear_registration_action(
    character_controller, guild
):
    view, _ = await open_menu(character_controller, guild)
    labels = [item.label for item in view.children]
    assert labels == ["Done", "Add Character", "Community Roster"]
    assert not button(view, "Add Character").disabled


@pytest.mark.parametrize("missing_main", [False, True])
async def test_registration_restrictions_are_explained_before_opening_a_form(
    character_controller, guild, missing_main
):
    ui = character_controller
    ui.settings = replace(ui.settings, max_characters_per_user=0 if missing_main else 1)
    ui.manager.roster.return_value = roster(character(main=not missing_main))
    call = interaction(guild, guild.member())
    await ui.open(call)
    reply = call.followup.send.call_args.kwargs
    assert button(reply["view"], "Add Character").disabled
    assert ("No main is designated" if missing_main else "limit of 1") in reply[
        "embed"
    ].description
    assert not button(reply["view"], "Edit").disabled


async def test_actual_discord_view_timer_runs_private_cleanup(
    character_controller, guild
):
    ui = character_controller
    view, message = await open_menu(ui, guild)
    deleted = asyncio.Event()

    async def delete():
        deleted.set()

    message.delete.side_effect = delete
    view.timeout = 0.01
    view_store = discord.ui.view.ViewStore(state=None)
    view_store.add_view(view, message_id=700)
    assert await asyncio.wait_for(view.wait(), 2)
    await asyncio.wait_for(deleted.wait(), 2)
    assert not ui._private_views and not ui._private_sessions


async def test_cancel_returns_to_the_same_character_and_page(
    character_controller, guild
):
    ui = character_controller
    entries = [character(index, main=index == 1) for index in range(1, 20)]
    ui.manager.roster.return_value = roster(*entries)
    confirmation = RemovalConfirmation(ui, guild.id, 456, entries[-1], None)
    ui.track_view(700, confirmation)
    call = component(guild)
    await confirmation.cancel(call)
    view = call.edit_original_response.call_args.kwargs["view"]
    assert view.selected.id == 19 and view.page == 1
    ui.manager.remove.assert_not_called()
