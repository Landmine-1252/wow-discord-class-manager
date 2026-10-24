"""Directory scale, privacy, search, and private-navigation regressions."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import http_error, interaction
from test_character_interface import character
from test_character_interface import character_controller as character_controller
from test_character_navigation import component
from test_characters import add_pair
from test_characters import character_setup as character_setup

from wow_class_manager.character_commands import build_roster_command
from wow_class_manager.config import Settings
from wow_class_manager.roster_directory import (
    DIRECTORY_PAGE_SIZE,
    Directory,
    RosterFilters,
    character_order,
)
from wow_class_manager.roster_ui import (
    GuildRosterBrowser,
    MemberRosterView,
    RosterFilterView,
    RosterSearchModal,
)


def text(view):
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def button(view, label):
    return next(
        item
        for item in view.walk_children()
        if isinstance(item, discord.ui.Button) and item.label == label
    )


def reply_view(call):
    if call.edit_original_response.called:
        return call.edit_original_response.call_args.kwargs["view"]
    return call.followup.send.call_args.kwargs["view"]


def view(ui, guild, *, characters=(), page=0, filters_open=False, **filters):
    state = RosterFilters(**filters)
    cls = RosterFilterView if filters_open else GuildRosterBrowser
    return cls(ui, guild, 456, Directory(characters, state), state, page)


def large_roster(members=80, alts=3):
    return [
        character(
            member * (alts + 1) + index,
            owner=1000 + member,
            primary=f"Player{member:02d}",
            secondary=f"Character{index}",
            main=index == 0,
            gameplay_role=("tank", "healing", "dps", None)[member % 4],
        )
        for member in reversed(range(members))
        for index in range(alts + 1)
    ]


def test_equal_names_sort_consistently_across_owners_and_input_order():
    entries = [
        character(3, owner=900, main=True),
        character(1, owner=700, main=True),
        character(2, owner=800, main=True, primary="JOHN"),
    ]
    for saved in (entries, entries[::-1]):
        result = Directory(saved, RosterFilters())
        assert [entry.user_id for entry in result.matches] == [700, 800, 900]


async def test_eighty_member_directory_has_one_main_each_and_bounded_pages(
    character_controller, guild
):
    ui = character_controller
    entries = large_roster()
    ui.manager.guild_roster.return_value = entries
    expected = sorted(
        (entry for entry in entries if entry.is_main), key=character_order
    )
    seen = []
    for page in range(10):
        call = interaction(guild, guild.member())
        await ui.guild_roster(call, page=page)
        browser = reply_view(call)
        sections = [
            item
            for item in browser.walk_children()
            if isinstance(item, discord.ui.Section)
        ]
        assert len(sections) == DIRECTORY_PAGE_SIZE
        seen.extend(
            int(section.accessory.custom_id.rsplit(":", 1)[1]) for section in sections
        )
        assert browser.total_children_count <= 40
        assert browser.content_length() <= 4000
        assert "80 registered members · 320 characters" in text(browser)
        assert "20 Tank · 💚 20 Healing · ⚔️ 20 DPS · 20 unset" in text(browser)
        assert "3 alts" in text(browser)
        assert "Owned by" not in text(browser)
        assert not any(
            isinstance(item, discord.ui.Select) for item in browser.walk_children()
        )
        assert button(browser, "Previous").disabled is (page == 0)
        assert button(browser, "Next").disabled is (page == 9)
        sent = call.followup.send.call_args.kwargs
        assert sent["ephemeral"] is True
        assert sent["allowed_mentions"].everyone is False
        assert sent["allowed_mentions"].users is False
        assert "embed" not in sent and "content" not in sent
    assert seen == [entry.id for entry in expected]
    assert ui.manager.guild_roster.await_count == 10
    guild.fetch_member.assert_not_called()
    guild.fetch_roles.assert_not_called()
    for operation in ("add", "edit", "remove", "promote", "purge", "roster"):
        getattr(ui.manager, operation).assert_not_called()


async def test_filters_combine_and_overview_counts_only_mains(
    character_controller, guild
):
    ui = character_controller
    ui.manager.guild_roster.return_value = [
        character(1, main=True, gameplay_role="tank"),
        character(2, primary="Jane", secondary="Light", gameplay_role="healing"),
        character(3, primary="Other", owner=789, gameplay_role="healing"),
        character(4, primary="Jane", class_id="priest", gameplay_role="healing"),
    ]
    call = interaction(guild, guild.member())
    await ui.guild_roster(
        call,
        kind="alts",
        class_id="paladin",
        gameplay_role="healing",
        user_id=456,
        query="LIGHT",
    )
    browser = reply_view(call)
    rendered = text(browser)
    assert "Jane Light" in rendered and "Other" not in rendered
    assert "1 result" in rendered
    assert "1 Tank · 💚 0 Healing" in rendered
    assert browser.children[0].to_component_dict()["accent_color"] == 0xF48CBA
    ui.manager.guild_roster.assert_awaited_once_with(guild, call.user)


async def test_unknown_roles_and_no_main_legacy_members_are_explicit(
    character_controller, guild
):
    ui = character_controller
    ui.manager.guild_roster.return_value = [
        character(main=True),
        character(2, owner=789, primary="Legacy"),
    ]
    call = interaction(guild, guild.member())
    await ui.guild_roster(call, gameplay_role="unset")
    assert "1 unset" in text(reply_view(call))
    assert "1 member without a main" in text(reply_view(call))
    assert "Legacy" not in text(reply_view(call))
    all_call = interaction(guild, guild.member())
    await ui.guild_roster(all_call, kind="all")
    assert "Legacy" in text(reply_view(all_call))


async def test_filter_panel_has_native_choices_and_current_defaults(
    character_controller, guild, env
):
    ui = character_controller
    ui.settings = Settings.from_env(
        {**env, "ENABLED_CLASSES": "monk,warrior,death_knight"}
    )
    browser = view(
        ui,
        guild,
        filters_open=True,
        class_id="monk",
        gameplay_role="healing",
        kind="alts",
    )
    assert [option.label for option in browser.class_filter.options] == [
        "All classes",
        "Death Knight",
        "Monk",
        "Warrior",
    ]
    assert all(option.emoji for option in browser.class_filter.options[1:])
    for control, value in (
        (browser.class_filter, "monk"),
        (browser.role_filter, "healing"),
        (browser.kind_filter, "alts"),
    ):
        assert [option.value for option in control.options if option.default] == [value]
    assert "unset" in {option.value for option in browser.role_filter.options}
    assert browser.total_children_count <= 40
    assert browser.to_components()[0]["type"] == 17


@pytest.mark.parametrize(
    "callback,value,field,expected",
    [
        ("change_class", "mage", "class_id", "mage"),
        ("change_class", "all", "class_id", None),
        ("change_role", "tank", "gameplay_role", "tank"),
        ("change_role", "unset", "gameplay_role", "unset"),
        ("change_role", "all", "gameplay_role", None),
        ("change_kind", "all", "kind", "all"),
    ],
)
async def test_changing_one_filter_resets_page_and_preserves_other_state(
    character_controller, guild, callback, value, field, expected
):
    original = view(
        character_controller,
        guild,
        filters_open=True,
        page=3,
        user_id=789,
        class_id="warrior",
        gameplay_role="dps",
        kind="alts",
        query="name",
    )
    call = interaction(guild, guild.member())
    call.data = {"values": [value]}
    await getattr(original, callback)(call)
    updated = reply_view(call)
    assert isinstance(updated, RosterFilterView)
    assert updated.filters == replace(original.filters, **{field: expected})
    assert updated.page == 0
    assert original.page == 3 and original.filters.class_id == "warrior"


async def test_filter_panel_round_trip_preserves_page_when_unchanged(
    character_controller, guild
):
    entries = large_roster()
    character_controller.manager.guild_roster.return_value = entries
    original = view(character_controller, guild, characters=entries, page=4)
    call = interaction(guild, guild.member())
    await original.open_filters(call)
    panel = reply_view(call)
    assert isinstance(panel, RosterFilterView) and panel.page == 4
    back = interaction(guild, guild.member())
    await panel.return_to_roster(back)
    assert reply_view(back).page == 4


async def test_member_detail_and_back_preserve_search_filters_and_page(
    character_controller, guild
):
    ui = character_controller
    entries = large_roster()
    original = view(
        ui, guild, characters=entries, page=2, class_id="paladin", query="Player"
    )
    member_entries = [entry for entry in entries if entry.user_id == 1020]
    ui.manager.guild_roster.return_value = member_entries
    call = interaction(guild, guild.member())
    await original.view_member(call, user_id=1020)
    detail = reply_view(call)
    assert isinstance(detail, MemberRosterView)
    assert detail.filters == original.filters and detail.page == 2
    assert all(entry.full_name in text(detail) for entry in member_entries)
    assert "⭐ Main" in text(detail) and "Alt" in text(detail)
    ui.manager.guild_roster.assert_awaited_once_with(guild, call.user, user_id=1020)
    ui.manager.guild_roster.return_value = entries
    back = interaction(guild, guild.member())
    await button(detail, "Back to Roster").callback(back)
    restored = reply_view(back)
    assert restored.page == 2 and restored.filters == original.filters
    assert "Page 3/10" in text(restored)


async def test_view_buttons_open_the_member_attached_to_each_row(
    character_controller, guild
):
    ui = character_controller
    browser = view(ui, guild, characters=large_roster(2))
    buttons = [
        item
        for item in browser.walk_children()
        if isinstance(item, discord.ui.Button) and item.label == "View"
    ]
    assert len(buttons) == 2
    for member_id, selected in zip((1000, 1001), buttons, strict=True):
        call = interaction(guild, guild.member())
        call.data = {"user_id": 999, "guild_id": 999}
        await selected.callback(call)
        ui.manager.guild_roster.assert_awaited_with(guild, call.user, user_id=member_id)


async def test_detail_pages_handle_many_alts_and_deleted_rosters(
    character_controller, guild
):
    ui = character_controller
    entries = large_roster(1, 24)
    ui.manager.guild_roster.return_value = entries
    original = view(ui, guild, characters=entries)
    call = interaction(guild, guild.member())
    await original.view_member(call, user_id=1000)
    detail = reply_view(call)
    seen = []
    for page in range(4):
        assert detail.detail_page == page
        seen.extend(
            entry.id for entry in entries if f"**{entry.full_name}**" in text(detail)
        )
        assert detail.content_length() < 4000
        if page < 3:
            call = interaction(guild, guild.member())
            await detail.next(call)
            detail = reply_view(call)
    assert sorted(seen) == sorted(entry.id for entry in entries)
    ui.manager.guild_roster.return_value = []
    call = interaction(guild, guild.member())
    await detail.previous(call)
    empty = reply_view(call)
    assert empty.detail_page == 0
    assert "no longer has registered characters" in text(empty)
    assert button(empty, "Back to Roster")


async def test_search_modal_uses_native_member_picker_without_deferring(
    character_controller, guild
):
    browser = view(character_controller, guild, user_id=789, query="Storm")
    call = interaction(guild, guild.member())
    call.response.send_modal = AsyncMock()
    await browser.search(call)
    modal = call.response.send_modal.call_args.args[0]
    assert isinstance(modal, RosterSearchModal)
    assert modal.query.default == "Storm"
    assert isinstance(modal.member, discord.ui.UserSelect)
    assert modal.member.default_values[0].id == 789
    assert modal.member.min_values == 0 and modal.member.max_values == 1
    assert not modal.query.required and not modal.member.required
    assert len(modal.to_components()) == 2
    call.response.defer.assert_not_called()
    character_controller.manager.guild_roster.assert_not_called()


async def test_search_finds_alts_with_unicode_and_clears_member_selection(
    character_controller, guild
):
    ui = character_controller
    ui.manager.guild_roster.return_value = [
        character(1, main=True),
        character(
            2, primary="Éowyn", secondary="Light", owner=789, gameplay_role="healing"
        ),
    ]
    source = view(ui, guild, user_id=456, class_id="paladin", gameplay_role="healing")
    modal = RosterSearchModal(source)
    modal.query._value = "  E\u0301OWYN   light  "
    modal.member._values = []
    call = interaction(guild, guild.member())
    await modal.on_submit(call)
    browser = reply_view(call)
    assert browser.filters.kind == "all"
    assert browser.filters.user_id is None
    assert browser.filters.class_id == "paladin"
    assert browser.filters.gameplay_role == "healing"
    assert "Éowyn Light" in text(browser) and "1 result" in text(browser)
    assert modal.is_finished()


async def test_member_search_uses_selected_id_and_can_clear_query(
    character_controller, guild
):
    source = view(character_controller, guild, query="Previous", kind="all", page=3)
    modal = RosterSearchModal(source)
    modal.query._value = ""
    modal.member._values = [discord.Object(id=789)]
    call = interaction(guild, guild.member())
    await modal.on_submit(call)
    result = reply_view(call)
    assert result.filters == replace(source.filters, user_id=789, query="")
    assert result.page == 0


@pytest.mark.parametrize("cause", ["expired", "replaced", "wrong_owner", "disabled"])
async def test_search_submission_rechecks_source_and_access(
    character_controller, guild, cause
):
    ui = character_controller
    source = view(ui, guild)
    modal = RosterSearchModal(source)
    modal.query._value = "John"
    if cause == "expired":
        source.stop()
    elif cause == "replaced":
        ui.track_view(700, source)
        ui.track_view(700, view(ui, guild))
    elif cause == "disabled":
        ui.settings = replace(ui.settings, enable_guild_roster=False)
    call = interaction(guild, guild.member(789 if cause == "wrong_owner" else 456))
    await modal.on_submit(call)
    ui.manager.guild_roster.assert_not_called()


async def test_double_search_submit_reads_only_once(character_controller, guild):
    modal = RosterSearchModal(view(character_controller, guild))
    modal.query._value = "John"
    calls = [interaction(guild, guild.member()) for _ in range(2)]
    await asyncio.gather(*(modal.on_submit(call) for call in calls))
    assert character_controller.manager.guild_roster.await_count == 1


@pytest.mark.parametrize("wrong_context", ["owner", "guild", "allowlist", "stopping"])
async def test_browser_rechecks_owner_guild_allowlist_and_shutdown(
    character_controller, guild, wrong_context
):
    ui = character_controller
    browser = view(ui, guild)
    call = interaction(guild, guild.member(789 if wrong_context == "owner" else 456))
    if wrong_context == "guild":
        browser.guild_id = 999
    elif wrong_context == "allowlist":
        ui.settings = replace(ui.settings, allowed_guild_ids=frozenset({999}))
    elif wrong_context == "stopping":
        ui.controller.stopping = True
    await browser.next(call)
    ui.manager.guild_roster.assert_not_called()


@pytest.mark.parametrize(
    "settings_change",
    [
        {"enable_guild_roster": False},
        {"roster_visibility": "disabled"},
        {"roster_visibility": "admins"},
    ],
)
@pytest.mark.parametrize("path", ["page", "filters", "search", "detail", "back"])
async def test_all_directory_paths_recheck_changed_visibility(
    character_controller, guild, settings_change, path
):
    ui = character_controller
    browser = view(ui, guild)
    ui.settings = replace(ui.settings, **settings_change)
    call = interaction(guild, guild.member())
    call.response.send_modal = AsyncMock()
    if path == "page":
        await browser.next(call)
    elif path == "filters":
        await browser.open_filters(call)
    elif path == "search":
        await browser.search(call)
    elif path == "detail":
        await browser.view_member(call, user_id=789)
    else:
        await MemberRosterView(browser, 789, 0, 1).return_to_roster(call)
    ui.manager.guild_roster.assert_not_called()
    call.response.send_modal.assert_not_called()


async def test_admin_browsing_and_personal_roster_fallback(character_controller, guild):
    ui = character_controller
    ui.settings = replace(ui.settings, roster_visibility="admins")
    call = interaction(guild, guild.member(), admin=True)
    await ui.guild_roster(call)
    assert isinstance(reply_view(call), GuildRosterBrowser)
    ui.settings = replace(ui.settings, enable_guild_roster=False)
    ui.open = AsyncMock()
    browser = view(ui, guild)
    own = interaction(guild, guild.member())
    await browser.my_characters(own)
    ui.open.assert_awaited_once_with(own)
    await browser.my_characters(interaction(guild, guild.member(789)))
    assert ui.open.await_count == 1


@pytest.mark.parametrize(
    "callback,data",
    [
        ("change_class", {"values": ["administrator"]}),
        ("change_kind", {"values": ["invalid"]}),
        ("change_role", {"values": ["administrator"]}),
        ("change_role", {"values": ["healing", "tank"]}),
        ("change_class", {}),
        ("change_class", {"values": "mage"}),
    ],
)
async def test_forged_filters_do_not_reach_storage(
    character_controller, guild, callback, data
):
    browser = view(character_controller, guild, filters_open=True)
    call = interaction(guild, guild.member())
    call.data = data
    await getattr(browser, callback)(call)
    character_controller.manager.guild_roster.assert_not_called()


@pytest.mark.parametrize("user_id", [True, -123, 2**64, "123"])
async def test_invalid_member_ids_do_not_read_storage(
    character_controller, guild, user_id
):
    await character_controller.guild_roster(
        interaction(guild, guild.member()), user_id=user_id
    )
    character_controller.manager.guild_roster.assert_not_called()


async def test_parallel_filter_clicks_use_interaction_values(
    character_controller, guild
):
    ui = character_controller
    original = view(ui, guild, filters_open=True)
    original.class_filter._values = ["druid"]
    calls = [interaction(guild, guild.member()) for _ in range(2)]
    for call, value in zip(calls, ("mage", "warrior"), strict=True):
        call.data = {"values": [value]}
    await asyncio.gather(*(original.change_class(call) for call in calls))
    assert [reply_view(call).filters.class_id for call in calls] == ["mage", "warrior"]
    assert original.filters.class_id is None


async def test_disabled_class_filters_fail_but_reset_and_saved_records_work(
    character_controller, guild
):
    ui = character_controller
    browser = view(ui, guild, class_id="warrior")
    ui.settings = replace(
        ui.settings,
        classes=tuple(entry for entry in ui.settings.classes if entry.id != "warrior"),
    )
    call = interaction(guild, guild.member())
    await browser.next(call)
    ui.manager.guild_roster.assert_not_called()
    ui.manager.guild_roster.return_value = [character(main=True, class_id="warrior")]
    reset = interaction(guild, guild.member())
    await browser.reset(reset)
    updated = reply_view(reset)
    assert updated.filters == RosterFilters()
    assert "Warrior" in text(updated)
    stale = interaction(guild, guild.member())
    await ui.guild_roster(stale, class_id="monk")
    assert "All classes" in stale.followup.send.call_args.kwargs["content"]
    assert ui.manager.guild_roster.await_count == 1


async def test_empty_results_and_shrinking_rosters_keep_navigation_usable(
    character_controller, guild
):
    ui = character_controller
    call = interaction(guild, guild.member())
    await ui.guild_roster(call, page=900, query="missing")
    empty = reply_view(call)
    assert empty.page == 0 and "No characters found" in text(empty)
    assert button(empty, "Previous").disabled and button(empty, "Next").disabled
    assert not button(empty, "Reset").disabled
    ui.manager.guild_roster.return_value = [character(main=True)]
    call = interaction(guild, guild.member())
    await ui.guild_roster(call, page=900)
    assert reply_view(call).page == 0 and "John Stormwind" in text(reply_view(call))


async def test_long_legacy_names_and_mentions_stay_within_discord_limits(
    character_controller, guild
):
    entries = large_roster(8)
    for entry in entries:
        entry.full_name = "@everyone **" + "A" * 1000
    guild.get_member = lambda _: SimpleNamespace(
        display_name="@everyone **name**" * 100
    )
    browser = view(character_controller, guild, characters=entries, query="@everyone")
    assert browser.content_length() <= 4000
    assert browser.total_children_count <= 40
    assert "@everyone" not in text(browser)
    assert "\\*\\*name" in text(browser)
    assert text(browser).count("Role not set") == 2
    ids = [
        item.custom_id
        for item in browser.walk_children()
        if isinstance(item, discord.ui.Button)
    ]
    assert len(ids) == len(set(ids))


async def test_slash_roster_defaults_and_explicit_search_filters(
    character_controller, guild
):
    ui = character_controller
    command = build_roster_command(ui)
    classes = next(item for item in command.parameters if item.name == "wow_class")
    assert [choice.value for choice in classes.choices] == [
        entry.id for entry in ui.settings.display_classes
    ]
    ui.guild_roster = AsyncMock()
    call = interaction(guild, guild.member())
    await command.callback(call)
    assert ui.guild_roster.call_args.kwargs["kind"] == "mains"
    await command.callback(call, query="Storm", gameplay_role="healing")
    assert ui.guild_roster.call_args.kwargs["kind"] == "all"
    assert ui.guild_roster.call_args.kwargs["gameplay_role"] == "healing"
    await command.callback(call, query="Storm", kind="mains")
    assert ui.guild_roster.call_args.kwargs["kind"] == "mains"


async def test_layout_to_personal_dashboard_replaces_then_dismisses_old_message(
    character_controller, guild
):
    ui = character_controller
    call = interaction(guild, guild.member())
    old_message = SimpleNamespace(id=700, delete=AsyncMock())
    call.followup.send.return_value = old_message
    await ui.guild_roster(call)
    browser = reply_view(call)
    personal = component(guild)
    personal.message.flags.components_v2 = True
    new_message = SimpleNamespace(id=701, delete=AsyncMock())
    personal.followup.send.return_value = new_message
    await browser.my_characters(personal)
    personal.edit_original_response.assert_not_called()
    assert personal.followup.send.call_args.kwargs["embed"].title == "Your Characters"
    personal.followup.delete_message.assert_awaited_once_with(700)
    assert browser.is_finished()
    assert set(ui._private_views) == {701}


async def test_failed_format_transition_keeps_original_directory(
    character_controller, guild
):
    ui = character_controller
    call = interaction(guild, guild.member())
    old_message = SimpleNamespace(id=700, delete=AsyncMock())
    call.followup.send.return_value = old_message
    await ui.guild_roster(call)
    browser = reply_view(call)
    personal = component(guild)
    personal.message.flags.components_v2 = True
    personal.followup.send.side_effect = [
        http_error(discord.HTTPException),
        SimpleNamespace(delete=AsyncMock()),
    ]
    await browser.my_characters(personal)
    assert not browser.is_finished()
    assert ui._private_views[700] is browser
    personal.followup.delete_message.assert_not_called()


@pytest.mark.parametrize("close", ["done", "timeout"])
async def test_layout_views_keep_private_cleanup(character_controller, guild, close):
    ui = character_controller
    call = interaction(guild, guild.member())
    message = SimpleNamespace(id=700, delete=AsyncMock())
    call.followup.send.return_value = message
    await ui.guild_roster(call)
    browser = reply_view(call)
    if close == "done":
        done = component(guild)
        done.message.flags.components_v2 = True
        await browser.done(done)
        done.delete_original_response.assert_awaited_once()
        done.followup.send.assert_not_called()
    else:
        await browser.on_timeout()
        message.delete.assert_awaited_once()
    assert browser.is_finished()
    assert not ui._private_views and not ui._private_sessions


async def test_directory_and_details_use_saved_owner_scopes_without_writes(
    character_controller, character_setup, guild, store
):
    ui = character_controller
    manager, member, _ = character_setup
    await add_pair(manager, guild, member)
    other = guild.member(789)
    await manager.add(guild, other.id, "Jane", "Light", "priest", "healing")
    ui.manager = manager
    roles_before = {entry.id for entry in member.roles}
    changes_before = await store._run(lambda: store.db.total_changes)
    guild.fetch_member.reset_mock()
    guild.fetch_roles.reset_mock()
    call = interaction(guild, member)
    await ui.guild_roster(call)
    browser = reply_view(call)
    assert "John Stormwind" in text(browser) and "Jane Light" in text(browser)
    assert "Sam Ironforge" not in text(browser)
    details_call = interaction(guild, member)
    await browser.view_member(details_call, user_id=789)
    detail = reply_view(details_call)
    assert "Jane Light" in text(detail)
    assert "John Stormwind" not in text(detail) and "Sam Ironforge" not in text(detail)
    search_call = interaction(guild, member)
    await ui.guild_roster(search_call, kind="all", query="Iron", user_id=member.id)
    assert "Sam Ironforge" in text(reply_view(search_call))
    assert await store._run(lambda: store.db.total_changes) == changes_before
    assert {entry.id for entry in member.roles} == roles_before
    guild.fetch_member.assert_not_called()
    guild.fetch_roles.assert_not_called()


async def test_queued_layout_click_cannot_overwrite_newer_menu(
    character_controller, guild
):
    ui = character_controller
    started, release = asyncio.Event(), asyncio.Event()

    async def read(*args, **kwargs):
        started.set()
        await release.wait()
        return large_roster()

    ui.manager.guild_roster.side_effect = read
    browser = view(ui, guild, characters=large_roster())
    ui.track_view(700, browser)
    first, second = component(guild), component(guild)
    first.message.flags.components_v2 = second.message.flags.components_v2 = True
    pending = asyncio.create_task(browser.next(first))
    await asyncio.wait_for(started.wait(), 1)
    queued = asyncio.create_task(browser.open_filters(second))
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(pending, queued), 1)
    assert reply_view(first).page == 1
    assert browser.is_finished()
    second.edit_original_response.assert_not_called()
    assert ui.manager.guild_roster.await_count == 1
    assert "menu has just changed" in second.followup.send.call_args.kwargs["content"]


@pytest.mark.parametrize("invalid", ["x" * 101, None])
async def test_unbounded_or_invalid_search_does_not_read_storage(
    character_controller, guild, invalid
):
    call = interaction(guild, guild.member())
    await character_controller.guild_roster(call, query=invalid)
    character_controller.manager.guild_roster.assert_not_called()
