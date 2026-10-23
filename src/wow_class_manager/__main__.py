"""Signal-aware executable; all startup errors are secret-free."""

import asyncio
import contextlib
import logging
import os
import signal

import discord

from .bot import ClassBot
from .config import ConfigError, Settings
from .health import write_health
from .logging_setup import configure_logging
from .storage import StorageError, Store

log = logging.getLogger(__name__)


async def run(settings: Settings) -> None:
    redact = configure_logging(
        settings.log_level, settings.token, settings.admin_webhook_url or ""
    )
    store = Store(settings.database_path)
    await store.open()
    bot = ClassBot(settings, store, redact)
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    connection: asyncio.Task | None = None
    shutdown: asyncio.Task | None = None
    try:
        await asyncio.to_thread(write_health, settings.health_path, "starting")
        async with bot:
            connection = asyncio.create_task(
                bot.start(settings.token, reconnect=True), name="discord-connection"
            )
            shutdown = asyncio.create_task(stopped.wait(), name="shutdown-signal")
            done, _ = await asyncio.wait(
                {connection, shutdown}, return_when=asyncio.FIRST_COMPLETED
            )
            if connection in done:
                await connection
    finally:
        await bot.close()
        for task in (connection, shutdown):
            if task:
                task.cancel()
                # A completed connection error was already raised above. Cleanup
                # must still remove signal handlers and close all resources.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


def main() -> None:
    os.umask(0o077)
    configure_logging("INFO")
    try:
        settings = Settings.from_env()
        asyncio.run(run(settings))
    except ConfigError as error:
        log.error("configuration_invalid: %s", error)
        raise SystemExit(2) from None
    except discord.LoginFailure:
        log.error(
            "discord_login_failed: token rejected; reset it in Developer Portal → Bot and update the configured token source."
        )
        raise SystemExit(2) from None
    except StorageError as error:
        log.error("database_unavailable: %s", error)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        pass
    except Exception as error:
        log.error(
            "application_failed error_type=%s; check network, permissions and configuration",
            type(error).__name__,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
