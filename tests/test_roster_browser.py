import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import interaction

from wow_class_manager.character_commands import build_roster_command
from wow_class_manager.character_ui import PAGE_SIZE
from wow_class_manager.commands import Controller
from wow_class_manager.config import Settings
from wow_class_manager.logging_setup import Redactor
from wow_class_manager.roles import UserError
from wow_class_manager.roster_ui import GuildRosterBrowser, show_guild_roster


def character(number, *, main=False, user_id=456):
    return SimpleNamespace(
        id=number,
        user_id=user_id,
        guild_id=123,
        full_name=f"John Stormwind{number}",
        class_id="paladin",
        is_main=main,
    )


@pytest.fixture
def browser_controller(settings, store, locks):
    bot = SimpleNamespace(
        settings=settings,
        store=store,
        locks=locks,
        characters=SimpleNamespace(guild_roster=AsyncMock(return_value=[])),
        user=SimpleNamespace(id=900),
        redact=Redactor(),
        notifier=SimpleNamespace(notify=lambda *args: None),
    )
    ui = Controller(bot).characters_ui
    ui.send = AsyncMock()
    ui.open = AsyncMock()

    async def display(call, **filters):
        async def render(guild):
            await show_guild_roster(ui, call, **filters)

        await ui.execute(call, render)

    # Exercise the delegated rendering contract independently of root UI edits.
    ui.guild_roster = AsyncMock(side_effect=display)
    return ui


def view(controller, **filters):
    return GuildRosterBrowser(controller, 123, 456, **filters)


def button(browser, label):
    return next(
        item for item in browser.children if getattr(item, "label", None) == label
    )


async def test_browser_native_filters_only_include_enabled_classes_and_selected_defaults(
    browser_controller,
):
    browser = view(browser_controller, class_id="paladin", kind="alts", user_id=789)
    assert {option.value for option in browser.class_filter.options} == {
        "all",
        *(entry.id for entry in browser_controller.settings.classes),
    }
    assert len(browser.class_filter.options) == 10
    assert "monk" not in {option.value for option in browser.class_filter.options}
    assert "death_knight" not in {
        option.value for option in browser.class_filter.options
    }
    assert (
        next(option for option in browser.class_filter.options if option.default).value
        == "paladin"
    )
    assert (
        next(option for option in browser.kind_filter.options if option.default).value
        == "alts"
    )
    assert isinstance(browser.member_filter, discord.ui.UserSelect)
    assert browser.member_filter.min_values == 0
    assert browser.member_filter.max_values == 1
    assert browser.member_filter.default_values[0].id == 789
    assert (
        browser.member_filter.default_values[0].type
        is discord.SelectDefaultValueType.user
    )
    serialized = browser.to_components()
    assert len(serialized) == 4
    assert serialized[2]["components"][0]["default_values"] == [
        {"id": 789, "type": "user"}
    ]


async def test_pagination_reads_only_registered_data_and_displays_filtered_totals(
    browser_controller, guild
):
    entries = [character(index, main=index == 1) for index in range(1, 39)]
    browser_controller.manager.guild_roster.return_value = entries
    call = interaction(guild, guild.member())
    await browser_controller.guild_roster(call, class_id="paladin", page=1)
    sent = browser_controller.send.call_args.kwargs
    assert "38 characters" in sent["embed"].description
    assert "1 main •" in sent["embed"].description
    assert "37 alts" in sent["embed"].description
    assert "1 member\n" in sent["embed"].description
    assert "John Stormwind16" in sent["embed"].description
    assert "John Stormwind31" not in sent["embed"].description
    assert "Page 2/3" in sent["embed"].footer.text
    assert not button(sent["view"], "Previous").disabled
    assert not button(sent["view"], "Next").disabled
    guild.fetch_member.assert_not_called()
    guild.fetch_roles.assert_not_called()


@pytest.mark.parametrize(
    ("callback", "selected", "expected"),
    [
        (
            "change_class",
            ["mage"],
            {"user_id": 789, "class_id": "mage", "kind": "alts"},
        ),
        ("change_class", ["all"], {"user_id": 789, "class_id": None, "kind": "alts"}),
        (
            "change_kind",
            ["mains"],
            {"user_id": 789, "class_id": "warrior", "kind": "mains"},
        ),
        (
            "change_member",
            ["987"],
            {"user_id": 987, "class_id": "warrior", "kind": "alts"},
        ),
        ("change_member", [], {"user_id": None, "class_id": "warrior", "kind": "alts"}),
    ],
)
async def test_filter_changes_reset_page_and_preserve_other_filters(
    browser_controller, guild, callback, selected, expected
):
    browser = view(
        browser_controller,
        user_id=789,
        class_id="warrior",
        kind="alts",
        page=2,
        pages=3,
    )
    call = interaction(guild, guild.member())
    call.data = {"values": selected}
    await getattr(browser, callback)(call)
    browser_controller.manager.guild_roster.assert_awaited_once_with(
        guild, call.user, **expected
    )
    assert browser_controller.send.call_args.kwargs["view"].page == 0
    browser_controller.guild_roster.assert_not_called()
    assert browser.page == 2
    assert browser.class_id == "warrior"
    assert browser.user_id == 789


async def test_reset_filters_and_navigation_keep_private_owner_context(
    browser_controller, guild
):
    browser_controller.manager.guild_roster.return_value = [
        character(index) for index in range(45)
    ]
    browser = view(
        browser_controller,
        user_id=789,
        class_id="warrior",
        kind="mains",
        page=1,
        pages=3,
    )
    call = interaction(guild, guild.member())
    await browser.previous(call)
    browser_controller.manager.guild_roster.assert_awaited_with(
        guild, call.user, user_id=789, class_id="warrior", kind="mains"
    )
    assert browser_controller.send.call_args.kwargs["view"].page == 0
    await browser.next(call)
    browser_controller.manager.guild_roster.assert_awaited_with(
        guild, call.user, user_id=789, class_id="warrior", kind="mains"
    )
    assert browser_controller.send.call_args.kwargs["view"].page == 2
    await browser.reset(call)
    browser_controller.manager.guild_roster.assert_awaited_with(
        guild, call.user, user_id=None, class_id=None, kind="all"
    )
    assert browser_controller.send.call_args.kwargs["view"].page == 0
    browser_controller.guild_roster.assert_not_called()


async def test_empty_filters_keep_controls_and_explain_next_steps(
    browser_controller, guild
):
    call = interaction(guild, guild.member())
    await browser_controller.guild_roster(
        call, user_id=789, class_id="warrior", kind="alts", page=9
    )
    sent = browser_controller.send.call_args.kwargs
    description = sent["embed"].description
    assert "Warrior • Alternate characters • <@789>" in description
    assert "0 characters" in description
    assert "Reset Filters" in description and "My Characters" in description
    assert sent["view"].page == 0
    assert button(sent["view"], "Previous").disabled
    assert button(sent["view"], "Next").disabled
    assert not sent["view"].class_filter.disabled


async def test_roster_shrinking_between_clicks_clamps_to_last_available_page(
    browser_controller, guild
):
    browser_controller.manager.guild_roster.return_value = [character(1)]
    call = interaction(guild, guild.member())
    await browser_controller.guild_roster(call, page=7)
    sent = browser_controller.send.call_args.kwargs
    assert "John Stormwind1" in sent["embed"].description
    assert "Page 1/1" in sent["embed"].footer.text
    assert button(sent["view"], "Previous").disabled
    assert button(sent["view"], "Next").disabled


@pytest.mark.parametrize("wrong_context", ["owner", "guild", "allowlist", "stopping"])
async def test_old_browser_rechecks_owner_guild_allowlist_and_shutdown(
    browser_controller, guild, wrong_context
):
    browser = view(browser_controller)
    call = interaction(guild, guild.member(789 if wrong_context == "owner" else 456))
    if wrong_context == "guild":
        browser.guild_id = 999
    elif wrong_context == "allowlist":
        browser_controller.settings = replace(
            browser_controller.settings, allowed_guild_ids=frozenset({999})
        )
    elif wrong_context == "stopping":
        browser_controller.controller.stopping = True
    await browser.next(call)
    browser_controller.manager.guild_roster.assert_not_called()
    browser_controller.guild_roster.assert_not_called()
    browser_controller.send.assert_not_called()


@pytest.mark.parametrize(
    "settings_change",
    [
        {"enable_guild_roster": False},
        {"roster_visibility": "disabled"},
        {"roster_visibility": "admins"},
    ],
)
async def test_every_filter_callback_rechecks_changed_roster_policy(
    browser_controller, guild, settings_change
):
    browser = view(browser_controller)
    browser_controller.settings = replace(
        browser_controller.settings, **settings_change
    )
    call = interaction(guild, guild.member())
    call.data = {"values": ["warrior"]}
    await browser.change_class(call)
    browser_controller.manager.guild_roster.assert_not_called()
    browser_controller.guild_roster.assert_not_called()
    browser_controller.send.assert_not_called()
    assert "My Characters" in call.followup.send.call_args.kwargs["content"]


async def test_admin_can_browse_restricted_roster(browser_controller, guild):
    browser_controller.settings = replace(
        browser_controller.settings, roster_visibility="admins"
    )
    call = interaction(guild, guild.member(), admin=True)
    await browser_controller.guild_roster(call)
    browser_controller.manager.guild_roster.assert_awaited_once()


async def test_my_characters_remains_private_and_available_if_guild_roster_is_disabled(
    browser_controller, guild
):
    browser = view(browser_controller)
    browser_controller.settings = replace(
        browser_controller.settings, enable_guild_roster=False
    )
    call = interaction(guild, guild.member())
    browser_controller.execute = AsyncMock()
    await browser.my_characters(call)
    browser_controller.open.assert_awaited_once_with(call, manage=False)
    browser_controller.manager.guild_roster.assert_not_called()
    browser_controller.execute.assert_not_called()


async def test_my_characters_still_checks_private_menu_owner(browser_controller, guild):
    call = interaction(guild, guild.member(789))
    await view(browser_controller).my_characters(call)
    browser_controller.open.assert_not_called()
    browser_controller.manager.guild_roster.assert_not_called()


@pytest.mark.parametrize(
    ("callback", "data"),
    [
        ("change_class", {"values": ["administrator"]}),
        ("change_kind", {"values": ["invalid"]}),
        ("change_member", {"values": ["-123"]}),
        ("change_member", {"values": ["9" * 30]}),
        ("change_member", {"values": ["123", "456"]}),
        ("change_member", {"values": ["１２３"]}),
        ("change_class", {}),
        ("change_class", {"values": "mage"}),
    ],
)
async def test_forged_filter_values_do_not_reach_storage(
    browser_controller, guild, callback, data
):
    call = interaction(guild, guild.member())
    call.data = data
    await getattr(view(browser_controller), callback)(call)
    browser_controller.manager.guild_roster.assert_not_called()
    browser_controller.guild_roster.assert_not_called()


async def test_parallel_clicks_use_each_interaction_value_and_do_not_mutate_other_menus(
    browser_controller, guild
):
    first = view(browser_controller, kind="mains")
    second = view(browser_controller, kind="alts", class_id="hunter")
    first.class_filter._values = ["druid"]
    mage, warrior = interaction(guild, guild.member()), interaction(
        guild, guild.members[456]
    )
    mage.data, warrior.data = {"values": ["mage"]}, {"values": ["warrior"]}
    await asyncio.gather(first.change_class(mage), first.change_class(warrior))
    applied = [
        call.kwargs["class_id"]
        for call in browser_controller.manager.guild_roster.call_args_list
    ]
    assert sorted(applied) == ["mage", "warrior"]
    assert first.class_id is None
    assert second.class_id == "hunter" and second.kind == "alts"
    browser_controller.guild_roster.assert_not_called()


async def test_long_names_and_escaped_owner_names_fit_without_hiding_rows(
    browser_controller, guild
):
    entries = [character(index, main=index == 1) for index in range(PAGE_SIZE)]
    for index, entry in enumerate(entries):
        entry.full_name = "A" * 50 + " " + "B" * 49 + chr(65 + index)
    browser_controller.manager.guild_roster.return_value = entries
    guild.get_member = lambda user_id: SimpleNamespace(
        display_name="@everyone **name**" * 20
    )
    call = interaction(guild, guild.member())
    await browser_controller.guild_roster(call, user_id=456)
    sent = browser_controller.send.call_args.kwargs
    assert all(entry.full_name in sent["embed"].description for entry in entries)
    assert len(sent["embed"].description) <= 4096
    assert "@everyone" not in sent["embed"].description
    assert "\\*\\*name" in sent["embed"].description
    guild.fetch_member.assert_not_called()


async def test_roster_slash_choices_exclude_disabled_classes(browser_controller):
    command = build_roster_command(browser_controller)
    parameter = next(item for item in command.parameters if item.name == "wow_class")
    assert [choice.value for choice in parameter.choices] == [
        entry.id for entry in browser_controller.settings.display_classes
    ]
    assert len(parameter.choices) == 9
    assert "monk" not in {choice.value for choice in parameter.choices}
    assert "death_knight" not in {choice.value for choice in parameter.choices}


async def test_enabled_modern_classes_appear_in_alphabetical_filters(
    browser_controller, env
):
    browser_controller.settings = Settings.from_env(
        {**env, "ENABLED_CLASSES": "monk,warrior,death_knight"}
    )
    browser = view(browser_controller, class_id="monk")
    assert [option.label for option in browser.class_filter.options] == [
        "All classes",
        "Death Knight",
        "Monk",
        "Warrior",
    ]
    command = build_roster_command(browser_controller)
    parameter = next(item for item in command.parameters if item.name == "wow_class")
    assert [choice.name for choice in parameter.choices] == [
        "Death Knight",
        "Monk",
        "Warrior",
    ]


@pytest.mark.parametrize("class_id", ["monk", "death_knight"])
async def test_disabled_filter_from_stale_command_or_menu_does_not_read_storage(
    browser_controller, guild, class_id
):
    call = interaction(guild, guild.member())
    await browser_controller.guild_roster(call, class_id=class_id)
    assert (
        "Choose an enabled class or All classes"
        in call.followup.send.call_args.kwargs["content"]
    )
    browser_controller.manager.guild_roster.assert_not_awaited()

    call = interaction(guild, guild.member())
    call.data = {"values": [class_id]}
    await view(browser_controller).change_class(call)
    assert (
        "Choose an enabled class or All classes"
        in call.followup.send.call_args.kwargs["content"]
    )
    browser_controller.manager.guild_roster.assert_not_awaited()


async def test_old_selected_class_can_be_reset_after_it_is_disabled(
    browser_controller, guild
):
    browser = view(browser_controller, class_id="warrior")
    browser_controller.settings = replace(
        browser_controller.settings,
        classes=tuple(
            entry
            for entry in browser_controller.settings.classes
            if entry.id != "warrior"
        ),
    )
    call = interaction(guild, guild.member())
    await browser.next(call)
    browser_controller.manager.guild_roster.assert_not_awaited()
    assert "All classes" in call.followup.send.call_args.kwargs["content"]
    await browser.reset(call)
    browser_controller.manager.guild_roster.assert_awaited_once_with(
        guild, call.user, user_id=None, class_id=None, kind="all"
    )
    updated = browser_controller.send.call_args.kwargs["view"]
    assert "warrior" not in {option.value for option in updated.class_filter.options}
    assert updated.class_id is None


async def test_disabled_class_cannot_be_rendered_as_missing_dropdown_default(
    browser_controller,
):
    with pytest.raises(UserError, match="enabled class or All classes"):
        view(browser_controller, class_id="monk")


async def test_all_classes_keeps_saved_disabled_characters_visible(
    browser_controller, guild
):
    saved = [character(1, main=True), character(2)]
    saved[0].class_id = "monk"
    saved[1].class_id = "death_knight"
    browser_controller.manager.guild_roster.return_value = saved
    call = interaction(guild, guild.member())
    await browser_controller.guild_roster(call)
    sent = browser_controller.send.call_args.kwargs
    assert "Monk | Main" in sent["embed"].description
    assert "Death Knight | Alt" in sent["embed"].description
    assert [entry.class_id for entry in saved] == ["monk", "death_knight"]
    assert "monk" not in {option.value for option in sent["view"].class_filter.options}
    browser_controller.manager.guild_roster.assert_awaited_once_with(
        guild, call.user, user_id=None, class_id=None, kind="all"
    )
