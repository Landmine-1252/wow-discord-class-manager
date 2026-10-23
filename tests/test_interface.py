import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from conftest import FakeGuild, interaction

from wow_class_manager.bot import ClassBot
from wow_class_manager.characters import CharacterManager
from wow_class_manager.commands import Controller, authorize, build_commands
from wow_class_manager.logging_setup import Redactor
from wow_class_manager.members import MemberManager
from wow_class_manager.roles import RoleManager, UserError
from wow_class_manager.ui import SELECT_ID, ClassView


@pytest.fixture
def controller(settings, store, locks):
    bot = SimpleNamespace(
        settings=settings,
        store=store,
        locks=locks,
        user=SimpleNamespace(id=900),
        redact=Redactor(),
        notifier=SimpleNamespace(notify=MagicMock()),
        roles=RoleManager(settings, store, locks),
        members=MemberManager(settings, store, locks),
    )
    bot.characters = CharacterManager(settings, store, locks, bot.members)
    return Controller(bot)


async def test_persistent_view(controller):
    view = ClassView(controller)
    assert view.is_persistent()
    select = view.children[0]
    assert select.custom_id == SELECT_ID
    assert select.min_values == select.max_values == 1
    assert len(select.options) == 9
    assert not any(option.default for option in select.options)


async def test_registration_on_startup(settings, store, monkeypatch):
    bot = ClassBot(settings, store, Redactor())

    # Keep lifecycle task scheduling, without running background loops here.
    def close_coroutine(coroutine, name):
        coroutine.close()

    monkeypatch.setattr(bot, "_spawn", close_coroutine)
    await bot.setup_hook()
    assert len(bot.persistent_views) == 2
    assert all(view.is_persistent() for view in bot.persistent_views)
    assert bot.intents.guilds
    assert (
        not bot.intents.members
        and not bot.intents.presences
        and not bot.intents.message_content
    )


async def test_setup_reuses_message_and_recovers_deletion(controller, guild, store):
    channel = guild.channel()
    first, _ = await controller.panels.setup(guild, channel.id)
    second, _ = await controller.panels.setup(guild, channel.id)
    assert first == second and channel.send.await_count == 1
    assert channel.messages[first.message_id].edit.await_count == 1
    channel.messages.pop(first.message_id)
    third, _ = await controller.panels.setup(guild, channel.id)
    assert third == await store.panel(guild.id)
    assert channel.send.await_count == 2


async def test_move_only_disables_old_bot_panel(controller, guild, store):
    first_channel, second_channel = guild.channel(111), guild.channel(222)
    first, _ = await controller.panels.setup(guild, first_channel.id)
    unrelated = await first_channel.send(content="Unrelated message")
    second, warnings = await controller.panels.setup(guild, second_channel.id)
    assert not warnings and first.message_id != second.message_id
    first_channel.messages[first.message_id].edit.assert_awaited_once_with(view=None)
    unrelated.edit.assert_not_called()
    assert second == await store.panel(guild.id)


async def test_multiple_guilds(controller, guild, store):
    other = FakeGuild(999)
    first, _ = await controller.panels.setup(guild, guild.channel().id)
    second, _ = await controller.panels.setup(other, other.channel(888).id)
    assert await store.panel(guild.id) == first
    assert await store.panel(other.id) == second


async def test_wrong_author_never_edited(controller, guild):
    channel = guild.channel()
    panel, _ = await controller.panels.setup(guild, channel.id)
    message = channel.messages[panel.message_id]
    message.author.id = 11111
    with pytest.raises(UserError, match="not owned"):
        await controller.panels.setup(guild, channel.id)
    message.edit.assert_not_called()


async def test_missing_channel_permissions(controller, guild):
    channel = guild.channel()
    channel.permissions_for.return_value.embed_links = False
    with pytest.raises(UserError, match="Embed Links"):
        await controller.panels.setup(guild, channel.id)
    channel.send.assert_not_called()


@pytest.mark.parametrize("action", ["setup", "sync", "status"])
async def test_unauthorized_administration(controller, guild, action):
    call = interaction(guild, guild.member(), guild.channel())
    await controller.execute(call, action)
    assert "Manage Server" in call.followup.send.call_args.kwargs["content"]
    assert call.followup.send.call_args.kwargs["ephemeral"] is True
    guild.create_role.assert_not_called()


def test_manage_roles_required(guild):
    call = interaction(guild, guild.member(), admin=True)
    call.permissions.manage_roles = False
    with pytest.raises(UserError, match="Manage Roles"):
        authorize(call, roles=True)
    authorize(call)


async def test_member_confirmation_is_ephemeral_and_panel_unchanged(controller, guild):
    await controller.bot.roles.reconcile(guild)
    channel = guild.channel()
    panel, _ = await controller.panels.setup(guild, channel.id)
    call = interaction(guild, guild.member(), channel)
    call.message = channel.messages[panel.message_id]
    await controller.execute(call, "set", class_id="mage", panel=True)
    call.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert (
        call.followup.send.call_args.kwargs["content"]
        == "Your class has been updated to Mage."
    )
    assert call.followup.send.call_args.kwargs["ephemeral"] is True
    assert call.followup.send.call_args.kwargs["allowed_mentions"].everyone is False
    call.message.edit.assert_not_called()


async def test_old_panel_cannot_change_roles(controller, guild):
    call = interaction(guild, guild.member(), guild.channel())
    call.message = SimpleNamespace(id=9999, author=SimpleNamespace(id=900))
    await controller.execute(call, "set", class_id="mage", panel=True)
    assert "no longer active" in call.followup.send.call_args.kwargs["content"]
    call.user.add_roles.assert_not_called()


def test_all_commands_registered(controller):
    group = build_commands(controller)
    assert group.name == "class"
    assert {command.name for command in group.commands} == {
        "setup",
        "sync",
        "status",
        "set",
        "me",
        "clear",
        "help",
    }
    assert len(group.get_command("set").parameters[0].choices) == 9


async def test_registration_is_not_repeated(settings, store):
    bot = ClassBot(settings, store, Redactor())
    bot.tree.fetch_commands = AsyncMock(
        return_value=[
            SimpleNamespace(name=name) for name in ("class", "character", "roster")
        ]
    )
    bot.tree.sync = AsyncMock()
    await bot.sync_commands()
    await bot.sync_commands()
    assert bot.tree.sync.await_count == 1


async def test_refresh_does_not_undo_concurrent_move(controller, guild, store):
    old_channel, new_channel = guild.channel(111), guild.channel(222)
    await controller.panels.setup(guild, old_channel.id)
    entered, release = asyncio.Event(), asyncio.Event()
    original = new_channel.send.side_effect

    async def pause_send(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    new_channel.send.side_effect = pause_send
    moving = asyncio.create_task(controller.panels.setup(guild, new_channel.id))
    await asyncio.wait_for(entered.wait(), 1)
    refreshing = asyncio.create_task(controller.panels.refresh(guild))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(moving, refreshing)
    assert (await store.panel(guild.id)).channel_id == new_channel.id
    assert old_channel.send.await_count == new_channel.send.await_count == 1


async def test_admin_setup_status_sync_and_help(controller, guild):
    channel = guild.channel()
    member = guild.member()
    setup = interaction(guild, member, channel, admin=True)
    await controller.execute(setup, "setup")
    assert "9 created" in setup.followup.send.call_args.kwargs["content"]
    status = interaction(guild, member, channel, admin=True)
    await controller.execute(status, "status")
    text = status.followup.send.call_args.kwargs["content"]
    assert "associated roles present: 9" in text
    assert "exists and owned by this bot" in text
    sync = interaction(guild, member, channel, admin=True)
    await controller.execute(sync, "sync")
    assert "0 created, 9 reused" in sync.followup.send.call_args.kwargs["content"]
    help_call = interaction(guild, member, channel)
    await controller.execute(help_call, "help")
    assert "/class set" in help_call.followup.send.call_args.kwargs["content"]


async def test_status_missing_deleted_unmanageable(controller, guild):
    from conftest import FakeRole

    from wow_class_manager.diagnostics import status

    initial = await status(controller.settings, controller.store, guild)
    assert "not configured" in initial and "missing; run sync" in initial
    await controller.bot.roles.reconcile(guild)
    channel = guild.channel()
    saved, _ = await controller.panels.setup(guild, channel.id)
    channel.messages.pop(saved.message_id)
    guild.roles[1].position = 101
    guild.roles.append(FakeRole(9999, "Officer", 200, discord.Colour(123)))
    report = await status(controller.settings, controller.store, guild)
    assert (
        "cannot find that message" in report
        and "move the bot" in report
        and "colored roles: 1" in report
    )


async def test_select_uses_each_interactions_value(controller, guild):
    controller.execute = AsyncMock()
    select = ClassView(controller).children[0]
    first, second = interaction(guild, guild.member(1)), interaction(
        guild, guild.member(2)
    )
    first.data, second.data = {"values": ["mage"]}, {"values": ["paladin"]}
    await asyncio.gather(select.callback(first), select.callback(second))
    assert [call.kwargs["class_id"] for call in controller.execute.call_args_list] == [
        "mage",
        "paladin",
    ]
