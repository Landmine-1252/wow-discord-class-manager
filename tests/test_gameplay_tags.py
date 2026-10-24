"""Free Discord role tags follow the committed main and share role recovery."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import FakeRole, http_error
from test_characters import character_setup as character_setup

from wow_class_manager.classes import GAMEPLAY_ROLE_TAGS
from wow_class_manager.diagnostics import inspect_guild, status
from wow_class_manager.members import MemberManager
from wow_class_manager.roles import RoleManager, UserError
from wow_class_manager.storage import StorageError


async def assert_roles(manager, guild, member, unrelated, class_id, gameplay_role):
    classes = await manager.store.roles(guild.id)
    tags = await manager.store.gameplay_roles(guild.id)
    expected = {unrelated.id}
    if class_id:
        expected.add(classes[class_id])
    if gameplay_role:
        expected.add(tags[gameplay_role])
    assert {role.id for role in member.roles} == expected
    member.edit.assert_not_called()
    for call in (*member.add_roles.call_args_list, *member.remove_roles.call_args_list):
        assert len(call.args) == 1 and call.kwargs["atomic"] is True


async def test_main_tags_follow_registration_edit_switch_and_removal(
    character_setup, guild
):
    manager, member, unrelated = character_setup
    manager.settings = replace(manager.settings, allow_clear_selection=True)
    await manager.add(guild, member.id, "John", "Stormwind", "priest", "healing")
    await assert_roles(manager, guild, member, unrelated, "priest", "healing")
    await manager.add(guild, member.id, "Sam", "Ironforge", "warrior", "tank")
    main, alt = (await manager.records.roster(guild.id, member.id)).characters
    await assert_roles(manager, guild, member, unrelated, "priest", "healing")
    await manager.edit(guild, member.id, alt.id, "Sam", "Ironforge", "warrior", "dps")
    await assert_roles(manager, guild, member, unrelated, "priest", "healing")
    await manager.promote(guild, member.id, alt.id)
    await assert_roles(manager, guild, member, unrelated, "warrior", "dps")
    await manager.edit(guild, member.id, alt.id, "Sam", "Ironforge", "warrior", "tank")
    await assert_roles(manager, guild, member, unrelated, "warrior", "tank")
    await manager.remove(guild, member.id, alt.id, replacement_id=main.id)
    await assert_roles(manager, guild, member, unrelated, "priest", "healing")
    await manager.remove(guild, member.id, main.id)
    await assert_roles(manager, guild, member, unrelated, None, None)
    assert not (await manager.records.roster(guild.id, member.id)).characters


async def test_same_class_main_switch_changes_only_gameplay_tag(character_setup, guild):
    manager, member, unrelated = character_setup
    await manager.add(guild, member.id, "John", "Stormwind", "paladin", "healing")
    await manager.add(guild, member.id, "Sam", "Ironforge", "paladin", "tank")
    alt = (await manager.records.roster(guild.id, member.id)).alts[0]
    member.add_roles.reset_mock()
    member.remove_roles.reset_mock()
    await manager.promote(guild, member.id, alt.id)
    tags = await manager.store.gameplay_roles(guild.id)
    assert [call.args[0].id for call in member.add_roles.call_args_list] == [
        tags["tank"]
    ]
    assert [call.args[0].id for call in member.remove_roles.call_args_list] == [
        tags["healing"]
    ]
    await assert_roles(manager, guild, member, unrelated, "paladin", "tank")


async def test_open_repairs_missing_and_duplicate_tags_without_touching_unrelated_roles(
    character_setup, guild
):
    manager, member, unrelated = character_setup
    await manager.add(guild, member.id, "John", "Stormwind", "priest", "healing")
    before = await manager.records.roster(guild.id, member.id)
    tags = await manager.store.gameplay_roles(guild.id)
    member.roles = [role for role in member.roles if role.id != tags["healing"]]
    member.roles.extend(
        role for role in guild.roles if role.id in {tags["tank"], tags["dps"]}
    )
    assert await manager.roster(guild, member.id) == before
    await assert_roles(manager, guild, member, unrelated, "priest", "healing")


async def test_legacy_class_selection_preserves_tags_when_character_management_disabled(
    character_setup, guild
):
    manager, member, unrelated = character_setup
    await manager.add(guild, member.id, "John", "Stormwind", "priest", "healing")
    disabled = replace(manager.settings, enable_character_management=False)
    legacy = MemberManager(disabled, manager.store, manager.locks)
    await legacy.change(guild, member.id, "mage")
    await assert_roles(manager, guild, member, unrelated, "mage", "healing")
    member.add_roles.reset_mock()
    member.remove_roles.reset_mock()
    await RoleManager(disabled, manager.store, manager.locks).reconcile(guild)
    member.add_roles.assert_not_called()
    member.remove_roles.assert_not_called()


@pytest.mark.parametrize(
    "step,ambiguous",
    [("add", False), ("add", True), ("remove", False), ("remove", True)],
)
async def test_partial_tag_failure_restores_both_roles_and_old_roster(
    character_setup, guild, step, ambiguous
):
    manager, member, unrelated = character_setup
    await manager.add(guild, member.id, "John", "Stormwind", "priest", "healing")
    before = await manager.records.roster(guild.id, member.id)
    tags = await manager.store.gameplay_roles(guild.id)
    method = member.add_roles if step == "add" else member.remove_roles
    original = method.side_effect
    failed = False
    tag_id = tags["tank" if step == "add" else "healing"]

    async def fail_once(role, **kwargs):
        nonlocal failed
        if role.id == tag_id and not failed:
            failed = True
            if ambiguous:
                await original(role, **kwargs)
            raise http_error()
        await original(role, **kwargs)

    method.side_effect = fail_once
    with pytest.raises(UserError, match="prior character roles were restored"):
        await manager.edit(
            guild, member.id, before.main.id, "John", "Stormwind", "warrior", "tank"
        )
    assert failed
    assert await manager.records.roster(guild.id, member.id) == before
    assert await manager.records.pending(guild.id, member.id) is None
    await assert_roles(manager, guild, member, unrelated, "priest", "healing")


async def test_journal_contains_both_role_kinds_before_discord_writes(
    character_setup, guild, monkeypatch
):
    manager, member, unrelated = character_setup
    original_add = member.add_roles.side_effect
    seen = []

    async def inspect_write(role, **kwargs):
        assert not manager.store.db.in_transaction
        pending = await manager.records.pending(guild.id, member.id)
        assert pending is not None
        assert pending.payload["_wanted_classes"] == {
            "paladin": (await manager.store.roles(guild.id))["paladin"]
        }
        assert pending.payload["_wanted_gameplay"] == {
            "tank": (await manager.store.gameplay_roles(guild.id))["tank"]
        }
        assert not (await manager.records.roster(guild.id, member.id)).characters
        seen.append(role.id)
        await original_add(role, **kwargs)

    member.add_roles.side_effect = inspect_write
    await manager.add(guild, member.id, "John", "Stormwind", "paladin", "tank")
    assert len(seen) == 2
    await assert_roles(manager, guild, member, unrelated, "paladin", "tank")


@pytest.mark.parametrize(
    "unsafe", ["permission", "managed", "hierarchy", "channel", "color"]
)
async def test_tag_safety_is_refreshed_after_journal_before_assignment(
    character_setup, guild, monkeypatch, unsafe
):
    manager, member, unrelated = character_setup
    await manager.add(guild, member.id, "John", "Stormwind", "paladin", "healing")
    before = await manager.records.roster(guild.id, member.id)
    tags = await manager.store.gameplay_roles(guild.id)
    target = next(role for role in guild.roles if role.id == tags["tank"])
    prepare = manager.records.prepare

    async def changed_after_prepare(*args):
        await prepare(*args)
        if unsafe == "permission":
            target.permissions = discord.Permissions(manage_roles=True)
        elif unsafe == "managed":
            target.managed = True
        elif unsafe == "hierarchy":
            target.position = 101
        elif unsafe == "color":
            target.colour = discord.Colour(0x123456)
        else:
            channel = guild.channel()
            channel.overwrites_for.side_effect = lambda role: (
                discord.PermissionOverwrite(view_channel=True)
                if role.id == target.id
                else discord.PermissionOverwrite()
            )

    monkeypatch.setattr(manager.records, "prepare", changed_after_prepare)
    member.add_roles.reset_mock()
    with pytest.raises(UserError, match="could not be confirmed"):
        await manager.edit(
            guild, member.id, before.main.id, "John", "Stormwind", "paladin", "tank"
        )
    member.add_roles.assert_not_called()
    assert await manager.records.roster(guild.id, member.id) == before
    await assert_roles(manager, guild, member, unrelated, "paladin", "healing")


async def test_failed_commit_retains_tag_recovery_until_recreated_role_is_restored(
    character_setup, guild, monkeypatch
):
    manager, member, unrelated = character_setup
    await manager.add(guild, member.id, "John", "Stormwind", "priest", "healing")
    before = await manager.records.roster(guild.id, member.id)
    tags = await manager.store.gameplay_roles(guild.id)
    commit, restore = manager.records.commit, manager.members._restore
    monkeypatch.setattr(
        manager.records, "commit", AsyncMock(side_effect=StorageError("write failed"))
    )
    monkeypatch.setattr(manager.members, "_restore", AsyncMock(return_value=False))
    with pytest.raises(UserError, match="Recovery is pending"):
        await manager.edit(
            guild, member.id, before.main.id, "John", "Stormwind", "warrior", "tank"
        )
    pending = await manager.records.pending(guild.id, member.id)
    assert pending.payload["_previous_gameplay"] == {"healing": tags["healing"]}
    assert pending.payload["_wanted_gameplay"] == {"tank": tags["tank"]}
    assert await manager.records.roster(guild.id, member.id) == before
    guild.roles = [role for role in guild.roles if role.id != tags["healing"]]
    member.roles = [role for role in member.roles if role.id != tags["healing"]]
    await RoleManager(manager.settings, manager.store, manager.locks).reconcile(guild)
    assert (await manager.store.gameplay_roles(guild.id))["healing"] != tags["healing"]
    monkeypatch.setattr(manager.records, "commit", commit)
    monkeypatch.setattr(manager.members, "_restore", restore)
    assert await manager.roster(guild, member.id) == before
    assert await manager.records.pending(guild.id, member.id) is None
    await assert_roles(manager, guild, member, unrelated, "priest", "healing")


async def test_first_registration_recovery_remaps_tag_snapshots_without_a_main(
    character_setup, guild, monkeypatch
):
    manager, member, unrelated = character_setup
    tags = await manager.store.gameplay_roles(guild.id)
    member.roles.append(
        next(role for role in guild.roles if role.id == tags["healing"])
    )
    commit, restore = manager.records.commit, manager.members._restore
    monkeypatch.setattr(
        manager.records, "commit", AsyncMock(side_effect=StorageError("write failed"))
    )
    monkeypatch.setattr(manager.members, "_restore", AsyncMock(return_value=False))
    with pytest.raises(UserError, match="Recovery is pending"):
        await manager.add(guild, member.id, "John", "Stormwind", "warrior", "tank")
    guild.roles.append(FakeRole(10000, "Other server role"))
    guild.roles = [role for role in guild.roles if role.id not in tags.values()]
    member.roles = [role for role in member.roles if role.id not in tags.values()]
    await RoleManager(manager.settings, manager.store, manager.locks).reconcile(guild)
    assert set((await manager.store.gameplay_roles(guild.id)).values()).isdisjoint(
        tags.values()
    )
    monkeypatch.setattr(manager.records, "commit", commit)
    monkeypatch.setattr(manager.members, "_restore", restore)
    assert not (await manager.roster(guild, member.id)).characters
    assert await manager.records.pending(guild.id, member.id) is None
    await assert_roles(manager, guild, member, unrelated, None, "healing")


async def test_cancellation_after_tag_add_restores_original_main(
    character_setup, guild
):
    manager, member, unrelated = character_setup
    await manager.add(guild, member.id, "John", "Stormwind", "priest", "healing")
    before = await manager.records.roster(guild.id, member.id)
    tags = await manager.store.gameplay_roles(guild.id)
    entered = asyncio.Event()
    add = member.add_roles.side_effect

    async def wait_after_add(role, **kwargs):
        await add(role, **kwargs)
        if role.id == tags["tank"]:
            entered.set()
            await asyncio.Event().wait()

    member.add_roles.side_effect = wait_after_add
    task = asyncio.create_task(
        manager.edit(
            guild, member.id, before.main.id, "John", "Stormwind", "warrior", "tank"
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await manager.records.roster(guild.id, member.id) == before
    assert await manager.records.pending(guild.id, member.id) is None
    await assert_roles(manager, guild, member, unrelated, "priest", "healing")


async def test_existing_tags_are_reused_by_id_and_keep_no_color(
    settings, store, locks, guild
):
    existing = FakeRole(50, GAMEPLAY_ROLE_TAGS["tank"], colour=discord.Colour(0xFF0000))
    guild.roles.append(existing)
    await RoleManager(settings, store, locks).reconcile(guild)
    tags = await store.gameplay_roles(guild.id)
    assert tags["tank"] == 50 and existing.colour.value == 0
    existing.name = "Renamed tag"
    result = await RoleManager(settings, store, locks).reconcile(guild)
    assert result.created == 0
    assert await store.gameplay_roles(guild.id) == tags
    assert existing.name == GAMEPLAY_ROLE_TAGS["tank"]
    for call in guild.create_role.call_args_list:
        assert "icon" not in call.kwargs and "unicode_emoji" not in call.kwargs
    assert "no boosts needed" in await status(settings, store, guild)


@pytest.mark.parametrize("problem", ["permission", "managed", "hierarchy", "duplicate"])
async def test_unsafe_or_ambiguous_tag_is_not_adopted(
    settings, store, locks, guild, problem
):
    role = FakeRole(50, GAMEPLAY_ROLE_TAGS["tank"])
    guild.roles.append(role)
    if problem == "permission":
        role.permissions = discord.Permissions(manage_roles=True)
    elif problem == "managed":
        role.managed = True
    elif problem == "hierarchy":
        role.position = 101
    else:
        guild.roles.append(FakeRole(51, role.name))
    result = await RoleManager(settings, store, locks).reconcile(guild)
    assert result.warnings and not role.edits
    assert "tank" not in await store.gameplay_roles(guild.id)
    check = await inspect_guild(settings, store, guild)
    assert check.gameplay_associated == check.gameplay_manageable == 2
    assert any(GAMEPLAY_ROLE_TAGS["tank"] in warning for warning in check.warnings)


async def test_class_name_override_cannot_collide_with_gameplay_tag(
    settings, store, locks, guild
):
    entries = list(settings.classes)
    entries[0] = replace(entries[0], name=GAMEPLAY_ROLE_TAGS["tank"])
    custom = replace(settings, classes=tuple(entries))
    result = await RoleManager(custom, store, locks).reconcile(guild)
    assert len(result.warnings) == 2
    assert entries[0].id not in await store.roles(guild.id)
    assert "tank" not in await store.gameplay_roles(guild.id)


async def test_no_tags_created_for_class_only_installation(
    settings, store, locks, guild
):
    disabled = replace(settings, enable_character_management=False)
    result = await RoleManager(disabled, store, locks).reconcile(guild)
    assert result.created == 9 and not result.warnings
    assert await store.gameplay_roles(guild.id) == {}
    assert not (await inspect_guild(disabled, store, guild)).warnings


async def test_unconfirmed_tag_never_commits_the_new_main_details(
    character_setup, guild
):
    manager, member, unrelated = character_setup
    await manager.add(guild, member.id, "John", "Stormwind", "paladin", "healing")
    before = await manager.records.roster(guild.id, member.id)
    tags = await manager.store.gameplay_roles(guild.id)
    add = member.add_roles.side_effect

    async def ignored_tag(role, **kwargs):
        if role.id != tags["tank"]:
            await add(role, **kwargs)

    member.add_roles.side_effect = ignored_tag
    with pytest.raises(UserError, match="did not confirm"):
        await manager.edit(
            guild, member.id, before.main.id, "John", "Ironforge", "paladin", "tank"
        )
    assert await manager.records.roster(guild.id, member.id) == before
    await assert_roles(manager, guild, member, unrelated, "paladin", "healing")


async def test_concurrent_role_edits_leave_tag_matching_the_committed_main(
    character_setup, guild
):
    manager, member, unrelated = character_setup
    await manager.add(guild, member.id, "John", "Stormwind", "paladin", "dps")
    before = (await manager.records.roster(guild.id, member.id)).main
    results = await asyncio.gather(
        *(
            manager.edit(
                guild,
                member.id,
                before.id,
                "John",
                "Stormwind",
                "paladin",
                role,
                expected_updated_at=before.updated_at,
            )
            for role in ("healing", "tank")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, UserError) for result in results) == 1
    current = (await manager.records.roster(guild.id, member.id)).main
    await assert_roles(
        manager, guild, member, unrelated, "paladin", current.gameplay_role
    )


async def test_unset_legacy_role_gets_no_tag(character_setup, guild):
    manager, member, unrelated = character_setup
    await manager.add(guild, member.id, "John", "Stormwind", "priest", "healing")

    def make_legacy():
        with manager.store.db:
            manager.store.db.execute(
                "UPDATE characters SET gameplay_role=NULL WHERE guild_id=? AND user_id=?",
                (guild.id, member.id),
            )

    await manager.store._run(make_legacy)
    roster = await manager.roster(guild, member.id)
    assert roster.main.gameplay_role is None
    await assert_roles(manager, guild, member, unrelated, "priest", None)
