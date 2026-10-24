"""Gateway lifecycle, command registration, startup recovery and shutdown."""

import asyncio
import contextlib
import hashlib
import json
import logging
import math

import discord
from discord import app_commands

from . import __version__, diagnostics
from .character_commands import build_character_commands, build_roster_command
from .character_ui import CharacterView
from .characters import CharacterManager
from .commands import Controller, build_commands
from .config import ConfigError, Settings
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
        self._guild_issues: dict[int, tuple[str, ...]] = {}
        self._guild_check_versions: dict[int, int] = {}
        self._skipped_guilds: set[int] = set()
        self._last_health: str | None = None
        self._command_error = False
        self._ever_ready = False
        self._commands_ready = False
        self._closing = False

    def _spawn(self, coroutine, name: str) -> asyncio.Task:
        task = asyncio.create_task(coroutine, name=name)
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)
        return task

    async def setup_hook(self) -> None:
        # discord.py has already authenticated and fetched AppInfo at this point.
        application = self.application
        if application is not None:
            log.info(
                "discord_authenticated bot_id=%s application_id=%s"
                " configured_application_id=%s version=%s",
                self.user.id if self.user else None,
                application.id,
                self.settings.application_id or "auto",
                __version__,
            )
            if (
                self.settings.application_id is not None
                and self.settings.application_id != application.id
            ):
                raise ConfigError(
                    "DISCORD_APPLICATION_ID does not match the application"
                    " authenticated by the bot token. Use the ID from that"
                    " application's General Information page, or leave it unset."
                )
            if application.interactions_endpoint_url:
                raise ConfigError(
                    "Clear Interactions Endpoint URL in the Discord Developer Portal."
                    " This bot receives interactions through the Gateway."
                )
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
        self._command_error = False
        log.info("commands_ready scope=%s", scope)

    async def _register_until_success(self) -> None:
        while not self._closing:
            try:
                await self.sync_commands()
                return
            except Exception as error:
                self._command_error = True
                log.error(
                    "command_registration_failed error_type=%s http_status=%s"
                    " discord_code=%s retry_seconds=60; check Guild Install, bot and"
                    " applications.commands scopes, application ID and command scope",
                    type(error).__name__,
                    getattr(error, "status", None),
                    getattr(error, "code", None),
                )
                self.notifier.notify(
                    "command-registration",
                    "Command registration failed. Check the application's installation"
                    " scopes, application ID and connectivity. Retrying automatically.",
                )
                await asyncio.sleep(60)

    def readiness(self) -> str:
        """Combine connectivity with cached startup checks; no network requests."""
        if not self._ever_ready:
            return "starting"
        if not self.is_ready() or not math.isfinite(self.latency) or self.latency >= 90:
            return "disconnected"
        guild_ids = {guild.id for guild in self.guilds}
        eligible = {
            guild_id for guild_id in guild_ids if self.settings.allowed(guild_id)
        }
        if not eligible:
            return "waiting_for_guild"
        expected = set(self.settings.allowed_guild_ids)
        if self.settings.command_sync_scope == "guild":
            expected.add(self.settings.development_guild_id)
        if expected - guild_ids or self._command_error:
            return "degraded"
        if any(
            guild.id in eligible and getattr(guild, "unavailable", False)
            for guild in self.guilds
        ):
            return "degraded"
        if any(self._guild_issues.get(guild_id) for guild_id in eligible):
            return "degraded"
        if not self._commands_ready or not eligible <= self._prepared:
            return "starting"
        return "connected"

    async def _heartbeat(self) -> None:
        while not self._closing:
            state = self.readiness()
            try:
                await self.store.get_meta("health")
                await asyncio.to_thread(write_health, self.settings.health_path, state)
                if state != self._last_health:
                    log.info("startup_health state=%s", state)
                    self._last_health = state
            except Exception as error:
                # Stale heartbeat becomes unhealthy if storage or local writes fail.
                log.error("health_write_failed error_type=%s", type(error).__name__)
            await asyncio.sleep(10)

    async def on_ready(self) -> None:
        self._ever_ready = True
        log.info("gateway_ready guild_count=%s", len(self.guilds))
        self._check_installations()
        for guild in self.guilds:
            self._prepare(guild)

    async def on_resumed(self) -> None:
        log.info("gateway_resumed")
        self._check_installations()
        for guild in self.guilds:
            self._prepare(guild)

    async def on_disconnect(self) -> None:
        for guild_id in self._guild_check_versions:
            self._guild_check_versions[guild_id] += 1
        for worker in self._guild_workers.values():
            worker.cancel()
        self._guild_workers.clear()
        self._prepared.clear()
        log.warning("gateway_disconnected reconnect=automatic")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("guild_joined guild_id=%s", guild.id)
        self._check_installations()
        self._prepare(guild)

    async def on_guild_available(self, guild: discord.Guild) -> None:
        self._prepare(guild)

    async def on_guild_unavailable(self, guild: discord.Guild) -> None:
        self._guild_check_versions[guild.id] = (
            self._guild_check_versions.get(guild.id, 0) + 1
        )
        worker = self._guild_workers.pop(guild.id, None)
        if worker:
            worker.cancel()
        self._prepared.discard(guild.id)
        self._guild_issues[guild.id] = ("Discord reports this server unavailable.",)
        log.warning(
            "guild_unavailable guild_id=%s; waiting for Discord recovery", guild.id
        )

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self._guild_check_versions[guild.id] = (
            self._guild_check_versions.get(guild.id, 0) + 1
        )
        task = self._guild_workers.pop(guild.id, None)
        if task:
            task.cancel()
        self._prepared.discard(guild.id)
        self._guild_issues.pop(guild.id, None)
        self._skipped_guilds.discard(guild.id)
        log.warning("guild_removed guild_id=%s; stored data retained", guild.id)
        self._check_installations()

    def _check_installations(self) -> None:
        guild_ids = {guild.id for guild in self.guilds}
        application_id = (
            self.application.id if self.application else self.application_id
        )
        if not guild_ids:
            log.warning(
                "startup_no_guilds application_id=%s; connected to Discord but no"
                " servers are installed. Enable Guild Install and invite this"
                " application with bot and applications.commands scopes; adding only a"
                " user app or commands is insufficient. Confirm the token belongs to"
                " the invited application."
                " invite=https://discord.com/oauth2/authorize"
                "?client_id=%s&integration_type=0"
                "&scope=bot%%20applications.commands&permissions=268520448",
                application_id,
                application_id,
            )
        expected = set(self.settings.allowed_guild_ids)
        if self.settings.command_sync_scope == "guild":
            expected.add(self.settings.development_guild_id)
        for guild_id in sorted(expected - guild_ids):
            log.warning(
                "startup_guild_missing guild_id=%s application_id=%s; invite the bot to"
                " this configured server or correct"
                " ALLOWED_GUILD_IDS/DEVELOPMENT_GUILD_ID",
                guild_id,
                application_id,
            )

    def _prepare(self, guild: discord.Guild) -> None:
        if not self.settings.allowed(guild.id):
            if guild.id not in self._skipped_guilds:
                log.warning(
                    "guild_skipped guild_id=%s reason=not_allowlisted; add this server"
                    " ID to ALLOWED_GUILD_IDS to enable it",
                    guild.id,
                )
                self._skipped_guilds.add(guild.id)
            return
        if (
            guild.id in self._prepared
            or self._closing
            or getattr(guild, "unavailable", False)
        ):
            return
        if guild.id in self._guild_workers and not self._guild_workers[guild.id].done():
            return
        self._guild_workers[guild.id] = self._spawn(
            self._prepare_until_success(guild), f"guild-setup-{guild.id}"
        )

    def _schedule_guild_retry(self, guild: discord.Guild) -> None:
        if (
            not self._ever_ready
            or self._closing
            or guild.id not in {candidate.id for candidate in self.guilds}
            or getattr(guild, "unavailable", False)
        ):
            return
        worker = self._guild_workers.get(guild.id)
        if worker and not worker.done():
            return

        async def retry() -> None:
            await asyncio.sleep(300)
            if guild.id not in self._prepared and not self._closing:
                await self._prepare_until_success(guild)

        self._guild_workers[guild.id] = self._spawn(
            retry(), f"guild-recheck-{guild.id}"
        )

    async def check_guild(self, guild: discord.Guild) -> bool:
        """Refresh safe diagnostics after startup preparation or an admin repair."""
        if not self.settings.allowed(guild.id):
            return False
        version = self._guild_check_versions.setdefault(guild.id, 0)
        try:
            check = await diagnostics.inspect_guild(self.settings, self.store, guild)
        except Exception as error:
            if version != self._guild_check_versions[guild.id]:
                return False
            self._prepared.discard(guild.id)
            self._guild_issues[guild.id] = ("Server diagnostics failed; see bot logs.",)
            log.error(
                "guild_check_failed guild_id=%s error_type=%s http_status=%s"
                " discord_code=%s; check database access and Discord connectivity",
                guild.id,
                type(error).__name__,
                getattr(error, "status", None),
                getattr(error, "code", None),
            )
            self._schedule_guild_retry(guild)
            return False
        if version != self._guild_check_versions[guild.id]:
            # A leave/unavailable event supersedes this in-flight REST snapshot.
            return False
        if getattr(guild, "unavailable", False):
            check.warnings.append("Discord reports this server unavailable.")
        self._guild_issues[guild.id] = tuple(check.warnings)
        log.info(
            "guild_startup_check guild_id=%s manage_roles=%s associated_roles=%s"
            " manageable_roles=%s enabled_classes=%s panel=%s result=%s",
            guild.id,
            check.manage_roles,
            check.associated,
            check.manageable,
            len(self.settings.classes),
            check.panel_state,
            "needs_attention" if check.warnings else "ok",
        )
        if check.panel_state == "not_configured":
            log.info(
                "panel_not_configured guild_id=%s; run %s in the desired text channel"
                " to publish the character/class panel",
                guild.id,
                self.settings.command("setup"),
            )
        for warning in check.warnings:
            log.warning("guild_check_warning guild_id=%s detail=%s", guild.id, warning)
        if check.warnings:
            self._prepared.discard(guild.id)
            self._schedule_guild_retry(guild)
        else:
            self._prepared.add(guild.id)
        return not check.warnings

    async def _prepare_until_success(self, guild: discord.Guild) -> None:
        while not self._closing:
            try:
                result = await self.roles.reconcile(guild)
                result.warnings.extend(await self.controller.panels.refresh(guild))
                checked = await self.check_guild(guild)
                if not checked and not self._guild_issues.get(guild.id):
                    # A newer lifecycle event superseded this snapshot.
                    return
                result.warnings.extend(
                    warning
                    for warning in self._guild_issues.get(guild.id, ())
                    if warning not in result.warnings
                )
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
                self._prepared.discard(guild.id)
                self._guild_issues[guild.id] = tuple(result.warnings)
                log.warning(
                    "guild_setup_needs_attention guild_id=%s detail=%s",
                    guild.id,
                    result.summary(),
                )
                self.notifier.notify(
                    f"roles:{guild.id}", f"Guild {guild.id}: {result.summary()}"
                )
            except Exception as error:
                self._prepared.discard(guild.id)
                self._guild_issues[guild.id] = ("Startup checks failed; see bot logs.",)
                log.error(
                    "guild_setup_failed guild_id=%s error_type=%s http_status=%s"
                    " discord_code=%s; check server installation, role/channel"
                    " permissions and database access",
                    guild.id,
                    type(error).__name__,
                    getattr(error, "status", None),
                    getattr(error, "code", None),
                )
                self.notifier.notify(
                    f"startup:{guild.id}",
                    f"Guild {guild.id}: startup setup failed ({type(error).__name__})."
                    " Check permissions, database access and connectivity.",
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
