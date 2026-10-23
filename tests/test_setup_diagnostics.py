"""Reproduce incomplete installations and distinguish Discord's 404 failures."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import interaction
from test_interface import controller as controller

from wow_class_manager.roles import api_problem


def missing_resource(code: int, *, message: str = "simulated error"):
    return discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"),
        {"code": code, "message": message},
    )


@pytest.mark.parametrize(
    "code,advice",
    [
        (10002, "DISCORD_APPLICATION_ID"),
        (10003, "selected channel"),
        (10004, "Guild Install"),
        (10007, "server member"),
        (10008, "that message"),
        (10011, "class role"),
        (10015, "response webhook"),
        (10062, "acknowledged in time"),
        (10063, "application command"),
    ],
)
def test_discord_resource_errors_are_specific_and_secret_free(code, advice):
    message = api_problem(missing_resource(code, message="private-request-secret"))
    assert advice in message
    assert str(code) in message
    assert "private-request-secret" not in message


def test_unknown_404_does_not_claim_a_resource_was_deleted():
    message = api_problem(missing_resource(0))
    assert "deleted" not in message
    assert "bot logs" in message


@pytest.mark.parametrize("action", ["setup", "sync", "set", "clear", "me"])
async def test_partial_guild_cannot_start_role_or_panel_operations(
    controller, guild, store, action
):
    member, channel = guild.member(), guild.channel()
    guild.me = None
    call = interaction(guild, member, channel, admin=True)
    await controller.execute(call, action, class_id="mage")
    reply = call.followup.send.call_args.kwargs
    assert reply["ephemeral"]
    assert "Guild Install" in reply["content"]
    assert "applications.commands" in reply["content"]
    guild.fetch_roles.assert_not_called()
    guild.fetch_channel.assert_not_called()
    guild.fetch_member.assert_not_called()
    guild.create_role.assert_not_called()
    channel.send.assert_not_called()
    assert await store.panel(guild.id) is None


async def test_class_help_remains_available_for_incomplete_install(controller, guild):
    member = guild.member()
    guild.me = None
    call = interaction(guild, member)
    await controller.execute(call, "help")
    assert "/class setup" in call.followup.send.call_args.kwargs["content"]
    guild.fetch_roles.assert_not_called()


async def test_setup_succeeds_before_any_character_is_registered(
    controller, guild, store
):
    member, channel = guild.member(), guild.channel()
    call = interaction(guild, member, channel, admin=True)
    await controller.execute(call, "setup")
    assert "panel ready" in call.followup.send.call_args.kwargs["content"]
    assert await store.panel(guild.id) is not None
    assert len(await store.roles(guild.id)) == 9
    roster = await controller.bot.characters.roster(guild, member.id, reconcile=False)
    assert not roster.characters and roster.main is None


@pytest.mark.parametrize(
    "code,advice", [(10003, "selected channel"), (10004, "Guild Install")]
)
async def test_setup_logs_http_code_and_operation_without_raw_error(
    controller, guild, store, caplog, code, advice
):
    channel = guild.channel()
    guild.fetch_channel.side_effect = missing_resource(
        code,
        message="private-request-secret https://discord.com/api/webhooks/123/secret",
    )
    call = interaction(guild, guild.member(), channel, admin=True)
    await controller.execute(call, "setup")
    reply = call.followup.send.call_args.kwargs["content"]
    assert advice in reply
    assert "action=setup" in caplog.text
    assert "operation=fetch_setup_channel" in caplog.text
    assert "http_status=404" in caplog.text
    assert f"discord_code={code}" in caplog.text
    assert "private-request-secret" not in caplog.text + reply
    assert "api/webhooks" not in caplog.text + reply
    guild.create_role.assert_not_called()
    channel.send.assert_not_called()
    assert await store.panel(guild.id) is None


@pytest.mark.parametrize("code", [0, 10003, 10004])
async def test_unconfirmed_message_deletion_does_not_recreate_panel(
    controller, guild, store, code
):
    channel = guild.channel()
    saved, _ = await controller.panels.setup(guild, channel.id)
    channel.fetch_message.side_effect = missing_resource(code)
    with pytest.raises(discord.NotFound):
        await controller.panels.setup(guild, channel.id)
    assert await store.panel(guild.id) == saved
    assert channel.send.await_count == 1


async def test_panel_deleted_between_fetch_and_edit_is_recreated(
    controller, guild, store
):
    channel = guild.channel()
    saved, _ = await controller.panels.setup(guild, channel.id)
    channel.messages[saved.message_id].edit.side_effect = missing_resource(10008)
    replacement, _ = await controller.panels.setup(guild, channel.id)
    assert replacement == await store.panel(guild.id)
    assert replacement.message_id != saved.message_id
    assert channel.send.await_count == 2


async def test_post_setup_diagnostic_failure_does_not_report_setup_as_failed(
    controller, guild, caplog
):
    controller.bot.check_guild = AsyncMock(side_effect=missing_resource(10004))
    call = interaction(guild, guild.member(), guild.channel(), admin=True)
    await controller.execute(call, "setup")
    assert call.followup.send.await_count == 1
    assert "panel ready" in call.followup.send.call_args.kwargs["content"]
    controller.bot.check_guild.assert_awaited_once_with(guild)
    assert "guild_diagnostics_failed" in caplog.text
    assert "interaction_failed" not in caplog.text


async def test_expired_interaction_response_logs_both_safe_error_codes(
    controller, guild, caplog
):
    call = interaction(guild, guild.member())
    call.response.is_done.return_value = True
    call.followup.send.side_effect = missing_resource(10015, message="private-secret")
    await controller.handle_error(
        call,
        missing_resource(10062, message="private-secret"),
        action="setup",
        operation="acknowledge_interaction",
    )
    assert "discord_code=10062" in caplog.text
    assert "discord_code=10015" in caplog.text
    assert "interaction_response_unavailable" in caplog.text
    assert "private-secret" not in caplog.text
