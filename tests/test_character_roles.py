"""Class capability, persistence, recovery and private role-selection regressions."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import interaction
from test_character_interface import character, roster
from test_character_interface import character_controller as character_controller
from test_character_navigation import component
from test_character_storage import change, named
from test_characters import assigned
from test_characters import character_setup as character_setup

from wow_class_manager.character_roles import validate_gameplay_role
from wow_class_manager.character_store import CharacterStore
from wow_class_manager.character_ui import (
    CharacterModal,
    CharacterRolePicker,
    RemovalConfirmation,
    RetryCharacterView,
)
from wow_class_manager.classes import GAMEPLAY_ROLES, REGISTRY
from wow_class_manager.roles import UserError
from wow_class_manager.storage import StorageError

CAPABILITIES = {
    "warrior": {"dps", "tank"},
    "paladin": {"dps", "healing", "tank"},
    "hunter": {"dps"},
    "rogue": {"dps"},
    "priest": {"dps", "healing"},
    "shaman": {"dps", "healing"},
    "mage": {"dps"},
    "warlock": {"dps"},
    "druid": {"dps", "healing", "tank"},
    "death_knight": {"dps", "tank"},
    "monk": {"dps", "healing", "tank"},
    "demon_hunter": {"dps", "tank"},
    "evoker": {"dps", "healing"},
}


@pytest.mark.parametrize("class_id,allowed", CAPABILITIES.items())
async def test_class_role_policy_and_picker_offer_only_supported_roles(
    character_controller, class_id, allowed
):
    assert set(REGISTRY) == set(CAPABILITIES)
    picker = CharacterRolePicker(
        character_controller, 123, 456, None, ("John", "Stormwind", class_id)
    )
    assert {option.value for option in picker.select.options} == allowed
    assert picker.select.min_values == picker.select.max_values == 1
    for role in (*GAMEPLAY_ROLES, None, "unknown"):
        if role in allowed:
            validate_gameplay_role(class_id, role)
        else:
            with pytest.raises(UserError, match="Choose"):
                validate_gameplay_role(class_id, role)


@pytest.mark.parametrize("class_id,allowed", CAPABILITIES.items())
async def test_storage_rechecks_class_role_pair_before_prepare_and_in_sql(
    store, class_id, allowed
):
    records = CharacterStore(store)
    for index, role in enumerate(GAMEPLAY_ROLES):
        payload = named(class_id=class_id, gameplay_role=role)
        if role in allowed:
            saved = await change(records, payload, user_id=456 + index)
            assert saved.main.gameplay_role == role
        else:
            with pytest.raises(UserError, match="cannot select"):
                await change(records, payload, user_id=456 + index)
            assert not (await records.roster(123, 456 + index)).characters
            assert await records.pending(123, 456 + index) is None

    saved = await records.roster(123, 456)
    for invalid in [None, "unknown"]:
        with pytest.raises(UserError, match="Choose a gameplay role"):
            await change(records, named(gameplay_role=invalid), user_id=789)
    for invalid in set(GAMEPLAY_ROLES) - allowed | {"unknown"}:

        def corrupt(role=invalid):
            with store.db:
                store.db.execute(
                    "UPDATE characters SET gameplay_role=? WHERE id=?",
                    (role, saved.main.id),
                )

        with pytest.raises(StorageError):
            await store._run(corrupt)
    assert await records.roster(123, 456) == saved


@pytest.mark.parametrize(
    "class_id,role", [("priest", "tank"), ("hunter", "tank"), ("hunter", "healing")]
)
async def test_invalid_registration_never_writes_discord_or_character_data(
    character_setup, guild, class_id, role
):
    manager, member, _ = character_setup
    guild.fetch_member.reset_mock()
    with pytest.raises(UserError, match="cannot select"):
        await manager.add(guild, member.id, "John", "Stormwind", class_id, role)
    guild.fetch_member.assert_not_called()
    member.add_roles.assert_not_called()
    member.remove_roles.assert_not_called()
    assert not (await manager.records.roster(guild.id, member.id)).characters
    assert await manager.records.pending(guild.id, member.id) is None


async def test_role_edit_and_class_change_preserve_ids_and_unrelated_roles(
    character_setup, guild
):
    manager, member, unrelated = character_setup
    await manager.add(guild, member.id, "John", "Stormwind", "priest", "healing")
    await manager.add(guild, member.id, "Sam", "Ironforge", "hunter", "dps")
    before = await manager.records.roster(guild.id, member.id)
    john = before.main
    member.add_roles.reset_mock()
    member.remove_roles.reset_mock()
    with pytest.raises(UserError, match="cannot select Healing"):
        await manager.edit(
            guild, member.id, john.id, "John", "Stormwind", "warrior", "healing"
        )
    assert await manager.records.roster(guild.id, member.id) == before
    await manager.edit(guild, member.id, john.id, "John", "Stormwind", "priest", "dps")
    tags = await manager.store.gameplay_roles(guild.id)
    assert [call.args[0].id for call in member.add_roles.call_args_list] == [
        tags["dps"]
    ]
    assert [call.args[0].id for call in member.remove_roles.call_args_list] == [
        tags["healing"]
    ]
    await manager.edit(
        guild, member.id, john.id, "John", "Stormwind", "warrior", "tank"
    )
    after = await manager.records.roster(guild.id, member.id)
    assert after.main.id == john.id and after.main.gameplay_role == "tank"
    assert after.alts == before.alts
    assert await assigned(manager, guild, member) == {"warrior"}
    assert unrelated in member.roles
    assert await manager.records.pending(guild.id, member.id) is None


async def test_concurrent_role_edits_reject_stale_form(character_setup, guild):
    manager, member, _ = character_setup
    await manager.add(guild, member.id, "John", "Stormwind", "paladin", "dps")
    original = (await manager.records.roster(guild.id, member.id)).main
    results = await asyncio.gather(
        *(
            manager.edit(
                guild,
                member.id,
                original.id,
                "John",
                "Stormwind",
                "paladin",
                role,
                expected_updated_at=original.updated_at,
            )
            for role in ("healing", "tank")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, UserError) for result in results) == 1
    assert any("changed while" in str(result) for result in results)
    current = (await manager.records.roster(guild.id, member.id)).main
    assert current.gameplay_role in {"healing", "tank"} and current.id == original.id


async def test_failed_class_and_role_edit_keeps_old_role_through_recovery(
    character_setup, guild, monkeypatch
):
    manager, member, unrelated = character_setup
    await manager.add(guild, member.id, "John", "Stormwind", "priest", "healing")
    original = (await manager.records.roster(guild.id, member.id)).main
    commit = manager.records.commit
    monkeypatch.setattr(
        manager.records, "commit", AsyncMock(side_effect=StorageError("write failed"))
    )
    monkeypatch.setattr(manager.members, "_restore", AsyncMock(return_value=False))
    with pytest.raises(UserError, match="Recovery is pending"):
        await manager.edit(
            guild, member.id, original.id, "John", "Stormwind", "warrior", "tank"
        )
    pending = await manager.records.pending(guild.id, member.id)
    assert pending.payload["gameplay_role"] == "tank"
    assert (await manager.records.roster(guild.id, member.id)).main == original
    monkeypatch.setattr(manager.records, "commit", commit)
    recovered = await manager.roster(guild, member.id)
    assert recovered.main == original and recovered.main.gameplay_role == "healing"
    assert await assigned(manager, guild, member) == {"priest"}
    assert unrelated in member.roles
    assert await manager.records.pending(guild.id, member.id) is None


async def test_role_can_change_when_class_editing_is_disabled(character_setup, guild):
    manager, member, _ = character_setup
    await manager.add(guild, member.id, "John", "Stormwind", "priest", "healing")
    original = (await manager.records.roster(guild.id, member.id)).main
    manager.settings = replace(manager.settings, allow_character_class_edit=False)
    await manager.edit(
        guild, member.id, original.id, "John", "Stormwind", "priest", "dps"
    )
    assert (
        await manager.records.roster(guild.id, member.id)
    ).main.gameplay_role == "dps"


async def test_role_picker_rechecks_owner_allowlist_and_submitted_role(
    character_controller, guild
):
    ui = character_controller
    picker = CharacterRolePicker(
        ui, guild.id, 456, None, ("John", "Stormwind", "hunter")
    )
    outsider = interaction(guild, guild.member(789))
    outsider.data = {"values": ["dps"]}
    await picker.selected(outsider)
    ui.manager.add.assert_not_called()
    assert (
        "another member" in outsider.response.send_message.call_args.kwargs["content"]
    )
    ui.settings = replace(ui.settings, allowed_guild_ids=frozenset({999}))
    denied = interaction(guild, guild.member())
    denied.data = {"values": ["dps"]}
    await picker.selected(denied)
    ui.manager.add.assert_not_called()
    ui.settings = replace(ui.settings, allowed_guild_ids=frozenset())
    forged = interaction(guild, guild.member())
    forged.data = {"values": ["tank"]}
    await picker.selected(forged)
    ui.manager.add.assert_not_called()
    result = forged.followup.send.call_args.kwargs
    assert "Hunter cannot select Tank" in result["content"]
    assert isinstance(result["view"], RetryCharacterView)
    assert result["view"].draft == ("John", "Stormwind", "hunter")


async def test_changed_class_filters_role_choices_and_keeps_stale_edit_version(
    character_controller, guild
):
    ui = character_controller
    saved = character(class_id="priest", gameplay_role="healing", main=True)
    modal = CharacterModal(ui, guild.id, 456, saved)
    modal.primary._value, modal.secondary._value = "John", "Stormwind"
    modal.class_select._values = ["hunter"]
    submit = component(guild)
    submit.type = discord.InteractionType.modal_submit
    await modal.on_submit(submit)
    picker = submit.edit_original_response.call_args.kwargs["view"]
    assert [option.value for option in picker.select.options] == ["dps"]
    ui.manager.edit.assert_not_called()
    choose = component(guild)
    choose.data = {"values": ["dps"]}
    await picker.selected(choose)
    ui.manager.edit.assert_awaited_once_with(
        guild,
        456,
        saved.id,
        "John",
        "Stormwind",
        "hunter",
        "dps",
        expected_updated_at=saved.updated_at,
    )
    await picker.selected(choose)
    assert ui.manager.edit.await_count == 1


async def test_dismissed_role_draft_cannot_save(character_controller, guild):
    picker = CharacterRolePicker(
        character_controller, guild.id, 456, None, ("John", "Stormwind", "paladin")
    )
    character_controller.track_view(700, picker)
    await picker.done(component(guild))
    choose = component(guild)
    choose.data = {"values": ["tank"]}
    await picker.selected(choose)
    character_controller.manager.add.assert_not_called()


async def test_saved_and_unset_roles_appear_on_personal_and_community_rosters(
    character_controller, guild
):
    entries = [
        character(class_id="priest", gameplay_role="healing", main=True),
        character(2, class_id="hunter", gameplay_role="dps"),
        character(3, class_id="warrior", gameplay_role="tank"),
        character(4),
    ]
    ui = character_controller
    ui.manager.roster.return_value = roster(*entries)
    ui.manager.guild_roster.return_value = entries
    for display in (ui.open, ui.guild_roster):
        call = interaction(guild, guild.member())
        await display(call)
        description = call.followup.send.call_args.kwargs["embed"].description
        for label in ("Healing", "DPS", "Tank", "Role not set"):
            assert label in description


async def test_direct_class_update_cannot_keep_incompatible_role(store):
    records = CharacterStore(store)
    saved = await change(records, named(class_id="priest", gameplay_role="healing"))

    def invalid_class_edit():
        with store.db:
            store.db.execute(
                "UPDATE characters SET class_id='hunter' WHERE id=?", (saved.main.id,)
            )

    with pytest.raises(StorageError):
        await store._run(invalid_class_edit)
    assert await records.roster(123, 456) == saved


async def test_removal_confirmation_rejects_changed_gameplay_role(
    character_controller, guild
):
    original = character(gameplay_role="healing")
    confirmation = RemovalConfirmation(
        character_controller, guild.id, 456, original, None
    )
    character_controller.manager.roster.return_value = roster(
        character(gameplay_role="tank")
    )
    call = interaction(guild, guild.member())
    await confirmation.confirm(call)
    character_controller.manager.remove.assert_not_called()
    assert "changed" in call.followup.send.call_args.kwargs["content"]
