import asyncio
import io
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import yaml

from wow_class_manager import __version__
from wow_class_manager.bot import ClassBot
from wow_class_manager.health import check_health, write_health
from wow_class_manager.logging_setup import JsonFormatter, Redactor, configure_logging
from wow_class_manager.notifications import Notifier
from wow_class_manager.storage import Panel, StorageError, Store


async def test_sqlite_persists_and_locks(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    await store.open()
    await store.associate(123, "mage", 567)
    await store.save_panel(Panel(123, 456, 789))
    await store.set_meta("version-test", "present")
    competing = Store(path)
    with pytest.raises(StorageError, match="one bot"):
        await competing.open()
    await store.close()
    restored = Store(path)
    await restored.open()
    assert await restored.roles(123) == {"mage": 567}
    assert await restored.panel(123) == Panel(123, 456, 789)
    assert await restored.get_meta("version-test") == "present"
    await restored.close()


async def test_schema_migrates_v1(tmp_path):
    path = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE guilds (guild_id INTEGER PRIMARY KEY); CREATE TABLE roles (guild_id INTEGER, class_id TEXT, role_id INTEGER, PRIMARY KEY(guild_id,class_id)); CREATE TABLE panels (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, message_id INTEGER); INSERT INTO guilds VALUES (123); INSERT INTO roles VALUES (123,'mage',456); PRAGMA user_version=1;"
        )
    store = Store(path)
    await store.open()
    assert await store.roles(123) == {"mage": 456}
    await store.set_meta("test", "migrated")
    assert await store.get_meta("test") == "migrated"
    await store.close()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


@pytest.mark.parametrize("problem", ["newer", "corrupt", "directory"])
async def test_database_errors_do_not_reset_data(tmp_path, problem):
    path = tmp_path / "broken.sqlite3"
    if problem == "newer":
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA user_version=999")
    elif problem == "corrupt":
        path.write_bytes(b"not a database")
    else:
        path.mkdir()
    with pytest.raises(StorageError):
        await Store(path).open()
    assert path.exists()
    if problem == "corrupt":
        assert path.read_bytes() == b"not a database"


async def test_retired_panel_transaction(store):
    first, second = Panel(1, 10, 100), Panel(1, 20, 200)
    await store.save_panel(first)
    await store.save_panel(second)
    assert await store.panel(1) == second
    assert await store.retired_panels(1) == [first]
    await store.forget_retired(100)
    assert not await store.retired_panels(1)


@pytest.mark.parametrize(
    "state,code,message",
    [
        ("starting", 1, "starting"),
        ("connected", 0, "connected"),
        ("disconnected", 1, "disconnected"),
        ("stopped", 1, "disconnected"),
    ],
)
def test_health_states(tmp_path, state, code, message):
    path = tmp_path / "health.json"
    write_health(path, state)
    assert check_health(path) == (code, message)


def test_health_stale_missing_dead_invalid(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    assert check_health(path)[0] == 1
    write_health(path, "connected")
    data = json.loads(path.read_text())
    data["heartbeat"] = time.time() - 100
    path.write_text(json.dumps(data))
    assert "stale" in check_health(path)[1]
    write_health(path, "connected")

    def dead(*args):
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", dead)
    assert check_health(path)[0] == 1
    path.write_text("invalid json")
    assert check_health(path)[0] == 1


@pytest.mark.parametrize(
    "ready,commands_ready,latency,expected",
    [
        (True, True, 0.1, "connected"),
        (True, False, 0.1, "starting"),
        (False, True, 0.1, "disconnected"),
        (True, True, float("inf"), "disconnected"),
        (True, True, 100, "disconnected"),
    ],
)
async def test_gateway_readiness_updates_health(
    settings, store, monkeypatch, ready, commands_ready, latency, expected
):
    bot = ClassBot(settings, store, Redactor())
    bot._ever_ready = True
    bot._commands_ready = commands_ready
    monkeypatch.setattr(ClassBot, "is_ready", lambda self: ready)
    monkeypatch.setattr(ClassBot, "latency", property(lambda self: latency))

    async def finish_iteration(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr("wow_class_manager.bot.asyncio.sleep", finish_iteration)
    with pytest.raises(asyncio.CancelledError):
        await bot._heartbeat()
    assert check_health(settings.health_path)[1] == expected


def test_log_redaction_in_exception():
    secret = "private-token-value"
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonFormatter(Redactor((secret,))))
    logger = logging.getLogger("test_redaction")
    logger.handlers = [handler]
    logger.propagate = False
    try:
        raise ValueError(secret)
    except ValueError:
        logger.exception("failed: %s", secret)
    value = output.getvalue()
    assert secret not in value
    assert "[REDACTED]" in json.loads(value)["exception"]


def test_logging_configuration_hides_payloads_and_irrelevant_voice_warnings(
    monkeypatch,
):
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", root.level)
    for name in ("discord", "aiohttp", "asyncio"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.setattr(logging, "StreamHandler", lambda: handler)
    configure_logging("DEBUG", "private-test-secret")
    logging.getLogger("discord.http").debug("private gateway payload")
    logging.getLogger("discord.client").warning(
        "PyNaCl is not installed, voice will NOT be supported"
    )
    logging.getLogger("discord.client").warning("Connection interrupted")
    logging.getLogger("wow_class_manager").warning("private-test-secret")
    logs = output.getvalue()
    assert "private gateway payload" not in logs and "PyNaCl" not in logs
    assert "Connection interrupted" in logs and "[REDACTED]" in logs
    assert "private-test-secret" not in logs


async def test_webhook_disabled_does_not_create_session(monkeypatch):
    factory = MagicMock(side_effect=AssertionError("No outbound request expected"))
    monkeypatch.setattr(
        "wow_class_manager.notifications.aiohttp.ClientSession", factory
    )
    notifier = Notifier(None, Redactor())
    notifier.start()
    notifier.notify("test", "message")
    await notifier.close()
    factory.assert_not_called()


async def test_webhook_timeout_is_nonfatal(monkeypatch):
    response = MagicMock()
    response.__aenter__ = AsyncMock(side_effect=TimeoutError)
    response.__aexit__ = AsyncMock()
    session = MagicMock()
    session.post.return_value = response
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock()
    monkeypatch.setattr(
        "wow_class_manager.notifications.aiohttp.ClientSession",
        MagicMock(return_value=context),
    )
    notifier = Notifier("https://discord.com/api/webhooks/123/" + "x" * 32, Redactor())
    notifier.start()
    notifier.notify("test", "safe message")
    notifier.notify("test", "deduplicated")
    await asyncio.wait_for(notifier._queue.join(), 1)
    assert session.post.call_count == 1
    assert session.post.call_args.kwargs["allow_redirects"] is False
    assert session.post.call_args.kwargs["json"]["allowed_mentions"] == {"parse": []}
    await notifier.close()


async def test_graceful_shutdown(settings, store, monkeypatch):
    bot = ClassBot(settings, store, Redactor())
    close_discord = AsyncMock()
    monkeypatch.setattr(discord.Client, "close", close_discord)
    started = asyncio.Event()

    async def worker():
        started.set()
        await asyncio.Event().wait()

    task = bot._spawn(worker(), "test-background")
    await started.wait()
    await bot.close()
    assert task.cancelled()
    assert store._connection is None
    assert check_health(settings.health_path)[0] == 1
    assert bot.controller.stopping
    close_discord.assert_awaited_once()
    await bot.close()
    close_discord.assert_awaited_once()


async def test_graceful_shutdown_drains_requests(settings, store, monkeypatch):
    bot = ClassBot(settings, store, Redactor())
    monkeypatch.setattr(discord.Client, "close", AsyncMock())
    completed = False

    async def request():
        nonlocal completed
        await asyncio.sleep(0.01)
        assert store._connection is not None
        completed = True

    task = asyncio.create_task(request())
    bot.controller.active.add(task)
    await bot.close()
    assert completed


async def test_command_scope_change_cleans_previous(settings, store):
    global_bot = ClassBot(settings, store, Redactor())
    global_bot.tree.fetch_commands = AsyncMock(
        return_value=[SimpleNamespace(name="class")]
    )
    global_bot.tree.sync = AsyncMock()
    await global_bot.sync_commands()
    guild_bot = ClassBot(
        replace(settings, command_sync_scope="guild", development_guild_id=123),
        store,
        Redactor(),
    )
    guild_bot.tree.fetch_commands = AsyncMock(return_value=[])
    guild_bot.tree.sync = AsyncMock()
    await guild_bot.sync_commands()
    assert guild_bot.tree.sync.await_count == 2
    assert guild_bot.tree.sync.await_args_list[0].kwargs["guild"].id == 123
    assert guild_bot.tree.get_command("class") is not None


def test_container_defaults_and_workflows():
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "compose.yaml").read_text())
    bot = compose["services"]["bot"]
    assert "ports" not in bot and not bot.get("privileged")
    assert bot["read_only"] and bot["cap_drop"] == ["ALL"]
    assert bot["volumes"] == ["bot-data:/data"]
    assert "no-new-privileges:true" in bot["security_opt"]
    dockerfile = (root / "Dockerfile").read_text()
    assert "USER 10001:10001" in dockerfile
    assert "--locked" in dockerfile and "@sha256:" in dockerfile
    assert 'CMD ["python", "-m", "wow_class_manager"]' in dockerfile
    assert "EXPOSE" not in dockerfile and "COPY . " not in dockerfile
    publish = yaml.safe_load((root / ".github/workflows/publish.yaml").read_text())
    assert publish["jobs"]["publish"]["permissions"] == {
        "contents": "read",
        "packages": "write",
    }
    assert publish["jobs"]["publish"]["needs"] == "validate"
    for name in ("ci.yaml", "publish.yaml"):
        workflow = yaml.safe_load((root / ".github/workflows" / name).read_text())
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if "uses" in step:
                    assert len(step["uses"].split("@")[1]) == 40
    import tomllib

    assert (
        tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
        == __version__
    )
