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
    RemovalConfirmation,
    RetryCharacterView,
)
from wow_class_manager.classes import GAMEPLAY_ROLES, REGISTRY, ClassRole
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


@pytest.mark.parametrize("saved_class", [None, "paladin"])
async def test_all_class_role_combinations_fit_one_menu_and_stay_grouped(
    character_controller, saved_class
):
    ui = character_controller
    ui.settings = replace(
        ui.settings,
        classes=tuple(
            ClassRole(entry, entry.name, entry.color)
            for entry in REGISTRY.values()
            if entry.id != saved_class
        ),
    )
    modal = CharacterModal(
        ui, 123, 456, character(class_id=saved_class) if saved_class else None
    )
    options = modal.class_role_select.options
    assert len(options) == 25
    assert {option.value for option in options} == {
        f"{class_id}:{role}"
        for class_id, allowed in CAPABILITIES.items()
        for role in allowed
    }
    grouped = list(dict.fromkeys(option.value.partition(":")[0] for option in options))
    assert grouped == [
        entry.id
        for entry in sorted(REGISTRY.values(), key=lambda entry: entry.name.casefold())
    ]
    assert all(
        option.emoji
        and len(option.label) <= 100
        and len(option.value) <= 100
        and len(option.description) <= 100
        for option in options
    )


@pytest.mark.parametrize("locked", [False, True])
async def test_combined_form_rechecks_class_policy_on_submission(
    character_controller, guild, locked
):
    ui = character_controller
    saved = character(main=True) if locked else None
    modal = CharacterModal(ui, guild.id, 456, saved)
    modal.primary._value, modal.secondary._value = "John", "Stormwind"
    modal.class_role_select._values = ["mage:dps"]
    if locked:
        ui.settings = replace(ui.settings, allow_character_class_edit=False)
    else:
        ui.settings = replace(
            ui.settings,
            classes=tuple(entry for entry in ui.settings.classes if entry.id != "mage"),
        )
    call = interaction(guild, guild.member())
    await modal.on_submit(call)
    ui.manager.add.assert_not_called()
    ui.manager.edit.assert_not_called()
    assert (
        "class editing is disabled" if locked else "enabled character class"
    ) in call.followup.send.call_args.kwargs["content"]


async def test_unknown_locked_legacy_class_has_actionable_error(character_controller):
    character_controller.settings = replace(
        character_controller.settings, allow_character_class_edit=False
    )
    with pytest.raises(
        UserError, match="Ask an admin to enable character class editing"
    ):
        CharacterModal(character_controller, 123, 456, character(class_id="unknown"))


@pytest.mark.parametrize("class_id,allowed", CAPABILITIES.items())
async def test_class_role_policy_and_locked_class_form_offer_only_supported_roles(
    character_controller, class_id, allowed
):
    assert set(REGISTRY) == set(CAPABILITIES)
    character_controller.settings = replace(
        character_controller.settings, allow_character_class_edit=False
    )
    modal = CharacterModal(character_controller, 123, 456, character(class_id=class_id))
    assert {option.value for option in modal.class_role_select.options} == {
        f"{class_id}:{role}" for role in allowed
    }
    assert modal.class_role_select.min_values == modal.class_role_select.max_values == 1
    assert not any(option.default for option in modal.class_role_select.options)
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


async def test_role_form_rechecks_owner_allowlist_and_submitted_role(
    character_controller, guild
):
    ui = character_controller
    modal = CharacterModal(ui, guild.id, 456)
    modal.primary._value, modal.secondary._value = "John", "Stormwind"
    modal.class_role_select._values = ["hunter:dps"]
    outsider = interaction(guild, guild.member(789))
    await modal.on_submit(outsider)
    ui.manager.add.assert_not_called()
    assert (
        "another member" in outsider.response.send_message.call_args.kwargs["content"]
    )
    ui.settings = replace(ui.settings, allowed_guild_ids=frozenset({999}))
    denied = interaction(guild, guild.member())
    await modal.on_submit(denied)
    ui.manager.add.assert_not_called()
    ui.settings = replace(ui.settings, allowed_guild_ids=frozenset())
    forged = interaction(guild, guild.member())
    modal.class_role_select._values = ["hunter:tank"]
    await modal.on_submit(forged)
    ui.manager.add.assert_not_called()
    result = forged.followup.send.call_args.kwargs
    assert "Hunter cannot select Tank" in result["content"]
    assert isinstance(result["view"], RetryCharacterView)
    assert result["view"].draft == ("John", "Stormwind", "hunter", "tank")


async def test_changed_class_revalidates_role_and_keeps_stale_edit_version(
    character_controller, guild
):
    ui = character_controller
    saved = character(class_id="priest", gameplay_role="healing", main=True)
    modal = CharacterModal(ui, guild.id, 456, saved)
    assert "hunter:healing" not in {
        option.value for option in modal.class_role_select.options
    }
    assert [
        option.value for option in modal.class_role_select.options if option.default
    ] == ["priest:healing"]
    modal.primary._value, modal.secondary._value = "John", "Stormwind"
    modal.class_role_select._values = ["hunter:healing"]
    submit = component(guild)
    submit.type = discord.InteractionType.modal_submit
    await modal.on_submit(submit)
    response = submit.edit_original_response.call_args.kwargs
    assert "Hunter cannot select Healing" in response["content"]
    retry = response["view"]
    assert retry.draft == ("John", "Stormwind", "hunter", "healing")
    ui.manager.edit.assert_not_called()
    click = component(guild)
    click.response.send_modal = AsyncMock()
    await retry.retry(click)
    corrected = click.response.send_modal.call_args.args[0]
    assert corrected.character.updated_at == saved.updated_at
    assert not any(option.default for option in corrected.class_role_select.options)
    corrected.primary._value, corrected.secondary._value = "John", "Stormwind"
    corrected.class_role_select._values = ["hunter:dps"]
    fixed = component(guild)
    fixed.type = discord.InteractionType.modal_submit
    await corrected.on_submit(fixed)
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
    await corrected.on_submit(component(guild))
    assert ui.manager.edit.await_count == 1


async def test_dismissed_role_retry_cannot_save(character_controller, guild):
    retry = RetryCharacterView(
        character_controller,
        guild.id,
        456,
        None,
        ("John", "Stormwind", "paladin", "tank"),
    )
    character_controller.track_view(700, retry)
    click = component(guild)
    click.response.send_modal = AsyncMock()
    await retry.retry(click)
    modal = click.response.send_modal.call_args.args[0]
    modal.primary._value, modal.secondary._value = "John", "Stormwind"
    modal.class_role_select._values = ["paladin:tank"]
    await retry.done(component(guild))
    submit = component(guild)
    submit.type = discord.InteractionType.modal_submit
    await modal.on_submit(submit)
    character_controller.manager.add.assert_not_called()
    assert "closed" in submit.followup.send.call_args.kwargs["content"]


@pytest.mark.parametrize(
    "choices,expected_pair,message",
    [
        (["priest:tank"], ("priest", "tank"), "Priest cannot select Tank"),
        (["hunter:tank"], ("hunter", "tank"), "Hunter cannot select Tank"),
        (["hunter:healing"], ("hunter", "healing"), "Hunter cannot select Healing"),
        ([], ("", None), "Choose a class and role"),
        (["paladin:unknown"], ("paladin", "unknown"), "Choose a gameplay role"),
        (["paladin:dps", "paladin:tank"], ("", None), "Choose a class and role"),
        (["paladin"], ("paladin", None), "Choose a class and role"),
        (["paladin:"], ("paladin", None), "Choose a class and role"),
        (["paladin:tank:extra"], ("paladin", "tank:extra"), "Choose a gameplay role"),
        (["unknown:dps"], ("unknown", "dps"), "recognized character class"),
    ],
)
async def test_invalid_modal_role_never_saves_and_keeps_draft(
    character_controller, guild, choices, expected_pair, message
):
    modal = CharacterModal(character_controller, guild.id, 456)
    modal.primary._value, modal.secondary._value = "John", "Stormwind"
    modal.class_role_select._values = choices
    call = interaction(guild, guild.member())
    await modal.on_submit(call)
    character_controller.manager.add.assert_not_called()
    character_controller.manager.edit.assert_not_called()
    result = call.followup.send.call_args.kwargs
    assert message in result["content"]
    assert result["view"].draft == (
        "John",
        "Stormwind",
        *expected_pair,
    )


async def test_concurrent_submissions_of_same_form_save_once(
    character_controller, guild
):
    ui = character_controller
    modal = CharacterModal(ui, guild.id, 456)
    modal.primary._value, modal.secondary._value = "John", "Stormwind"
    modal.class_role_select._values = ["paladin:tank"]
    started, release = asyncio.Event(), asyncio.Event()

    async def add(*args):
        started.set()
        await release.wait()
        return "Character registered."

    ui.manager.add.side_effect = add
    first = asyncio.create_task(modal.on_submit(interaction(guild, guild.member())))
    await asyncio.wait_for(started.wait(), 2)
    repeated = interaction(guild, guild.member())
    second = asyncio.create_task(modal.on_submit(repeated))
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 2)
    ui.manager.add.assert_awaited_once()
    assert "closed or changed" in repeated.followup.send.call_args.kwargs["content"]


@pytest.mark.parametrize("editing", [False, True])
async def test_save_failure_preserves_selected_role_and_original_edit_version(
    character_controller, guild, editing
):
    ui = character_controller
    original = character(gameplay_role="healing", main=True) if editing else None
    operation = ui.manager.edit if editing else ui.manager.add
    operation.side_effect = UserError("Roles could not be confirmed. Try again.")
    modal = CharacterModal(ui, guild.id, 456, original)
    modal.primary._value, modal.secondary._value = "John", "Ironforge"
    modal.class_role_select._values = ["paladin:tank"]
    submit = component(guild)
    submit.type = discord.InteractionType.modal_submit
    await modal.on_submit(submit)
    operation.assert_awaited_once()
    result = submit.edit_original_response.call_args.kwargs
    assert "could not be confirmed" in result["content"]
    retry = result["view"]
    assert retry.draft == ("John", "Ironforge", "paladin", "tank")
    click = component(guild)
    click.response.send_modal = AsyncMock()
    await retry.retry(click)
    reopened = click.response.send_modal.call_args.args[0]
    assert reopened.character is original
    assert [
        option.value for option in reopened.class_role_select.options if option.default
    ] == ["paladin:tank"]
    if editing:
        assert operation.call_args.kwargs["expected_updated_at"] == original.updated_at


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
