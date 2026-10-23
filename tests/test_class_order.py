from types import SimpleNamespace

import pytest

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


async def test_character_modal_orders_classes_and_preserves_selected_disabled_class(
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
    assert [option.label for option in edit.class_select.options] == [
        "Death Knight (disabled)",
        *CLASSIC_NAMES,
    ]
    assert [option.value for option in edit.class_select.options if option.default] == [
        "death_knight"
    ]


async def test_guild_roster_dropdown_and_command_alphabetize_all_classes(controller):
    ui = controller.characters_ui
    browser = GuildRosterBrowser(ui, 123, 456)
    expected = [entry.name for entry in ALPHABETICAL_CLASSES]
    assert [option.label for option in browser.class_filter.options] == [
        "All classes",
        *expected,
    ]
    command = build_roster_command(ui)
    parameter = next(item for item in command.parameters if item.name == "wow_class")
    assert [choice.name for choice in parameter.choices] == expected
