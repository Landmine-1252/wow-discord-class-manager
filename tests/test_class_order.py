from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import interaction

from wow_class_manager.bot import ClassBot
from wow_class_manager.character_commands import build_roster_command
from wow_class_manager.character_ui import CharacterModal
from wow_class_manager.classes import ALPHABETICAL_CLASSES, REGISTRY
from wow_class_manager.commands import build_commands
from wow_class_manager.config import Settings
from wow_class_manager.logging_setup import Redactor
from wow_class_manager.roster_ui import GuildRosterBrowser
from wow_class_manager.ui import ClassSelect

CLASSIC_NAMES = [
    "Druid",
    "Hunter",
    "Mage",
    "Paladin",
    "Priest",
    "Rogue",
    "Shaman",
    "Warlock",
    "Warrior",
]


@pytest.fixture
async def controller(settings, store):
    bot = ClassBot(settings, store, Redactor())
    yield bot.controller
    await bot.close()


def test_display_order_preserves_configuration_and_uses_canonical_names(env):
    settings = Settings.from_env(
        {
            **env,
            "ENABLED_CLASSES": "warrior,mage,druid,death_knight",
            "ROLE_NAME_MAGE": "A Wizard",
            "ROLE_NAME_DRUID": "Z Druid",
        }
    )
    assert [entry.id for entry in settings.classes] == [
        "warrior",
        "mage",
        "druid",
        "death_knight",
    ]
    assert [entry.id for entry in settings.display_classes] == [
        "death_knight",
        "druid",
        "mage",
        "warrior",
    ]
    assert settings.display_classes[2].name == "A Wizard"
    assert [entry.name for entry in ALPHABETICAL_CLASSES] == sorted(
        entry.name for entry in REGISTRY.values()
    )


async def test_legacy_dropdown_and_slash_choices_use_same_alphabetical_order(
    controller,
):
    dropdown = ClassSelect(controller)
    command = build_commands(controller).get_command("set")
    assert [option.label for option in dropdown.options] == CLASSIC_NAMES
    assert [choice.name for choice in command.parameters[0].choices] == CLASSIC_NAMES


async def test_character_modal_only_offers_enabled_classes_when_editing_legacy_class(
    controller,
):
    ui = controller.characters_ui
    new = CharacterModal(ui, 123, 456)
    assert [option.label for option in new.class_select.options] == CLASSIC_NAMES
    saved = SimpleNamespace(
        id=1,
        class_id="death_knight",
        primary_name="John",
        secondary_name="Stormwind",
        needs_completion=False,
    )
    edit = CharacterModal(ui, 123, 456, character=saved)
    assert [option.label for option in edit.class_select.options] == CLASSIC_NAMES
    assert not any(option.default for option in edit.class_select.options)
    assert not edit.class_select.required
    assert edit.class_select.min_values == 0


async def test_guild_roster_dropdown_and_command_alphabetize_enabled_classes(
    controller,
):
    ui = controller.characters_ui
    browser = GuildRosterBrowser(ui, 123, 456)
    expected = CLASSIC_NAMES
    assert [option.label for option in browser.class_filter.options] == [
        "All classes",
        *expected,
    ]
    command = build_roster_command(ui)
    parameter = next(item for item in command.parameters if item.name == "wow_class")
    assert [choice.name for choice in parameter.choices] == expected


@pytest.mark.parametrize("selection", [None, "mage"])
async def test_edit_disabled_main_preserves_record_and_only_changes_class_if_selected(
    settings, store, guild, selection
):
    original = ClassBot(settings, store, Redactor())
    await original.roles.reconcile(guild)
    member = guild.member()
    await original.characters.add(
        guild, member.id, "John", "Stormwind", "paladin", "healing"
    )
    saved = (await original.characters.records.roster(guild.id, member.id)).main
    restricted = replace(
        settings,
        classes=tuple(entry for entry in settings.classes if entry.id != "paladin"),
    )
    current = ClassBot(restricted, store, Redactor())
    try:
        member.add_roles.reset_mock()
        member.remove_roles.reset_mock()
        modal = CharacterModal(
            current.controller.characters_ui, guild.id, member.id, saved
        )
        assert "paladin" not in {option.value for option in modal.class_select.options}
        modal.primary._value, modal.secondary._value = "John", "Ironforge"
        modal.class_select._values = [selection] if selection else []
        call = interaction(guild, member)
        await modal.on_submit(call)
        picker = call.followup.send.call_args.kwargs["view"]
        selected = interaction(guild, member)
        selected.data = {"values": ["dps" if selection else "tank"]}
        await picker.selected(selected)
        result = await current.characters.records.roster(guild.id, member.id)
        assert result.main.id == saved.id
        assert result.main.full_name == "John Ironforge"
        assert result.main.class_id == (selection or "paladin")
        assert result.main.gameplay_role == ("dps" if selection else "tank")
        mapping = await store.roles(guild.id)
        assert {role.id for role in member.roles} == {mapping[selection or "paladin"]}
        assert "has been updated" in selected.followup.send.call_args.kwargs["content"]
        if selection is None:
            member.add_roles.assert_not_awaited()
            member.remove_roles.assert_not_awaited()
    finally:
        await current.close()
        await original.close()
