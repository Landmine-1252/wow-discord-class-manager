"""Read-only startup and administrator diagnostics using Discord resources."""

from dataclasses import dataclass, field

import discord

from . import __version__
from .config import Settings
from .roles import (
    UserError,
    api_problem,
    configured_roles,
    require_bot_member,
    unsafe_reason,
)
from .storage import Store
from .ui import missing_channel_permissions


@dataclass(slots=True)
class GuildCheck:
    associated: int = 0
    manageable: int = 0
    gameplay_associated: int = 0
    gameplay_manageable: int = 0
    manage_roles: bool = False
    panel_state: str = "not_configured"
    warnings: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)


async def inspect_guild(
    settings: Settings, store: Store, guild: discord.Guild
) -> GuildCheck:
    """Read roles and the saved panel without scanning or modifying members."""
    check = GuildCheck()
    try:
        require_bot_member(guild)
    except UserError as error:
        check.warnings.append(str(error))
        check.panel_state = "unverified"
        return check
    check.manage_roles = bool(guild.me and guild.me.guild_permissions.manage_roles)
    if not check.manage_roles:
        check.warnings.append("Grant the bot Manage Roles, then run sync.")
    mapping = await store.roles(guild.id)
    tag_mapping = await store.gameplay_roles(guild.id)
    try:
        roles = {role.id: role for role in await guild.fetch_roles()}
    except discord.HTTPException as error:
        check.warnings.append("Cannot verify server roles. " + api_problem(error))
        check.panel_state = "unverified"
        return check
    check.associated = sum(mapping.get(entry.id) in roles for entry in settings.classes)
    safe_roles = []
    for entry in configured_roles(settings):
        associations = tag_mapping if entry.gameplay else mapping
        role = roles.get(associations.get(entry.id))
        if entry.gameplay and role:
            check.gameplay_associated += 1
        reason = unsafe_reason(guild, role) if role else "missing; run sync"
        if reason:
            check.warnings.append(f"{entry.label}: {reason}.")
        else:
            if entry.gameplay:
                check.gameplay_manageable += 1
            else:
                safe_roles.append(role)
                check.manageable += 1
            if role.name != entry.name or role.colour.value != entry.color:
                check.warnings.append(
                    f"{entry.label}: configuration differs; run sync."
                )
    if safe_roles:
        higher = [
            role
            for role in roles.values()
            if role.colour.value
            and any(role > managed for managed in safe_roles)
            and role.id not in mapping.values()
        ]
        check.details.append(
            f"Higher-priority unrelated colored roles: {len(higher)}. Members holding"
            " one may show its color instead. Officers can review role order; this bot"
            " never reorders roles."
        )
    panel = await store.panel(guild.id)
    if not panel:
        check.details.append(
            f"Panel: not configured. Run {settings.command('setup')} in a text channel."
        )
    else:
        check.details.append(
            f"Panel channel: {panel.channel_id}; message: {panel.message_id}."
        )
        try:
            channel = await guild.fetch_channel(panel.channel_id)
            if not isinstance(channel, discord.TextChannel):
                check.panel_state = "unsupported_channel"
                check.warnings.append(
                    "Panel channel is not a supported text channel; rerun setup."
                )
            else:
                missing = (
                    missing_channel_permissions(channel, guild.me)
                    if guild.me
                    else ["Bot membership unavailable"]
                )
                check.details.append(
                    "Panel channel permissions: "
                    + (", ".join(missing) + " missing." if missing else "OK.")
                )
                if missing:
                    check.warnings.append(
                        "Restore these panel channel permissions: "
                        + ", ".join(missing)
                        + "."
                    )
                message = await channel.fetch_message(panel.message_id)
                owned = bool(guild.me and message.author.id == guild.me.id)
                check.panel_state = "ready" if owned and not missing else "unavailable"
                check.details.append(
                    "Panel: exists and owned by this bot."
                    if owned
                    else (
                        "Panel: unexpected author; restore the matching token/database"
                        " pair."
                    )
                )
                if not owned:
                    check.warnings.append(
                        "Panel has an unexpected author; restore the matching"
                        " token/database pair."
                    )
        except discord.NotFound as error:
            check.panel_state = (
                "missing" if error.code in {10003, 10008} else "unavailable"
            )
            check.warnings.append("Panel: cannot verify. " + api_problem(error))
        except discord.HTTPException as error:
            check.panel_state = "unavailable"
            check.warnings.append("Panel: cannot verify. " + api_problem(error))
    return check


async def status(settings: Settings, store: Store, guild: discord.Guild) -> str:
    check = await inspect_guild(settings, store, guild)
    lines = [
        f"WoW Class Manager v{__version__}",
        f"Guild: {guild.id}",
        (
            f"Enabled classes: {len(settings.classes)}; associated roles present:"
            f" {check.associated}; manageable: {check.manageable}"
        ),
        f"Command scope: {settings.command_sync_scope}",
        "Manage Roles: " + ("granted" if check.manage_roles else "MISSING"),
        *check.details,
        *check.warnings,
    ]
    if settings.enable_character_management:
        lines.insert(
            3,
            f"Gameplay tags: {check.gameplay_associated}/3 present; manageable:"
            f" {check.gameplay_manageable}. Click a member's name to see their main's"
            " tag in the server profile; no boosts needed.",
        )
    lines.append(
        "Class roles must be cosmetic: no role permissions or channel grants. Disabled"
        " classes and unrelated member roles are preserved."
    )
    return "\n".join(lines)
