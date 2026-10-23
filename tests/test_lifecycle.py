import asyncio
import signal
import threading
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
    bot.roles.reconcile = AsyncMock()
    bot.roles.reconcile.return_value.warnings = []
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

    from wow_class_manager.roles import ReconcileResult

    other = FakeGuild(999)
    bot = ClassBot(settings, store, Redactor())

    async def reconcile(candidate):
        if candidate.id == guild.id:
            raise discord.Forbidden(
                MagicMock(status=403, reason="forbidden"), "no permission"
            )
        return ReconcileResult(reused=9)

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
