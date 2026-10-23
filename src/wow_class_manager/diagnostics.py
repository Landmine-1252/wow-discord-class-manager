"""Read-only administrator diagnostics using current Discord resources."""

import discord

from . import __version__
from .config import Settings
from .roles import api_problem, unsafe_reason
from .storage import Store
from .ui import missing_channel_permissions


async def status(settings: Settings, store: Store, guild: discord.Guild) -> str:
    mapping = await store.roles(guild.id)
    roles = {role.id: role for role in await guild.fetch_roles()}
    associated = sum(mapping.get(entry.id) in roles for entry in settings.classes)
    lines = [
        f"WoW Class Manager v{__version__}",
        f"Guild: {guild.id}",
        f"Enabled classes: {len(settings.classes)}; associated roles present: {associated}",
        f"Command scope: {settings.command_sync_scope}",
        "Manage Roles: "
        + (
            "granted"
            if guild.me and guild.me.guild_permissions.manage_roles
            else "MISSING"
        ),
    ]
    safe_roles = []
    for entry in settings.classes:
        role = roles.get(mapping.get(entry.id))
        reason = unsafe_reason(guild, role) if role else "missing; run sync"
        if reason:
            lines.append(f"{entry.definition.name}: {reason}.")
        else:
            safe_roles.append(role)
            if role.name != entry.name or role.colour.value != entry.color:
                lines.append(
                    f"{entry.definition.name}: configuration differs; run sync."
                )
    if safe_roles:
        higher = [
            role
            for role in roles.values()
            if role.colour.value
            and any(role > managed for managed in safe_roles)
            and role.id not in mapping.values()
        ]
        lines.append(
            f"Higher-priority unrelated colored roles: {len(higher)}. Members holding one may show its color instead. Officers can review role order; this bot never reorders roles."
        )
    panel = await store.panel(guild.id)
    if not panel:
        lines.append(
            f"Panel: not configured. Run {settings.command('setup')} in a text channel."
        )
    else:
        lines.append(f"Panel channel: {panel.channel_id}; message: {panel.message_id}.")
        try:
            channel = await guild.fetch_channel(panel.channel_id)
            if not isinstance(channel, discord.TextChannel):
                lines.append(
                    "Panel channel is not a supported text channel; rerun setup."
                )
            else:
                missing = (
                    missing_channel_permissions(channel, guild.me)
                    if guild.me
                    else ["Bot membership unavailable"]
                )
                lines.append(
                    "Panel channel permissions: "
                    + (", ".join(missing) + " missing." if missing else "OK.")
                )
                message = await channel.fetch_message(panel.message_id)
                lines.append(
                    "Panel: exists and owned by this bot."
                    if guild.me and message.author.id == guild.me.id
                    else "Panel: unexpected author; restore the matching token/database pair."
                )
        except discord.NotFound:
            lines.append(
                f"Panel/channel: deleted. Run {settings.command('setup')} to recreate it."
            )
        except discord.HTTPException as error:
            lines.append("Panel: cannot verify. " + api_problem(error))
    lines.append(
        "Class roles must be cosmetic: no role permissions or channel grants. Disabled classes and unrelated member roles are preserved."
    )
    return "\n".join(lines)
