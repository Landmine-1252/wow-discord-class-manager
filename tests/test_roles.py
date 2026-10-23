from dataclasses import replace

import discord
import pytest
from conftest import FakeRole, http_error

from wow_class_manager.config import Settings
from wow_class_manager.roles import RoleManager, UserError


async def test_empty_guild_and_repeated_startup(settings, store, locks, guild):
    manager = RoleManager(settings, store, locks)
    first = await manager.reconcile(guild)
    assert first.created == 9 and not first.warnings
    assert len(await store.roles(guild.id)) == 9
    for role, configured in zip(guild.roles[1:], settings.classes, strict=True):
        assert role.name == configured.name and role.colour.value == configured.color
        assert role.permissions.value == 0 and not role.hoist and not role.mentionable
    second = await RoleManager(settings, store, locks).reconcile(guild)
    assert second.created == 0 and second.reused == 9
    assert guild.create_role.await_count == 9


@pytest.mark.parametrize("count", [4, 9])
async def test_existing_roles(settings, store, locks, guild, count):
    guild.roles.extend(
        FakeRole(10 + i, entry.name) for i, entry in enumerate(settings.classes[:count])
    )
    result = await RoleManager(settings, store, locks).reconcile(guild)
    assert result.reused == count and result.created == 9 - count
    assert result.updated == count and not result.warnings


async def test_rename_uses_id(env, settings, store, locks, guild):
    await RoleManager(settings, store, locks).reconcile(guild)
    before = await store.roles(guild.id)
    env.update(ROLE_NAME_PALADIN="Holy Knight", ROLE_COLOR_PALADIN="#112233")
    result = await RoleManager(Settings.from_env(env), store, locks).reconcile(guild)
    assert result.created == 0 and result.updated == 1
    assert before == await store.roles(guild.id)
    assert (
        next(role for role in guild.roles if role.id == before["paladin"]).name
        == "Holy Knight"
    )


async def test_deleted_association(settings, store, locks, guild):
    await store.associate(guild.id, "mage", 99999)
    role = FakeRole(50, "Mage")
    guild.roles.append(role)
    result = await RoleManager(settings, store, locks).reconcile(guild)
    assert result.reused == 1
    assert (await store.roles(guild.id))["mage"] == role.id


async def test_duplicate_names_fail_closed(settings, store, locks, guild):
    guild.roles.extend([FakeRole(10, "Mage"), FakeRole(11, "mage")])
    result = await RoleManager(settings, store, locks).reconcile(guild)
    assert result.created == 8 and len(result.warnings) == 1
    assert "duplicate" in result.warnings[0]
    assert "mage" not in await store.roles(guild.id)


@pytest.mark.parametrize("unsafe", ["permission", "managed", "hierarchy", "channel"])
async def test_unsafe_role_not_adopted(settings, store, locks, guild, unsafe):
    role = FakeRole(10, "Mage")
    if unsafe == "permission":
        role.permissions = discord.Permissions(administrator=True)
    elif unsafe == "managed":
        role.managed = True
    elif unsafe == "hierarchy":
        role.position = 101
    else:
        channel = guild.channel()
        channel.overwrites_for.side_effect = lambda candidate: (
            discord.PermissionOverwrite(view_channel=True)
            if candidate.id == role.id
            else discord.PermissionOverwrite()
        )
    guild.roles.append(role)
    result = await RoleManager(settings, store, locks).reconcile(guild)
    assert len(result.warnings) == 1 and not role.edits
    assert "mage" not in await store.roles(guild.id)
    assert result.created == 8


async def test_known_role_becomes_unsafe(settings, store, locks, guild):
    role = FakeRole(50, "Mage", permissions=discord.Permissions(manage_roles=True))
    guild.roles.append(role)
    await store.associate(guild.id, "mage", role.id)
    result = await RoleManager(settings, store, locks).reconcile(guild)
    assert result.warnings and not role.edits
    assert (await store.roles(guild.id))["mage"] == role.id


async def test_missing_manage_roles(settings, store, locks, guild):
    guild.me.guild_permissions.manage_roles = False
    result = await RoleManager(settings, store, locks).reconcile(guild)
    assert result.warnings and result.created == 0
    guild.create_role.assert_not_called()


async def test_api_failure_per_class(settings, store, locks, guild):
    normal = guild.create_role.side_effect

    async def fail_one(**kwargs):
        if kwargs["name"] == "Mage":
            raise http_error()
        return await normal(**kwargs)

    guild.create_role.side_effect = fail_one
    result = await RoleManager(settings, store, locks).reconcile(guild)
    assert result.created == 8 and len(result.warnings) == 1


async def test_allowlist_enforced(settings, store, locks, guild):
    manager = RoleManager(
        replace(settings, allowed_guild_ids=frozenset({999})), store, locks
    )
    with pytest.raises(UserError, match="ALLOWED"):
        await manager.reconcile(guild)
    guild.fetch_roles.assert_not_called()


async def test_no_auto_creation(settings, store, locks, guild):
    result = await RoleManager(
        replace(settings, auto_create_roles=False), store, locks
    ).reconcile(guild)
    assert result.created == 0 and len(result.warnings) == 9


async def test_disabled_class_untouched(settings, store, locks, guild):
    await RoleManager(settings, store, locks).reconcile(guild)
    mapping = await store.roles(guild.id)
    disabled = next(role for role in guild.roles if role.id == mapping["mage"])
    disabled.colour = discord.Colour(1)
    subset = replace(
        settings,
        classes=tuple(entry for entry in settings.classes if entry.id != "mage"),
    )
    result = await RoleManager(subset, store, locks).reconcile(guild)
    assert result.created == 0 and disabled.colour.value == 1
    assert (await store.roles(guild.id))["mage"] == disabled.id
