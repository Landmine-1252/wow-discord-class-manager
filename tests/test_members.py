import asyncio
from dataclasses import replace

import aiohttp
import discord
import pytest
from conftest import FakeRole, http_error

from wow_class_manager.members import MemberManager
from wow_class_manager.roles import RoleManager, UserError


@pytest.fixture
async def member_setup(settings, store, locks, guild):
    await RoleManager(settings, store, locks).reconcile(guild)
    mapping = await store.roles(guild.id)
    roles = {
        key: next(role for role in guild.roles if role.id == rid)
        for key, rid in mapping.items()
    }
    manager = MemberManager(settings, store, locks)
    unrelated = FakeRole(
        88, "Guild Member", permissions=discord.Permissions(view_channel=True)
    )
    guild.roles.append(unrelated)
    member = guild.member(roles=[unrelated])
    return manager, member, roles, unrelated


async def test_select_switch_same_and_preserve(guild, member_setup):
    manager, member, roles, unrelated = member_setup
    assert (
        await manager.change(guild, member.id, "paladin")
        == "Your class has been updated to Paladin."
    )
    assert {role.id for role in member.roles} == {unrelated.id, roles["paladin"].id}
    assert (
        await manager.change(guild, member.id, "mage")
        == "Your class has been updated to Mage."
    )
    assert {role.id for role in member.roles} == {unrelated.id, roles["mage"].id}
    count = member.add_roles.await_count
    assert "already" in await manager.change(guild, member.id, "mage")
    assert member.add_roles.await_count == count
    member.edit.assert_not_called()


async def test_clear_and_current(settings, store, locks, guild, member_setup):
    _, member, roles, unrelated = member_setup
    manager = MemberManager(replace(settings, allow_clear_selection=True), store, locks)
    member.roles += [roles["mage"], roles["paladin"]]
    assert "Mage" in await manager.current(guild, member.id)
    assert "cleared" in await manager.change(guild, member.id, None)
    assert member.roles == [unrelated]
    assert "no enabled class" in await manager.current(guild, member.id)


async def test_clear_disabled(guild, member_setup):
    manager, member, _, _ = member_setup
    with pytest.raises(UserError, match="disabled"):
        await manager.change(guild, member.id, None)


async def test_multiple_class_roles(guild, member_setup):
    manager, member, roles, unrelated = member_setup
    member.roles += [roles["mage"], roles["paladin"], roles["hunter"]]
    await manager.change(guild, member.id, "mage")
    assert {role.id for role in member.roles} == {unrelated.id, roles["mage"].id}


async def test_assignment_failure_preserves_previous(guild, member_setup):
    manager, member, roles, unrelated = member_setup
    member.roles.append(roles["paladin"])
    member.add_roles.side_effect = http_error()
    with pytest.raises(UserError, match="not completed"):
        await manager.change(guild, member.id, "mage")
    assert {role.id for role in member.roles} == {unrelated.id, roles["paladin"].id}
    member.remove_roles.assert_not_called()


async def test_partial_removal_failure_rolls_back(guild, member_setup):
    manager, member, roles, unrelated = member_setup
    member.roles += [roles["paladin"], roles["hunter"]]
    original = member.remove_roles.side_effect
    calls = 0

    async def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise http_error()
        await original(*args, **kwargs)

    member.remove_roles.side_effect = fail_once
    with pytest.raises(UserError, match="restored"):
        await manager.change(guild, member.id, "mage")
    assert {role.id for role in member.roles} == {
        unrelated.id,
        roles["paladin"].id,
        roles["hunter"].id,
    }


async def test_ambiguous_add_outcome_is_recovered(guild, member_setup):
    manager, member, roles, unrelated = member_setup
    original = member.add_roles.side_effect

    async def applied_but_error(*args, **kwargs):
        await original(*args, **kwargs)
        raise http_error(discord.HTTPException)

    member.add_roles.side_effect = applied_but_error
    with pytest.raises(UserError, match="restored"):
        await manager.change(guild, member.id, "mage")
    assert member.roles == [unrelated]


async def test_cannot_confirm_failure_is_never_success(guild, member_setup):
    manager, member, _, _ = member_setup
    original = guild.fetch_member.side_effect
    calls = 0

    async def missing_confirmation(member_id):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise http_error(discord.HTTPException)
        return await original(member_id)

    guild.fetch_member.side_effect = missing_confirmation
    with pytest.raises(UserError, match="could not be confirmed"):
        await manager.change(guild, member.id, "mage")


async def test_same_member_concurrent_requests(guild, member_setup):
    manager, member, roles, unrelated = member_setup
    await asyncio.gather(
        manager.change(guild, member.id, "mage"),
        manager.change(guild, member.id, "paladin"),
    )
    assert {role.id for role in member.roles} == {unrelated.id, roles["paladin"].id}


async def test_different_members_do_not_block(guild, member_setup):
    manager, first, roles, _ = member_setup
    second = guild.member(789)
    entered, release = asyncio.Event(), asyncio.Event()
    original = first.add_roles.side_effect

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        await original(*args, **kwargs)

    first.add_roles.side_effect = slow
    task = asyncio.create_task(manager.change(guild, first.id, "mage"))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        await asyncio.wait_for(manager.change(guild, second.id, "paladin"), 1)
        assert roles["paladin"] in second.roles
    finally:
        release.set()
        await task


async def test_disabled_role_is_preserved(settings, store, locks, guild, member_setup):
    _, member, roles, unrelated = member_setup
    member.roles.append(roles["paladin"])
    manager = MemberManager(
        replace(
            settings,
            classes=tuple(entry for entry in settings.classes if entry.id != "paladin"),
        ),
        store,
        locks,
    )
    with pytest.raises(UserError, match="disabled"):
        await manager.change(guild, member.id, "paladin")
    await manager.change(guild, member.id, "mage")
    assert {role.id for role in member.roles} == {
        unrelated.id,
        roles["paladin"].id,
        roles["mage"].id,
    }


async def test_tampered_role_is_not_assigned(guild, member_setup):
    manager, member, roles, _ = member_setup
    roles["mage"].permissions = discord.Permissions(administrator=True)
    with pytest.raises(UserError, match="grants permissions"):
        await manager.change(guild, member.id, "mage")
    member.add_roles.assert_not_called()


async def test_allowlist_and_arbitrary_id(settings, store, locks, guild, member_setup):
    _, member, _, _ = member_setup
    manager = MemberManager(
        replace(settings, allowed_guild_ids=frozenset({999})), store, locks
    )
    with pytest.raises(UserError, match="ALLOWED"):
        await manager.change(guild, member.id, "mage")
    manager = MemberManager(settings, store, locks)
    with pytest.raises(UserError, match="unknown"):
        await manager.change(guild, member.id, "123456789")


async def test_cooldown(settings, store, locks, guild, member_setup):
    _, member, _, _ = member_setup
    manager = MemberManager(replace(settings, role_change_cooldown=3), store, locks)
    await manager.change(guild, member.id, "mage")
    with pytest.raises(UserError, match="wait"):
        await manager.change(guild, member.id, "paladin")


async def test_cancelled_selection_recovers(guild, member_setup):
    manager, member, roles, unrelated = member_setup
    entered = asyncio.Event()
    original = member.add_roles.side_effect

    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        entered.set()
        await asyncio.Event().wait()

    member.add_roles.side_effect = interrupted
    task = asyncio.create_task(manager.change(guild, member.id, "mage"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert member.roles == [unrelated]


@pytest.mark.parametrize("error_type", [OSError, aiohttp.ServerDisconnectedError])
async def test_transport_failure_during_removal_recovers(
    guild, member_setup, error_type
):
    manager, member, roles, unrelated = member_setup
    member.roles.append(roles["paladin"])
    original = member.remove_roles.side_effect
    calls = 0

    async def network_failure(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error_type("connection reset")
        await original(*args, **kwargs)

    member.remove_roles.side_effect = network_failure
    with pytest.raises(UserError, match="restored"):
        await manager.change(guild, member.id, "mage")
    assert {role.id for role in member.roles} == {roles["paladin"].id, unrelated.id}
