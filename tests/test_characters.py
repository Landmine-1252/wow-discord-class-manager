import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import FakeGuild, FakeRole, http_error

from wow_class_manager.character_names import normalized_name_key
from wow_class_manager.characters import CharacterManager
from wow_class_manager.members import MemberManager
from wow_class_manager.roles import RoleManager, UserError
from wow_class_manager.storage import StorageError, Store


@pytest.fixture
async def character_setup(settings, store, locks, guild):
    await RoleManager(settings, store, locks).reconcile(guild)
    unrelated = FakeRole(
        88, "Guild Rank", permissions=discord.Permissions(view_channel=True)
    )
    guild.roles.append(unrelated)
    member = guild.member(roles=[unrelated])
    manager = CharacterManager(
        settings, store, locks, MemberManager(settings, store, locks)
    )
    return manager, member, unrelated


async def assigned(manager, guild, member):
    mapping = await manager.store.roles(guild.id)
    held = {role.id for role in member.roles}
    return {key for key, value in mapping.items() if value in held}


async def add_pair(manager, guild, member):
    await manager.add(guild, member.id, "John", "Stormwind", "paladin")
    await manager.add(guild, member.id, "Sam", "Ironforge", "warrior")
    roster = await manager.roster(guild, member.id)
    return roster.characters


async def test_complete_main_alt_sequence(character_setup, guild):
    manager, member, unrelated = character_setup
    assert (
        await manager.add(guild, member.id, "John", "Stormwind", "paladin")
        == "John Stormwind has been registered as your main Paladin."
    )
    roster = await manager.roster(guild, member.id)
    john = roster.main
    assert john.primary_name == "John" and john.secondary_name == "Stormwind"
    assert john.full_name == "John Stormwind"
    assert await assigned(manager, guild, member) == {"paladin"}
    assert (
        await manager.add(guild, member.id, "Sam", "Ironforge", "warrior")
        == "Sam Ironforge has been added as a Warrior alt."
    )
    roster = await manager.roster(guild, member.id)
    sam = roster.alts[0]
    assert roster.main.id == john.id and await assigned(manager, guild, member) == {
        "paladin"
    }
    assert "John Stormwind is now an alt" in await manager.promote(
        guild, member.id, sam.id
    )
    roster = await manager.roster(guild, member.id)
    assert roster.main.id == sam.id and roster.alts[0].id == john.id
    assert await assigned(manager, guild, member) == {"warrior"}
    await manager.promote(guild, member.id, john.id)
    assert await assigned(manager, guild, member) == {"paladin"}
    await manager.edit(guild, member.id, sam.id, "Sam", "Ironforge", "hunter")
    assert await assigned(manager, guild, member) == {"paladin"}
    await manager.edit(guild, member.id, john.id, "John", "Stormwind", "warrior")
    roster = await manager.roster(guild, member.id)
    assert roster.main.id == john.id and roster.main.class_id == "warrior"
    assert roster.alts[0].id == sam.id and roster.alts[0].class_id == "hunter"
    assert await assigned(manager, guild, member) == {"warrior"}
    assert unrelated in member.roles
    member.edit.assert_not_called()


async def test_current_main_and_same_class_switch_avoid_role_writes(
    character_setup, guild
):
    manager, member, _ = character_setup
    await manager.add(guild, member.id, "John", "Stormwind", "paladin")
    await manager.add(guild, member.id, "John", "Ironforge", "paladin")
    john, alt = (await manager.roster(guild, member.id)).characters
    member.add_roles.reset_mock()
    member.remove_roles.reset_mock()
    original_updated = john.updated_at
    assert "already your main" in await manager.promote(guild, member.id, john.id)
    await manager.promote(guild, member.id, alt.id)
    roster = await manager.roster(guild, member.id)
    assert roster.main.id == alt.id
    assert roster.characters[0].updated_at == original_updated
    member.add_roles.assert_not_called()
    member.remove_roles.assert_not_called()


async def test_renaming_preserves_id_and_main(character_setup, guild):
    manager, member, _ = character_setup
    john, sam = await add_pair(manager, guild, member)
    await manager.edit(guild, member.id, john.id, "John", "Ironforge", "paladin")
    await manager.edit(guild, member.id, sam.id, "Samuel", "Ironforge", "warrior")
    roster = await manager.roster(guild, member.id)
    assert roster.main.id == john.id and roster.main.full_name == "John Ironforge"
    assert roster.alts[0].id == sam.id and roster.alts[0].primary_name == "Samuel"


async def test_edit_accepts_current_version_and_preserves_newer_details_on_retry(
    character_setup, guild
):
    manager, member, unrelated = character_setup
    john, _ = await add_pair(manager, guild, member)
    assert (
        await manager.edit(
            guild,
            member.id,
            john.id,
            "Jonathan",
            "Ironforge",
            "mage",
            expected_updated_at=john.updated_at,
        )
        == "Jonathan Ironforge has been updated."
    )
    fresh = await manager.records.character(guild.id, member.id, john.id)
    assert fresh.updated_at != john.updated_at
    member.add_roles.reset_mock()
    member.remove_roles.reset_mock()
    with pytest.raises(UserError, match="changed while the edit form was open"):
        await manager.edit(
            guild,
            member.id,
            john.id,
            "John",
            "Stormwind",
            "paladin",
            expected_updated_at=john.updated_at,
        )
    assert await manager.records.character(guild.id, member.id, john.id) == fresh
    assert fresh.full_name == "Jonathan Ironforge" and fresh.class_id == "mage"
    assert await assigned(manager, guild, member) == {"mage"}
    assert unrelated in member.roles
    assert await manager.records.pending(guild.id, member.id) is None
    member.add_roles.assert_not_called()
    member.remove_roles.assert_not_called()


async def test_concurrent_edits_with_same_version_only_commit_once(
    character_setup, guild
):
    manager, member, _ = character_setup
    john, _ = await add_pair(manager, guild, member)
    results = await asyncio.gather(
        manager.edit(
            guild,
            member.id,
            john.id,
            "Jonathan",
            "Ironforge",
            "mage",
            expected_updated_at=john.updated_at,
        ),
        manager.edit(
            guild,
            member.id,
            john.id,
            "Johnny",
            "Stormwind",
            "warrior",
            expected_updated_at=john.updated_at,
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, UserError) for result in results) == 1
    current = await manager.records.character(guild.id, member.id, john.id)
    assert current.full_name == "Jonathan Ironforge" and current.class_id == "mage"
    assert await assigned(manager, guild, member) == {"mage"}


async def test_multiple_alts_unlimited_and_case_insensitive_duplicate(
    character_setup, guild
):
    manager, member, _ = character_setup
    await add_pair(manager, guild, member)
    await manager.add(guild, member.id, "John", "Ironforge", "mage")
    with pytest.raises(UserError, match="full name already exists"):
        await manager.add(guild, member.id, "john", "stormwind", "warlock")
    for index in range(30):
        suffix = chr(ord("A") + index // 26) + chr(ord("a") + index % 26)
        await manager.add(guild, member.id, "Alt" + suffix, "Stormwind", "warrior")
    roster = await manager.roster(guild, member.id)
    assert len(roster.characters) == 33 and len(roster.alts) == 32
    assert await assigned(manager, guild, member) == {"paladin"}


async def test_concurrent_main_switches_preserve_roster(character_setup, guild):
    manager, member, _ = character_setup
    john, sam = await add_pair(manager, guild, member)
    await asyncio.gather(
        manager.promote(guild, member.id, sam.id),
        manager.promote(guild, member.id, john.id),
    )
    roster = await manager.roster(guild, member.id)
    assert roster.main.id == john.id and len(roster.characters) == 2
    assert sum(item.is_main for item in roster.characters) == 1
    assert await assigned(manager, guild, member) == {"paladin"}


async def test_remove_alt_main_replacement_and_last(
    character_setup, guild, settings, store, locks
):
    manager, member, unrelated = character_setup
    john, sam = await add_pair(manager, guild, member)
    with pytest.raises(UserError, match="replacement"):
        await manager.remove(guild, member.id, john.id)
    await manager.remove(guild, member.id, john.id, sam.id)
    roster = await manager.roster(guild, member.id)
    assert roster.main.id == sam.id and len(roster.characters) == 1
    assert await assigned(manager, guild, member) == {"warrior"}
    with pytest.raises(UserError, match="last character"):
        await manager.remove(guild, member.id, sam.id)
    allowed = replace(settings, allow_clear_selection=True)
    manager = CharacterManager(
        allowed, store, locks, MemberManager(allowed, store, locks)
    )
    await manager.remove(guild, member.id, sam.id)
    assert not (await manager.roster(guild, member.id)).characters
    assert member.roles == [unrelated]
    john, sam = await add_pair(manager, guild, member)
    await manager.remove(guild, member.id, sam.id)
    assert (await manager.roster(guild, member.id)).main.id == john.id
    assert await assigned(manager, guild, member) == {"paladin"}


async def test_disabled_class_is_retained_until_explicit_valid_main(
    character_setup, guild, settings, store, locks
):
    manager, member, _ = character_setup
    john, sam = await add_pair(manager, guild, member)
    subset = replace(
        settings,
        classes=tuple(item for item in settings.classes if item.id != "paladin"),
    )
    manager = CharacterManager(
        subset, store, locks, MemberManager(subset, store, locks)
    )
    with pytest.raises(UserError, match="disabled"):
        await manager.roster(guild, member.id)
    assert await assigned(manager, guild, member) == {"paladin"}
    with pytest.raises(UserError, match="disabled"):
        await manager.promote(guild, member.id, john.id)
    await manager.promote(guild, member.id, sam.id)
    roster = await manager.roster(guild, member.id)
    assert len(roster.characters) == 2 and roster.main.id == sam.id
    assert await assigned(manager, guild, member) == {"warrior"}
    await manager.edit(guild, member.id, john.id, "John", "Ironforge", "paladin")


async def test_missing_or_duplicate_role_repaired_on_interaction(
    character_setup, guild
):
    manager, member, unrelated = character_setup
    await add_pair(manager, guild, member)
    mapping = await manager.store.roles(guild.id)
    member.roles = [
        unrelated,
        next(role for role in guild.roles if role.id == mapping["mage"]),
    ]
    await manager.roster(guild, member.id)
    assert await assigned(manager, guild, member) == {"paladin"}
    assert unrelated in member.roles


async def test_deleted_role_requires_sync_without_data_loss(character_setup, guild):
    manager, member, _ = character_setup
    john, sam = await add_pair(manager, guild, member)
    mapping = await manager.store.roles(guild.id)
    guild.roles = [role for role in guild.roles if role.id != mapping["warrior"]]
    with pytest.raises(UserError, match="not ready"):
        await manager.promote(guild, member.id, sam.id)
    assert (await manager.roster(guild, member.id)).main.id == john.id


async def test_hierarchy_failure_keeps_main(character_setup, guild):
    manager, member, _ = character_setup
    john, sam = await add_pair(manager, guild, member)
    mapping = await manager.store.roles(guild.id)
    next(role for role in guild.roles if role.id == mapping["warrior"]).position = 101
    with pytest.raises(UserError, match="above this class role"):
        await manager.promote(guild, member.id, sam.id)
    assert (await manager.records.roster(guild.id, member.id)).main.id == john.id


async def test_role_failure_compensates_and_can_retry(character_setup, guild):
    manager, member, _ = character_setup
    john, sam = await add_pair(manager, guild, member)
    original = member.remove_roles.side_effect
    calls = 0

    async def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise http_error()
        await original(*args, **kwargs)

    member.remove_roles.side_effect = fail_once
    with pytest.raises(UserError, match="previous roster and main were kept"):
        await manager.promote(guild, member.id, sam.id)
    assert (await manager.records.roster(guild.id, member.id)).main.id == john.id
    assert await assigned(manager, guild, member) == {"paladin"}
    assert await manager.records.pending(guild.id, member.id) is None
    await manager.promote(guild, member.id, sam.id)
    assert await assigned(manager, guild, member) == {"warrior"}


async def test_database_commit_failure_restores_roles(
    character_setup, guild, monkeypatch
):
    manager, member, _ = character_setup
    john, sam = await add_pair(manager, guild, member)
    monkeypatch.setattr(
        manager.records,
        "commit",
        AsyncMock(side_effect=StorageError("Simulated write failure")),
    )
    with pytest.raises(UserError, match="previous roster and main were kept"):
        await manager.promote(guild, member.id, sam.id)
    assert (await manager.records.roster(guild.id, member.id)).main.id == john.id
    assert await assigned(manager, guild, member) == {"paladin"}
    assert await manager.records.pending(guild.id, member.id) is None


async def test_error_after_successful_commit_does_not_undo_roles(
    character_setup, guild, monkeypatch
):
    manager, member, _ = character_setup
    _, sam = await add_pair(manager, guild, member)
    original = manager.records.commit

    async def uncertain(*args):
        await original(*args)
        raise StorageError("Simulated lost acknowledgment")

    monkeypatch.setattr(manager.records, "commit", uncertain)
    with pytest.raises(UserError, match="Reopen My Characters"):
        await manager.promote(guild, member.id, sam.id)
    assert (await manager.records.roster(guild.id, member.id)).main.id == sam.id
    assert await assigned(manager, guild, member) == {"warrior"}


async def test_incomplete_operation_recovers_after_restart(
    character_setup, guild, settings, store, locks
):
    manager, member, _ = character_setup
    john, sam = await add_pair(manager, guild, member)
    original = member.remove_roles.side_effect
    member.remove_roles.side_effect = http_error()
    with pytest.raises(UserError, match="Recovery is pending"):
        await manager.promote(guild, member.id, sam.id)
    assert await manager.records.pending(guild.id, member.id) is not None
    assert (await manager.records.roster(guild.id, member.id)).main.id == john.id
    member.remove_roles.side_effect = original
    await store.close()
    reopened = Store(store.path)
    await reopened.open()
    try:
        fresh = CharacterManager(
            settings, reopened, locks, MemberManager(settings, reopened, locks)
        )
        roster = await fresh.roster(guild, member.id)
        assert roster.main.id == john.id and len(roster.characters) == 2
        assert await assigned(fresh, guild, member) == {"paladin"}
        assert await fresh.records.pending(guild.id, member.id) is None
        await fresh.promote(guild, member.id, sam.id)
    finally:
        await reopened.close()


async def test_crash_after_roles_before_database_commit(character_setup, guild):
    manager, member, _ = character_setup
    john, sam = await add_pair(manager, guild, member)
    plan = await manager._role_plan(guild, member.id, "warrior")
    await manager.records.prepare(
        guild.id,
        member.id,
        "crash",
        {"kind": "promote", "character_id": sam.id},
        plan.previous,
        plan.wanted,
    )
    await manager.members.apply_locked(guild, member.id, plan, recover=False)
    assert await assigned(manager, guild, member) == {"warrior"}
    assert (await manager.roster(guild, member.id)).main.id == john.id
    assert await assigned(manager, guild, member) == {"paladin"}


async def test_failed_first_registration_preserves_legacy_class(
    character_setup, guild, monkeypatch
):
    manager, member, _ = character_setup
    await manager.members.change(guild, member.id, "mage")
    monkeypatch.setattr(
        manager.records,
        "commit",
        AsyncMock(side_effect=StorageError("Simulated write failure")),
    )
    with pytest.raises(UserError):
        await manager.add(guild, member.id, "John", "Stormwind", "paladin")
    assert not (await manager.roster(guild, member.id)).characters
    assert await assigned(manager, guild, member) == {"mage"}


async def test_cancelled_switch_keeps_old_main(character_setup, guild):
    manager, member, _ = character_setup
    john, sam = await add_pair(manager, guild, member)
    entered = asyncio.Event()
    original = member.add_roles.side_effect

    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        entered.set()
        await asyncio.Event().wait()

    member.add_roles.side_effect = interrupted
    task = asyncio.create_task(manager.promote(guild, member.id, sam.id))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await manager.records.roster(guild.id, member.id)).main.id == john.id
    assert await assigned(manager, guild, member) == {"paladin"}


async def test_owner_and_guild_isolation(
    character_setup, guild, settings, store, locks
):
    manager, member, _ = character_setup
    john, sam = await add_pair(manager, guild, member)
    other_user = guild.member(789)
    with pytest.raises(UserError, match="not in your roster"):
        await manager.promote(guild, other_user.id, sam.id)
    other_guild = FakeGuild(456)
    await RoleManager(settings, store, locks).reconcile(other_guild)
    same_user = other_guild.member(member.id)
    await manager.add(other_guild, same_user.id, "John", "Stormwind", "mage")
    assert await assigned(manager, guild, member) == {"paladin"}
    assert await assigned(manager, other_guild, same_user) == {"mage"}
    with pytest.raises(UserError, match="not in your roster"):
        await manager.remove(other_guild, same_user.id, john.id)


async def test_legacy_changes_blocked_after_first_character(
    character_setup, guild, settings, store, locks
):
    manager, member, _ = character_setup
    await add_pair(manager, guild, member)
    with pytest.raises(UserError, match="registered main"):
        await manager.members.change(guild, member.id, "mage")
    with pytest.raises(UserError, match="registered main"):
        await manager.members.change(guild, member.id, None)
    disabled = replace(settings, enable_character_management=False)
    await MemberManager(disabled, store, locks).change(guild, member.id, "mage")
    assert await assigned(manager, guild, member) == {"mage"}
    await manager.roster(guild, member.id)
    assert await assigned(manager, guild, member) == {"paladin"}


async def test_class_edits_and_limits_enforced(
    character_setup, guild, settings, store, locks
):
    manager, member, _ = character_setup
    john, _ = await add_pair(manager, guild, member)
    restricted = replace(
        settings, allow_character_class_edit=False, max_characters_per_user=2
    )
    manager = CharacterManager(
        restricted, store, locks, MemberManager(restricted, store, locks)
    )
    with pytest.raises(UserError, match="editing is disabled"):
        await manager.edit(guild, member.id, john.id, "John", "Ironforge", "warrior")
    await manager.edit(guild, member.id, john.id, "John", "Ironforge", "paladin")
    with pytest.raises(UserError, match="limit of 2"):
        await manager.add(guild, member.id, "Another", "Stormwind", "mage")


async def test_admin_erasure_preserves_roles_and_other_users(character_setup, guild):
    manager, member, _ = character_setup
    await add_pair(manager, guild, member)
    admin = guild.member(999)
    admin.guild_permissions = discord.Permissions(manage_guild=True, manage_roles=True)
    member.guild_permissions = discord.Permissions.none()
    await manager.add(guild, admin.id, "Other", "Player", "mage")
    with pytest.raises(UserError, match="Manage Server"):
        await manager.purge(guild, member, admin.id)
    guild.members.pop(member.id)
    await manager.purge(guild, admin, member.id)
    assert not (await manager.records.roster(guild.id, member.id)).characters
    assert len((await manager.roster(guild, admin.id)).characters) == 1
    assert await assigned(manager, guild, member) == {"paladin"}


async def test_roster_visibility_and_filters(
    character_setup, guild, settings, store, locks
):
    manager, member, _ = character_setup
    john, sam = await add_pair(manager, guild, member)
    member.guild_permissions = discord.Permissions.none()
    assert [
        item.id for item in await manager.guild_roster(guild, member, kind="mains")
    ] == [john.id]
    assert [
        item.id
        for item in await manager.guild_roster(
            guild, member, class_id="warrior", kind="alts"
        )
    ] == [sam.id]
    restricted = replace(settings, roster_visibility="admins")
    manager = CharacterManager(
        restricted, store, locks, MemberManager(restricted, store, locks)
    )
    with pytest.raises(UserError, match="Manage Server"):
        await manager.guild_roster(guild, member)
    assert len((await manager.roster(guild, member.id)).characters) == 2
    disabled = replace(settings, enable_guild_roster=False)
    manager = CharacterManager(
        disabled, store, locks, MemberManager(disabled, store, locks)
    )
    with pytest.raises(UserError, match="disabled"):
        await manager.guild_roster(guild, member)


async def test_allowlist_blocks_all_character_work(
    character_setup, guild, settings, store, locks
):
    _, member, _ = character_setup
    restricted = replace(settings, allowed_guild_ids=frozenset({999}))
    manager = CharacterManager(
        restricted, store, locks, MemberManager(restricted, store, locks)
    )
    for call in (
        manager.add(guild, member.id, "John", "Stormwind", "paladin"),
        manager.roster(guild, member.id),
        manager.promote(guild, member.id, 1),
    ):
        with pytest.raises(UserError, match="ALLOWED"):
            await call


async def test_crashed_first_registration_has_no_invented_character(
    character_setup, guild
):
    manager, member, _ = character_setup
    plan = await manager._role_plan(guild, member.id, "paladin")
    payload = {
        "kind": "add",
        "primary_name": "John",
        "secondary_name": "Stormwind",
        "name_key": normalized_name_key("John", "Stormwind"),
        "class_id": "paladin",
        "max_characters": 0,
    }
    await manager.records.prepare(
        guild.id, member.id, "first-crash", payload, plan.previous, plan.wanted
    )
    await manager.members.apply_locked(guild, member.id, plan, recover=False)
    assert not (await manager.roster(guild, member.id)).characters
    assert await assigned(manager, guild, member) == set()


async def test_first_registration_recovery_remaps_deleted_legacy_role(
    character_setup, guild, settings, store, locks, monkeypatch
):
    manager, member, unrelated = character_setup
    await manager.members.change(guild, member.id, "mage")
    original_commit = manager.records.commit
    original_restore = manager.members._restore
    monkeypatch.setattr(
        manager.records, "commit", AsyncMock(side_effect=StorageError("Write failed"))
    )
    monkeypatch.setattr(manager.members, "_restore", AsyncMock(return_value=False))
    with pytest.raises(UserError, match="Recovery is pending"):
        await manager.add(guild, member.id, "John", "Stormwind", "paladin")
    pending = await manager.records.pending(guild.id, member.id)
    mapping = await store.roles(guild.id)
    old_role = mapping["mage"]
    assert pending.payload["_previous_classes"] == {"mage": old_role}
    guild.roles = [role for role in guild.roles if role.id != old_role]
    member.roles = [role for role in member.roles if role.id != old_role]
    await RoleManager(settings, store, locks).reconcile(guild)
    assert (await store.roles(guild.id))["mage"] != old_role
    monkeypatch.setattr(manager.records, "commit", original_commit)
    monkeypatch.setattr(manager.members, "_restore", original_restore)
    assert not (await manager.roster(guild, member.id)).characters
    assert await assigned(manager, guild, member) == {"mage"}
    assert unrelated in member.roles
    assert await manager.records.pending(guild.id, member.id) is None
    await manager.add(guild, member.id, "John", "Stormwind", "paladin")
    assert (await manager.roster(guild, member.id)).main.full_name == "John Stormwind"


async def test_cancel_after_successful_commit_does_not_undo_new_main(
    character_setup, guild, monkeypatch
):
    manager, member, _ = character_setup
    _, sam = await add_pair(manager, guild, member)
    original = manager.records.commit

    async def cancelled_after_commit(*args):
        await original(*args)
        raise asyncio.CancelledError

    monkeypatch.setattr(manager.records, "commit", cancelled_after_commit)
    with pytest.raises(asyncio.CancelledError):
        await manager.promote(guild, member.id, sam.id)
    assert (await manager.roster(guild, member.id)).main.id == sam.id
    assert await assigned(manager, guild, member) == {"warrior"}


async def test_failed_replacement_does_not_delete_main(character_setup, guild):
    manager, member, unrelated = character_setup
    john, sam = await add_pair(manager, guild, member)
    member.add_roles.side_effect = http_error()
    with pytest.raises(UserError, match="could not be confirmed"):
        await manager.remove(guild, member.id, john.id, sam.id)
    roster = await manager.records.roster(guild.id, member.id)
    assert roster.main.id == john.id and len(roster.characters) == 2
    assert await assigned(manager, guild, member) == {"paladin"}
    assert unrelated in member.roles
