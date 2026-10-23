"""Gateway lifecycle, command registration, startup recovery and shutdown."""

import asyncio
import contextlib
import hashlib
import json
import logging
import math

import discord
from discord import app_commands

from .character_commands import build_character_commands, build_roster_command
from .character_ui import CharacterView
from .characters import CharacterManager
from .commands import Controller, build_commands
from .config import Settings
from .health import write_health
from .logging_setup import Redactor
from .members import MemberManager
from .notifications import Notifier
from .roles import Locks, RoleManager
from .storage import Store
from .ui import ClassView

log = logging.getLogger(__name__)


class ClassBot(discord.Client):
    def __init__(self, settings: Settings, store: Store, redact: Redactor):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(
            intents=intents,
            application_id=settings.application_id,
            allowed_mentions=discord.AllowedMentions.none(),
            member_cache_flags=discord.MemberCacheFlags.none(),
            max_messages=None,
            enable_debug_events=False,
        )
        self.settings, self.store, self.redact = settings, store, redact
        self.locks = Locks()
        self.roles = RoleManager(settings, store, self.locks)
        self.members = MemberManager(settings, store, self.locks)
        self.characters = CharacterManager(settings, store, self.locks, self.members)
        self.notifier = Notifier(settings.admin_webhook_url, redact)
        self.controller = Controller(self)
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(build_commands(self.controller))
        if settings.enable_character_management:
            self.tree.add_command(
                build_character_commands(self.controller.characters_ui)
            )
            if (
                settings.enable_guild_roster
                and settings.roster_visibility != "disabled"
            ):
                self.tree.add_command(
                    build_roster_command(self.controller.characters_ui)
                )
        self.tree.on_error = self.controller.handle_error
        self._workers: set[asyncio.Task] = set()
        self._guild_workers: dict[int, asyncio.Task] = {}
        self._prepared: set[int] = set()
        self._ever_ready = False
        self._commands_ready = False
        self._closing = False

    def _spawn(self, coroutine, name: str) -> asyncio.Task:
        task = asyncio.create_task(coroutine, name=name)
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)
        return task

    async def setup_hook(self) -> None:
        # Registered before the first READY, independent of cached messages.
        self.add_view(ClassView(self.controller, register_all=True))
        self.add_view(CharacterView(self.controller, register_all=True))
        self.notifier.start()
        self._spawn(self._heartbeat(), "health-heartbeat")
        self._spawn(self._register_until_success(), "command-registration")

    async def sync_commands(self) -> None:
        scope = (
            str(self.settings.development_guild_id)
            if self.settings.command_sync_scope == "guild"
            else "global"
        )
        target = discord.Object(id=int(scope)) if scope != "global" else None
        if target:
            self.tree.copy_global_to(guild=target)
        payload = [
            command.to_dict(self.tree)
            for command in self.tree.get_commands(guild=target)
        ]
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        key = f"commands:{self.application_id}"
        previous_raw = await self.store.get_meta(key)
        previous = json.loads(previous_raw) if previous_raw else {}
        current = {"scope": scope, "fingerprint": fingerprint}
        remote = await self.tree.fetch_commands(guild=target)
        remote_names = {command.name for command in remote}
        expected_names = {
            command.name for command in self.tree.get_commands(guild=target)
        }
        if previous != current or remote_names != expected_names:
            await self.tree.sync(guild=target)
        if previous and previous["scope"] != scope:
            old_target = (
                discord.Object(id=int(previous["scope"]))
                if previous["scope"] != "global"
                else None
            )
            # This dedicated application owns its whole command tree.
            if old_target is None:
                commands = self.tree.get_commands()
                self.tree.clear_commands(guild=None)
                try:
                    await self.tree.sync()
                finally:
                    for command in commands:
                        self.tree.add_command(command)
            else:
                self.tree.clear_commands(guild=old_target)
                await self.tree.sync(guild=old_target)
        await self.store.set_meta(key, json.dumps(current))
        self._commands_ready = True
        log.info("commands_ready scope=%s", scope)

    async def _register_until_success(self) -> None:
        while not self._closing:
            try:
                await self.sync_commands()
                return
            except Exception as error:
                log.error(
                    "command_registration_failed error_type=%s retry_seconds=60",
                    type(error).__name__,
                )
                self.notifier.notify(
                    "command-registration",
                    "Command registration failed. Check the application's installation scopes, application ID and connectivity. Retrying automatically.",
                )
                await asyncio.sleep(60)

    async def _heartbeat(self) -> None:
        while not self._closing:
            state = (
                "starting"
                if not self._ever_ready or not self._commands_ready
                else "disconnected"
            )
            if (
                self._commands_ready
                and self.is_ready()
                and math.isfinite(self.latency)
                and self.latency < 90
            ):
                state = "connected"
            try:
                await self.store.get_meta("health")
                await asyncio.to_thread(write_health, self.settings.health_path, state)
            except Exception as error:
                # Stale heartbeat becomes unhealthy if storage or local writes fail.
                log.error("health_write_failed error_type=%s", type(error).__name__)
            await asyncio.sleep(10)

    async def on_ready(self) -> None:
        self._ever_ready = True
        log.info("gateway_ready guild_count=%s", len(self.guilds))
        for guild in self.guilds:
            self._prepare(guild)

    async def on_resumed(self) -> None:
        log.info("gateway_resumed")

    async def on_disconnect(self) -> None:
        log.warning("gateway_disconnected reconnect=automatic")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        self._prepare(guild)

    async def on_guild_available(self, guild: discord.Guild) -> None:
        self._prepare(guild)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        task = self._guild_workers.pop(guild.id, None)
        if task:
            task.cancel()
        self._prepared.discard(guild.id)

    def _prepare(self, guild: discord.Guild) -> None:
        if (
            not self.settings.allowed(guild.id)
            or guild.id in self._prepared
            or self._closing
        ):
            return
        if guild.id in self._guild_workers and not self._guild_workers[guild.id].done():
            return
        self._guild_workers[guild.id] = self._spawn(
            self._prepare_until_success(guild), f"guild-setup-{guild.id}"
        )

    async def _prepare_until_success(self, guild: discord.Guild) -> None:
        while not self._closing:
            try:
                result = await self.roles.reconcile(guild)
                result.warnings.extend(await self.controller.panels.refresh(guild))
                if not result.warnings:
                    self._prepared.add(guild.id)
                    log.info(
                        "guild_ready guild_id=%s created=%s reused=%s updated=%s",
                        guild.id,
                        result.created,
                        result.reused,
                        result.updated,
                    )
                    return
                log.warning(
                    "guild_setup_needs_attention guild_id=%s detail=%s",
                    guild.id,
                    result.summary(),
                )
                self.notifier.notify(
                    f"roles:{guild.id}", f"Guild {guild.id}: {result.summary()}"
                )
            except Exception as error:
                log.error(
                    "guild_setup_failed guild_id=%s error_type=%s",
                    guild.id,
                    type(error).__name__,
                )
                self.notifier.notify(
                    f"startup:{guild.id}",
                    f"Guild {guild.id}: startup setup failed ({type(error).__name__}). Check permissions, database access and connectivity.",
                )
            await asyncio.sleep(300)

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        # Do not dump event arguments (which may contain personal information).
        log.error("gateway_event_failed event=%s", event_method)

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.controller.stopping = True
        if self.controller.active:
            _, pending = await asyncio.wait(self.controller.active, timeout=15)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for task in tuple(self._workers):
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        try:
            await self.notifier.close()
        finally:
            try:
                await super().close()
            finally:
                await self.store.close()
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(
                        write_health, self.settings.health_path, "stopped"
                    )
        log.info("shutdown_complete")
