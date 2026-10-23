import pytest

from wow_class_manager.config import (
    CHARACTER_PANEL_DESCRIPTION,
    PANEL_DESCRIPTION,
    ConfigError,
    Settings,
)


def test_character_defaults(env):
    settings = Settings.from_env(env)
    assert settings.enable_character_management
    assert settings.character_command_root == "character"
    assert settings.max_characters_per_user == 0
    assert settings.character_primary_name_min_length == 2
    assert settings.character_primary_name_max_length == 12
    assert settings.character_secondary_name_min_length == 2
    assert settings.character_secondary_name_max_length == 12
    assert settings.enable_guild_roster and settings.roster_visibility == "members"
    assert settings.allow_character_class_edit
    assert settings.character_panel_title == "World of Warcraft Character Manager"
    assert settings.panel_description == CHARACTER_PANEL_DESCRIPTION


def test_character_feature_disabled_preserves_legacy_defaults(env):
    env["ENABLE_CHARACTER_MANAGEMENT"] = "false"
    settings = Settings.from_env(env)
    assert not settings.enable_character_management
    assert settings.panel_description == PANEL_DESCRIPTION
    assert settings.panel_title == "Choose Your Class"


def test_character_customization(env):
    env.update(
        CHARACTER_COMMAND_ROOT="hero",
        MAX_CHARACTERS_PER_USER="100",
        CHARACTER_PRIMARY_NAME_MIN_LENGTH="1",
        CHARACTER_PRIMARY_NAME_MAX_LENGTH="50",
        CHARACTER_SECONDARY_NAME_MIN_LENGTH="3",
        CHARACTER_SECONDARY_NAME_MAX_LENGTH="30",
        CHARACTER_PANEL_TITLE="Guild Characters",
        PANEL_DESCRIPTION="Register your guild characters here.",
        ROSTER_VISIBILITY="admins",
        ALLOW_CHARACTER_CLASS_EDIT="false",
    )
    settings = Settings.from_env(env)
    assert settings.character_command_root == "hero"
    assert settings.max_characters_per_user == 100
    assert settings.character_primary_name_max_length == 50
    assert settings.character_secondary_name_min_length == 3
    assert settings.character_panel_title == "Guild Characters"
    assert settings.panel_description == "Register your guild characters here."
    assert settings.roster_visibility == "admins"
    assert not settings.allow_character_class_edit


@pytest.mark.parametrize(
    "key,value",
    [
        ("ENABLE_CHARACTER_MANAGEMENT", "maybe"),
        ("CHARACTER_COMMAND_ROOT", "Main"),
        ("CHARACTER_COMMAND_ROOT", "main character"),
        ("CHARACTER_COMMAND_ROOT", "class"),
        ("CHARACTER_COMMAND_ROOT", "roster"),
        ("SLASH_COMMAND_ROOT", "roster"),
        ("MAX_CHARACTERS_PER_USER", "-1"),
        ("MAX_CHARACTERS_PER_USER", "1.5"),
        ("MAX_CHARACTERS_PER_USER", "9" * 5000),
        ("CHARACTER_PRIMARY_NAME_MIN_LENGTH", "0"),
        ("CHARACTER_PRIMARY_NAME_MIN_LENGTH", "13"),
        ("CHARACTER_PRIMARY_NAME_MAX_LENGTH", "1"),
        ("CHARACTER_PRIMARY_NAME_MAX_LENGTH", "51"),
        ("CHARACTER_SECONDARY_NAME_MIN_LENGTH", "13"),
        ("CHARACTER_SECONDARY_NAME_MAX_LENGTH", "1"),
        ("ENABLE_GUILD_ROSTER", "yes"),
        ("ROSTER_VISIBILITY", "public"),
        ("CHARACTER_PANEL_TITLE", ""),
        ("CHARACTER_PANEL_TITLE", "x" * 257),
        ("ALLOW_CHARACTER_CLASS_EDIT", "sometimes"),
    ],
)
def test_invalid_character_configuration(env, key, value):
    env[key] = value
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_inactive_roster_name_is_not_reserved(env):
    env.update(ENABLE_GUILD_ROSTER="false", CHARACTER_COMMAND_ROOT="roster")
    assert Settings.from_env(env).character_command_root == "roster"
    env.update(ENABLE_GUILD_ROSTER="true", ROSTER_VISIBILITY="disabled")
    assert Settings.from_env(env).character_command_root == "roster"


def test_active_panel_title_controls_embed_limit(env):
    env.update(
        PANEL_TITLE="x" * 256,
        CHARACTER_PANEL_TITLE="x",
        PANEL_DESCRIPTION="x" * 4096,
        PANEL_FOOTER="x" * 1800,
    )
    assert Settings.from_env(env).character_panel_title == "x"
    env["ENABLE_CHARACTER_MANAGEMENT"] = "false"
    with pytest.raises(ConfigError, match="6000"):
        Settings.from_env(env)
