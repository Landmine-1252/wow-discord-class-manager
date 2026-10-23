import asyncio
import signal
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from wow_class_manager import __main__ as entrypoint
from wow_class_manager.bot import ClassBot
from wow_class_manager.config import ConfigError
from wow_class_manager.logging_setup import Redactor
from wow_class_manager.storage import Store


@pytest.mark.parametrize("failed_login", [False, True])
async def test_runner_handles_sigterm_and_login_failure(
    settings, monkeypatch, failed_login
):
    loop = asyncio.get_running_loop()
    handlers = {}
    removed = []
    monkeypatch.setattr(
        loop,
        "add_signal_handler",
        lambda sig, handler: handlers.__setitem__(sig, handler),
    )
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: removed.append(sig))
    monkeypatch.setattr(entrypoint, "configure_logging", lambda *args: Redactor())

    async def fake_start(self, token, reconnect):
        assert token == settings.token and reconnect
        if failed_login:
            raise discord.LoginFailure("private details must not be logged")
        loop.call_soon(handlers[signal.SIGTERM])
        await asyncio.Event().wait()

    monkeypatch.setattr(ClassBot, "start", fake_start)
    if failed_login:
        with pytest.raises(discord.LoginFailure):
            await asyncio.wait_for(entrypoint.run(settings), 2)
    else:
        await asyncio.wait_for(entrypoint.run(settings), 2)
    assert set(removed) == {signal.SIGINT, signal.SIGTERM}
    reopened = Store(settings.database_path)
    await reopened.open()
    await reopened.close()


def test_missing_config_exits_safely(monkeypatch, caplog):
    monkeypatch.setattr(entrypoint, "configure_logging", lambda *args: Redactor())
    monkeypatch.setattr(
        entrypoint.Settings,
        "from_env",
        MagicMock(side_effect=ConfigError("Configure exactly one token source.")),
    )
    with pytest.raises(SystemExit) as result:
        entrypoint.main()
    assert result.value.code == 2
    assert "configuration_invalid" in caplog.text


async def test_ready_and_join_schedule_each_guild_once(
    settings, store, guild, monkeypatch
):
    bot = ClassBot(settings, store, Redactor())
    monkeypatch.setattr(type(bot), "guilds", property(lambda self: [guild]))
    bot.roles.reconcile = AsyncMock(wraps=bot.roles.reconcile)
    bot.controller.panels.refresh = AsyncMock(return_value=[])
    await bot.on_ready()
    await bot.on_guild_join(guild)
    await asyncio.gather(*bot._workers)
    assert guild.id in bot._prepared
    bot.roles.reconcile.assert_awaited_once()
    await bot.on_ready()
    assert not bot._workers
    await bot.on_guild_remove(guild)
    assert guild.id not in bot._prepared


async def test_guild_failure_does_not_block_other_guild(
    settings, store, guild, monkeypatch
):
    from conftest import FakeGuild

    other = FakeGuild(999)
    bot = ClassBot(settings, store, Redactor())
    original_reconcile = bot.roles.reconcile

    async def reconcile(candidate):
        if candidate.id == guild.id:
            raise discord.Forbidden(
                MagicMock(status=403, reason="forbidden"), "no permission"
            )
        return await original_reconcile(candidate)

    bot.roles.reconcile = AsyncMock(side_effect=reconcile)
    bot.controller.panels.refresh = AsyncMock(return_value=[])
    bot._prepare(guild)
    bot._prepare(other)
    await asyncio.wait_for(bot._guild_workers[other.id], 1)
    assert other.id in bot._prepared
    assert not bot._guild_workers[guild.id].done()
    bot._guild_workers[guild.id].cancel()
    await asyncio.gather(*bot._workers, return_exceptions=True)


async def test_storage_cancellation_waits_for_worker(store):
    started, release = threading.Event(), threading.Event()

    def operation():
        started.set()
        assert release.wait(2)

    task = asyncio.create_task(store._run(operation))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await store.set_meta("after-cancellation", "working")
    assert await store.get_meta("after-cancellation") == "working"


async def test_startup_reports_no_guild_and_late_join_recovers(
    settings, store, guild, monkeypatch, caplog
):
    caplog.set_level("INFO")
    bot = ClassBot(settings, store, Redactor())
    installed = []
    monkeypatch.setattr(ClassBot, "guilds", property(lambda self: installed))
    monkeypatch.setattr(ClassBot, "is_ready", lambda self: True)
    monkeypatch.setattr(ClassBot, "latency", property(lambda self: 0.1))
    bot._commands_ready = True
    bot._connection.application_id = 987654
    await bot.on_ready()
    assert bot.readiness() == "waiting_for_guild"
    assert "startup_no_guilds application_id=987654" in caplog.text
    assert (
        "Guild Install" in caplog.text
        and "scope=bot%20applications.commands" in caplog.text
    )
    installed.append(guild)
    await bot.on_guild_join(guild)
    await asyncio.gather(*bot._workers)
    assert bot.readiness() == "connected"
    assert "guild_joined guild_id=123" in caplog.text
    assert "guild_startup_check" in caplog.text and "manageable_roles=9" in caplog.text
    assert "panel_not_configured" in caplog.text
    assert await store.panel(guild.id) is None
    assert not guild.fetch_member.await_count


async def test_startup_allowlist_reports_skipped_and_missing_guilds(
    settings, store, guild, monkeypatch, caplog
):
    caplog.set_level("INFO")
    bot = ClassBot(
        replace(settings, allowed_guild_ids=frozenset({999})), store, Redactor()
    )
    monkeypatch.setattr(ClassBot, "guilds", property(lambda self: [guild]))
    monkeypatch.setattr(ClassBot, "is_ready", lambda self: True)
    monkeypatch.setattr(ClassBot, "latency", property(lambda self: 0.1))
    bot._commands_ready = True
    await bot.on_ready()
    await bot.on_guild_available(guild)
    assert "startup_guild_missing guild_id=999" in caplog.text
    assert caplog.text.count("guild_skipped guild_id=123") == 1
    assert bot.readiness() == "waiting_for_guild"
    assert not bot._workers
    guild.create_role.assert_not_awaited()


async def test_readiness_degraded_for_expected_missing_guild(
    settings, store, guild, monkeypatch
):
    bot = ClassBot(
        replace(settings, allowed_guild_ids=frozenset({guild.id, 999})),
        store,
        Redactor(),
    )
    monkeypatch.setattr(ClassBot, "guilds", property(lambda self: [guild]))
    monkeypatch.setattr(ClassBot, "is_ready", lambda self: True)
    monkeypatch.setattr(ClassBot, "latency", property(lambda self: 0.1))
    bot._ever_ready = bot._commands_ready = True
    bot._prepared.add(guild.id)
    assert bot.readiness() == "degraded"


async def test_guild_diagnostics_permission_failure_and_recovery(
    settings, store, guild, monkeypatch, caplog
):
    bot = ClassBot(settings, store, Redactor())
    monkeypatch.setattr(ClassBot, "guilds", property(lambda self: [guild]))
    monkeypatch.setattr(ClassBot, "is_ready", lambda self: True)
    monkeypatch.setattr(ClassBot, "latency", property(lambda self: 0.1))
    bot._ever_ready = bot._commands_ready = True
    guild.me.guild_permissions.manage_roles = False
    assert not await bot.check_guild(guild)
    assert bot.readiness() == "degraded"
    assert "Manage Roles" in caplog.text
    guild.me.guild_permissions.manage_roles = True
    await bot.roles.reconcile(guild)
    assert await bot.check_guild(guild)
    assert bot.readiness() == "connected"
    for task in tuple(bot._workers):
        task.cancel()
    await asyncio.gather(*bot._workers, return_exceptions=True)


async def test_saved_panel_permissions_affect_startup_health(settings, store, guild):
    from wow_class_manager.storage import Panel

    bot = ClassBot(settings, store, Redactor())
    await bot.roles.reconcile(guild)
    channel = guild.channel()
    message = await channel.send(content="existing panel")
    await store.save_panel(Panel(guild.id, channel.id, message.id))
    channel.permissions_for.return_value.send_messages = False
    assert not await bot.check_guild(guild)
    assert any("Send Messages" in warning for warning in bot._guild_issues[guild.id])
    channel.permissions_for.return_value.send_messages = True
    assert await bot.check_guild(guild)
    assert channel.send.await_count == 1
    message.edit.assert_not_awaited()


async def test_missing_bot_membership_has_install_diagnostics_without_rest(
    settings, store, guild
):
    from wow_class_manager.diagnostics import status

    guild.me = None
    report = await status(settings, store, guild)
    assert "Guild Install" in report and "bot and applications.commands" in report
    guild.fetch_roles.assert_not_awaited()
    guild.fetch_member.assert_not_awaited()


async def test_diagnostic_storage_failure_is_cached_as_unhealthy(
    settings, store, guild, monkeypatch, caplog
):
    from wow_class_manager.storage import StorageError

    bot = ClassBot(settings, store, Redactor())
    bot._prepared.add(guild.id)
    monkeypatch.setattr(
        store, "roles", AsyncMock(side_effect=StorageError("private details"))
    )
    assert not await bot.check_guild(guild)
    assert guild.id not in bot._prepared
    assert bot._guild_issues[guild.id]
    assert "guild_check_failed" in caplog.text
    assert "private details" not in caplog.text


@pytest.mark.parametrize("problem", ["mismatched_id", "http_endpoint"])
async def test_authenticated_configuration_rejects_wrong_identity_and_endpoint(
    settings, store, monkeypatch, caplog, problem
):
    bot = ClassBot(replace(settings, application_id=123), store, Redactor())
    bot._application = SimpleNamespace(
        id=456 if problem == "mismatched_id" else 123,
        interactions_endpoint_url=(
            "https://secret.example/credential" if problem == "http_endpoint" else None
        ),
    )
    spawn = MagicMock()
    monkeypatch.setattr(bot, "_spawn", spawn)
    with pytest.raises(
        ConfigError,
        match=(
            "DISCORD_APPLICATION_ID"
            if problem == "mismatched_id"
            else "Interactions Endpoint URL"
        ),
    ):
        await bot.setup_hook()
    spawn.assert_not_called()
    assert "secret.example" not in caplog.text


async def test_authenticated_identity_is_logged_without_secrets(
    settings, store, monkeypatch, caplog
):
    caplog.set_level("INFO")
    bot = ClassBot(settings, store, Redactor())
    bot._application = SimpleNamespace(id=123, interactions_endpoint_url=None)
    bot._connection.user = SimpleNamespace(id=456)
    monkeypatch.setattr(bot, "_spawn", lambda coroutine, name: coroutine.close())
    await bot.setup_hook()
    assert (
        "discord_authenticated bot_id=456 application_id=123 configured_application_id=auto"
        in caplog.text
    )
    assert settings.token not in caplog.text


@pytest.mark.parametrize("problem", ["database", "heartbeat"])
async def test_startup_failure_releases_store_before_connecting(
    settings, monkeypatch, problem
):
    from wow_class_manager.storage import StorageError

    start = AsyncMock()
    monkeypatch.setattr(ClassBot, "start", start)
    monkeypatch.setattr(entrypoint, "configure_logging", lambda *args: Redactor())
    if problem == "database":
        monkeypatch.setattr(
            Store,
            "check_health",
            AsyncMock(side_effect=StorageError("SQLite probe failed")),
        )
        expected = StorageError
    else:
        monkeypatch.setattr(
            entrypoint, "write_health", MagicMock(side_effect=PermissionError)
        )
        expected = PermissionError
    with pytest.raises(expected):
        await entrypoint.run(settings)
    start.assert_not_called()
    reopened = Store(settings.database_path)
    await reopened.open()
    await reopened.close()


async def test_reconnect_rechecks_guilds_without_reregistering_commands(
    settings, store, guild, monkeypatch
):
    bot = ClassBot(settings, store, Redactor())
    monkeypatch.setattr(ClassBot, "guilds", property(lambda self: [guild]))
    bot.roles.reconcile = AsyncMock(wraps=bot.roles.reconcile)
    bot.sync_commands = AsyncMock()
    await bot.on_ready()
    await asyncio.gather(*bot._workers)
    await bot.on_disconnect()
    assert guild.id not in bot._prepared
    await bot.on_resumed()
    await asyncio.gather(*bot._workers)
    assert guild.id in bot._prepared
    assert bot.roles.reconcile.await_count == 2
    bot.sync_commands.assert_not_awaited()


async def test_startup_diagnostics_reports_role_hierarchy_and_missing_role(
    settings, store, guild
):
    bot = ClassBot(settings, store, Redactor())
    await bot.roles.reconcile(guild)
    guild.roles[1].position = guild.me.top_role.position + 1
    guild.roles.pop()
    assert not await bot.check_guild(guild)
    warnings = bot._guild_issues[guild.id]
    assert any("move the bot's highest role" in item for item in warnings)
    assert any("missing; run sync" in item for item in warnings)


async def test_unknown_guild_diagnostic_does_not_claim_deleted_panel(
    settings, store, guild
):
    from wow_class_manager.diagnostics import status

    guild.fetch_roles.side_effect = discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"),
        {"code": 10004, "message": "Unknown Guild"},
    )
    report = await status(settings, store, guild)
    assert "Guild Install" in report
    assert "deleted" not in report
    guild.fetch_channel.assert_not_awaited()


async def test_health_storage_failure_does_not_refresh_success_heartbeat(
    settings, store, guild, monkeypatch, caplog
):
    from wow_class_manager.health import write_health
    from wow_class_manager.storage import StorageError

    bot = ClassBot(settings, store, Redactor())
    monkeypatch.setattr(ClassBot, "guilds", property(lambda self: [guild]))
    monkeypatch.setattr(ClassBot, "is_ready", lambda self: True)
    monkeypatch.setattr(ClassBot, "latency", property(lambda self: 0.1))
    bot._ever_ready = bot._commands_ready = True
    bot._prepared.add(guild.id)
    write_health(settings.health_path, "connected")
    previous = settings.health_path.read_text()
    monkeypatch.setattr(
        store, "get_meta", AsyncMock(side_effect=StorageError("secret"))
    )

    async def stop_after_iteration(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr("wow_class_manager.bot.asyncio.sleep", stop_after_iteration)
    with pytest.raises(asyncio.CancelledError):
        await bot._heartbeat()
    assert settings.health_path.read_text() == previous
    assert "health_write_failed error_type=StorageError" in caplog.text
    assert "secret" not in caplog.text


async def test_failed_admin_recheck_retries_and_recovers(
    settings, store, guild, monkeypatch
):
    from conftest import http_error

    bot = ClassBot(settings, store, Redactor())
    monkeypatch.setattr(ClassBot, "guilds", property(lambda self: [guild]))
    monkeypatch.setattr(ClassBot, "is_ready", lambda self: True)
    monkeypatch.setattr(ClassBot, "latency", property(lambda self: 0.1))
    bot._ever_ready = bot._commands_ready = True
    await bot.roles.reconcile(guild)
    assert await bot.check_guild(guild)
    started, release = asyncio.Event(), asyncio.Event()

    async def wait_for_retry(seconds):
        assert seconds == 300
        started.set()
        await release.wait()

    monkeypatch.setattr("wow_class_manager.bot.asyncio.sleep", wait_for_retry)
    guild.fetch_roles.side_effect = http_error()
    assert not await bot.check_guild(guild)
    assert bot.readiness() == "degraded"
    await asyncio.wait_for(started.wait(), 1)
    worker = bot._guild_workers[guild.id]
    assert not await bot.check_guild(guild)
    assert bot._guild_workers[guild.id] is worker
    guild.fetch_roles.side_effect = lambda: list(guild.roles)
    release.set()
    await asyncio.wait_for(worker, 1)
    assert bot.readiness() == "connected"
    assert len(guild.roles) == 10


async def test_unavailable_guild_cannot_be_marked_healthy_by_inflight_check(
    settings, store, guild, monkeypatch
):
    from wow_class_manager import diagnostics

    bot = ClassBot(settings, store, Redactor())
    monkeypatch.setattr(ClassBot, "guilds", property(lambda self: [guild]))
    monkeypatch.setattr(ClassBot, "is_ready", lambda self: True)
    monkeypatch.setattr(ClassBot, "latency", property(lambda self: 0.1))
    bot._ever_ready = bot._commands_ready = True
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_check(*args):
        started.set()
        await release.wait()
        return diagnostics.GuildCheck(associated=9, manageable=9, manage_roles=True)

    monkeypatch.setattr(diagnostics, "inspect_guild", delayed_check)
    check = asyncio.create_task(bot.check_guild(guild))
    await asyncio.wait_for(started.wait(), 1)
    guild.unavailable = True
    await bot.on_guild_unavailable(guild)
    release.set()
    assert not await check
    assert guild.id not in bot._prepared
    assert bot.readiness() == "degraded"
    assert not bot._workers


async def test_removed_guild_ignores_inflight_check_and_rejoin_prepares_again(
    settings, store, guild, monkeypatch
):
    from wow_class_manager import diagnostics

    bot = ClassBot(settings, store, Redactor())
    installed = [guild]
    monkeypatch.setattr(ClassBot, "guilds", property(lambda self: installed))
    bot._ever_ready = True
    started, release = asyncio.Event(), asyncio.Event()
    inspect = diagnostics.inspect_guild

    async def delayed_check(*args):
        started.set()
        await release.wait()
        return diagnostics.GuildCheck(associated=9, manageable=9, manage_roles=True)

    monkeypatch.setattr(diagnostics, "inspect_guild", delayed_check)
    check = asyncio.create_task(bot.check_guild(guild))
    await asyncio.wait_for(started.wait(), 1)
    installed.clear()
    await bot.on_guild_remove(guild)
    release.set()
    assert not await check
    assert guild.id not in bot._prepared
    assert guild.id not in bot._guild_issues
    monkeypatch.setattr(diagnostics, "inspect_guild", inspect)
    installed.append(guild)
    await bot.on_guild_join(guild)
    await asyncio.gather(*bot._workers)
    assert guild.id in bot._prepared
    assert guild.create_role.await_count == 9


async def test_disconnect_invalidates_inflight_check_and_resume_rechecks(
    settings, store, guild, monkeypatch
):
    from wow_class_manager import diagnostics

    bot = ClassBot(settings, store, Redactor())
    monkeypatch.setattr(ClassBot, "guilds", property(lambda self: [guild]))
    bot._ever_ready = True
    started, release = asyncio.Event(), asyncio.Event()
    inspect = diagnostics.inspect_guild

    async def delayed_check(*args):
        started.set()
        await release.wait()
        return diagnostics.GuildCheck(associated=9, manageable=9, manage_roles=True)

    monkeypatch.setattr(diagnostics, "inspect_guild", delayed_check)
    check = asyncio.create_task(bot.check_guild(guild))
    await asyncio.wait_for(started.wait(), 1)
    await bot.on_disconnect()
    release.set()
    assert not await check
    assert guild.id not in bot._prepared
    monkeypatch.setattr(diagnostics, "inspect_guild", inspect)
    await bot.on_resumed()
    await asyncio.gather(*bot._workers)
    assert guild.id in bot._prepared


async def test_unavailable_guild_is_not_prepared_until_available(
    settings, store, guild
):
    bot = ClassBot(settings, store, Redactor())
    guild.unavailable = True
    bot._prepare(guild)
    assert not bot._workers
    guild.fetch_roles.assert_not_awaited()
    guild.unavailable = False
    await bot.on_guild_available(guild)
    await asyncio.gather(*bot._workers)
    assert guild.id in bot._prepared
