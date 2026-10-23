"""Exercise real discord.py interaction payloads instead of complete guild mocks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from wow_class_manager.bot import ClassBot
from wow_class_manager.logging_setup import Redactor
from wow_class_manager.roles import ReconcileResult
from wow_class_manager.storage import Panel


def user_payload(user_id: int, name: str) -> dict:
    return {
        "id": str(user_id),
        "username": name,
        "discriminator": "0000",
        "avatar": None,
    }


def channel_payload(channel_id: int) -> dict:
    return {
        "id": str(channel_id),
        "type": 0,
        "name": "character-setup",
        "position": 0,
    }


def setup_interaction(bot: ClassBot, *, can_send: bool = True) -> discord.Interaction:
    permissions = discord.Permissions(
        manage_guild=True, manage_roles=True, view_channel=True, send_messages=can_send
    )
    call = discord.Interaction(
        state=bot._connection,
        data={
            "id": "123456789012345678",
            "type": 2,
            "token": "synthetic-interaction-never-sent",
            "version": 1,
            "application_id": "900",
            "data": {
                "id": "10000",
                "name": "class",
                "type": 1,
                "options": [{"name": "setup", "type": 1, "options": []}],
            },
            "guild_id": "123",
            "attachment_size_limit": 10000000,
            "locale": "en-US",
            "channel": channel_payload(777),
            "member": {
                "user": user_payload(456, "Officer"),
                "roles": [],
                "flags": 0,
                "permissions": str(permissions.value),
            },
        },
    )
    response = SimpleNamespace(done=False, send_message=AsyncMock())

    async def defer(**kwargs):
        response.done = True

    response.is_done = lambda: response.done
    response.defer = AsyncMock(side_effect=defer)
    call._cs_response = response
    call._cs_followup = SimpleNamespace(send=AsyncMock())
    return call


def cache_guild(bot: ClassBot, *, can_send: bool = False) -> discord.Guild:
    permissions = discord.Permissions(view_channel=True, send_messages=can_send)
    guild = discord.Guild(
        state=bot._connection,
        data={
            "id": "123",
            "name": "Registered guild",
            "owner_id": "789",
            "roles": [
                {
                    "id": "123",
                    "name": "@everyone",
                    "permissions": str(permissions.value),
                    "position": 0,
                }
            ],
        },
    )
    guild._add_member(
        discord.Member._from_client_user(
            user=bot.user, guild=guild, state=bot._connection
        )
    )
    bot._connection._add_guild(guild)
    guild._add_channel(
        discord.TextChannel(
            state=bot._connection, guild=guild, data=channel_payload(777)
        )
    )
    return guild


@pytest.fixture
async def permission_bot(settings, store):
    bot = ClassBot(settings, store, Redactor())
    bot._connection.user = discord.ClientUser(
        state=bot._connection, data=user_payload(900, "Bot")
    )
    bot.roles.reconcile = AsyncMock(return_value=ReconcileResult())
    bot.controller.panels._channel = AsyncMock()
    bot.controller.panels.setup = AsyncMock(return_value=(Panel(123, 777, 888), []))
    bot.notifier.notify = MagicMock()
    bot.check_guild = AsyncMock(return_value=True)
    yield bot
    await bot.close()


async def test_fallback_interaction_fabricates_bot_member_but_cannot_run_setup(
    permission_bot,
    caplog,
):
    caplog.set_level("INFO")
    call = setup_interaction(permission_bot)
    assert not permission_bot.guilds
    assert call.guild.me is not None
    assert call.guild.default_role is None
    assert call.permissions.view_channel and call.permissions.send_messages
    assert not call.channel.permissions_for(call.user).view_channel
    assert not call.channel.permissions_for(call.user).send_messages

    await permission_bot.controller.execute(call, "setup")

    content = call.followup.send.call_args.kwargs["content"]
    assert "Guild Install" in content
    assert "You need View Channel" not in content
    assert "interaction_rejected guild_id=123 action=setup" in caplog.text
    assert "reason=bot_not_installed" in caplog.text
    assert call.token not in caplog.text
    permission_bot.roles.reconcile.assert_not_awaited()
    permission_bot.controller.panels._channel.assert_not_awaited()
    permission_bot.controller.panels.setup.assert_not_awaited()


@pytest.mark.parametrize("explicit_channel", [False, True])
async def test_same_channel_uses_discord_resolved_permissions(
    permission_bot, explicit_channel
):
    cache_guild(permission_bot)
    call = setup_interaction(permission_bot)
    assert not call.channel.permissions_for(call.user).send_messages
    assert call.permissions.send_messages
    permission_bot.controller.panels._channel.return_value = call.channel

    await permission_bot.controller.execute(
        call, "setup", channel=call.channel if explicit_channel else None
    )

    assert "panel ready" in call.followup.send.call_args.kwargs["content"]
    permission_bot.roles.reconcile.assert_awaited_once()
    permission_bot.controller.panels.setup.assert_awaited_once()


async def test_same_channel_resolved_denial_overrides_stale_cached_grant(
    permission_bot,
    caplog,
):
    caplog.set_level("INFO")
    cache_guild(permission_bot, can_send=True)
    call = setup_interaction(permission_bot, can_send=False)
    assert call.channel.permissions_for(call.user).send_messages
    permission_bot.controller.panels._channel.return_value = call.channel

    await permission_bot.controller.execute(call, "setup")

    assert "Send Messages" in call.followup.send.call_args.kwargs["content"]
    assert "reason=invoker_channel_permissions" in caplog.text
    assert "operation=validate_invoker_channel_permissions" in caplog.text
    permission_bot.roles.reconcile.assert_not_awaited()
    permission_bot.controller.panels.setup.assert_not_awaited()


async def test_removed_guild_with_stale_roles_is_not_proof_of_membership(
    permission_bot,
):
    guild = cache_guild(permission_bot, can_send=True)
    call = setup_interaction(permission_bot)
    permission_bot._connection._remove_guild(guild)
    assert call.guild.me is not None and call.guild.default_role is not None
    await permission_bot.controller.execute(call, "setup")
    assert "Guild Install" in call.followup.send.call_args.kwargs["content"]
    permission_bot.roles.reconcile.assert_not_awaited()


async def test_unavailable_guild_asks_to_retry_without_setup(permission_bot):
    guild = cache_guild(permission_bot, can_send=True)
    guild.unavailable = True
    call = setup_interaction(permission_bot)
    await permission_bot.controller.execute(call, "setup")
    assert "temporarily unavailable" in call.followup.send.call_args.kwargs["content"]
    permission_bot.roles.reconcile.assert_not_awaited()


@pytest.mark.parametrize("fresh_can_send", [False, True])
async def test_different_target_fetches_current_channel_permissions(
    permission_bot, monkeypatch, fresh_can_send
):
    guild = cache_guild(permission_bot, can_send=True)
    call = setup_interaction(permission_bot)
    target = discord.TextChannel(
        state=permission_bot._connection,
        guild=guild,
        data={
            **channel_payload(778),
            "permission_overwrites": [
                {
                    "id": "456",
                    "type": 1,
                    "allow": "0" if fresh_can_send else "2048",
                    "deny": "2048" if fresh_can_send else "0",
                }
            ],
        },
    )
    fresh = discord.TextChannel(
        state=permission_bot._connection,
        guild=guild,
        data={
            **channel_payload(778),
            "permission_overwrites": [
                {
                    "id": "456",
                    "type": 1,
                    "allow": "2048" if fresh_can_send else "0",
                    "deny": "0" if fresh_can_send else "2048",
                }
            ],
        },
    )
    fetch = AsyncMock(return_value=fresh)
    monkeypatch.setattr(discord.Guild, "fetch_channel", fetch)
    permission_bot.controller.panels._channel.return_value = fresh
    permission_bot.controller.panels.setup.return_value = (Panel(123, 778, 889), [])
    assert target.permissions_for(call.user).send_messages is not fresh_can_send
    assert fresh.permissions_for(call.user).send_messages is fresh_can_send

    await permission_bot.controller.execute(call, "setup", channel=target)

    fetch.assert_awaited_once_with(778)
    content = call.followup.send.call_args.kwargs["content"]
    if fresh_can_send:
        assert "panel ready" in content
        permission_bot.controller.panels.setup.assert_awaited_once_with(guild, 778)
    else:
        assert "Send Messages" in content
        permission_bot.roles.reconcile.assert_not_awaited()
        permission_bot.controller.panels.setup.assert_not_awaited()


@pytest.mark.parametrize("installed", [False, True])
async def test_uncached_channel_transformer_has_actionable_error(
    permission_bot, installed
):
    if installed:
        cache_guild(permission_bot, can_send=True)
    call = setup_interaction(permission_bot)
    selected = app_commands.AppCommandChannel(
        state=permission_bot._connection,
        guild_id=123,
        data={**channel_payload(778), "permissions": str(call.permissions.value)},
    )
    assert selected.resolve() is None
    command = permission_bot.tree.get_command("class").get_command("setup")
    with pytest.raises(app_commands.TransformerError) as caught:
        await command._params["channel"].transform(call, selected)

    await permission_bot.controller.handle_error(call, caught.value)

    reply = call.response.send_message.call_args.kwargs
    assert reply["ephemeral"]
    if installed:
        assert "channel" in reply["content"].lower()
        assert "could not be completed" not in reply["content"]
    else:
        assert "Guild Install" in reply["content"]
    permission_bot.roles.reconcile.assert_not_awaited()
    permission_bot.controller.panels.setup.assert_not_awaited()
