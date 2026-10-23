import pytest
from conftest import TEST_TOKEN

from wow_class_manager.classes import DEFAULT_CLASSES, REGISTRY
from wow_class_manager.config import ConfigError, Settings
from wow_class_manager.logging_setup import Redactor


def test_defaults():
    settings = Settings.from_env({"DISCORD_BOT_TOKEN": TEST_TOKEN})
    assert tuple(item.id for item in settings.classes) == DEFAULT_CLASSES
    assert len(settings.classes) == 9
    assert settings.classes[1].color == 0xF48CBA
    assert settings.root_command == "class"
    assert settings.database_path.as_posix() == "/data/classes.sqlite3"
    assert settings.application_id is None
    assert settings.allowed(123)
    assert not settings.allow_clear_selection
    assert settings.role_change_cooldown == 3


@pytest.mark.parametrize(
    "token", ["", "replace-me", "Bot " + TEST_TOKEN, "x y.z.y", "x" * 5000]
)
def test_missing_or_invalid_token(token):
    with pytest.raises(ConfigError) as error:
        Settings.from_env({"DISCORD_BOT_TOKEN": token})
    assert not token or token not in str(error.value)


def test_token_file(env, tmp_path):
    path = tmp_path / "secret"
    path.write_text(TEST_TOKEN + "\n")
    env.pop("DISCORD_BOT_TOKEN")
    env["DISCORD_BOT_TOKEN_FILE"] = str(path)
    assert Settings.from_env(env).token == TEST_TOKEN
    env["DISCORD_BOT_TOKEN"] = TEST_TOKEN
    with pytest.raises(ConfigError, match="exactly one"):
        Settings.from_env(env)


def test_unreadable_token_file():
    with pytest.raises(ConfigError, match="could not be read"):
        Settings.from_env({"DISCORD_BOT_TOKEN_FILE": "/nonexistent/token"})


def test_overrides(env):
    env.update(
        ENABLED_CLASSES="MAGE,death_knight",
        ROLE_NAME_TEMPLATE="WoW {name}",
        ROLE_NAME_MAGE="Wizard",
        ROLE_COLOR_MAGE="#123456",
        SLASH_COMMAND_ROOT="wow",
        SLASH_SETUP_NAME="panel",
    )
    settings = Settings.from_env(env)
    assert [item.id for item in settings.classes] == ["mage", "death_knight"]
    assert settings.classes[0].name == "Wizard"
    assert settings.classes[0].color == 0x123456
    assert settings.classes[1].name == "WoW Death Knight"
    assert settings.command("setup") == "/wow panel"


@pytest.mark.parametrize(
    "key,value",
    [
        ("ENABLED_CLASSES", ""),
        ("ENABLED_CLASSES", "mage,mage"),
        ("ENABLED_CLASSES", "wizard"),
        ("ROLE_COLOR_PALADIN", "pink"),
        ("ROLE_COLOR_MAGE", "#123"),
        ("ROLE_NAME_MAGE", "Paladin"),
        ("ROLE_NAME_TEMPLATE", "{name.__class__}"),
        ("ROLE_NAME_TEMPLATE", "{unknown}"),
        ("ROLE_NAME_TEMPLATE", "{name!r}"),
        ("ROLE_NAME_MAGE", "x" * 101),
        ("ROLE_NAME_WIZARD", "Wizard"),
        ("SLASH_COMMAND_ROOT", "Uppercase"),
        ("SLASH_COMMAND_ROOT", "bad space"),
        ("SLASH_COMMAND_ROOT", "x" * 33),
        ("SLASH_ME_NAME", "set"),
        ("AUTO_CREATE_ROLES", "maybe"),
        ("ROLE_CHANGE_COOLDOWN_SECONDS", "nan"),
        ("ROLE_CHANGE_COOLDOWN_SECONDS", "inf"),
        ("ROLE_CHANGE_COOLDOWN_SECONDS", "-1"),
        ("LOG_LEVEL", "LOUD"),
        ("PANEL_TITLE", "x" * 257),
        ("PANEL_PLACEHOLDER", "x" * 151),
        ("COMMAND_SYNC_SCOPE", "guild"),
        ("COMMAND_SYNC_SCOPE", "invalid"),
        ("DISCORD_APPLICATION_ID", "abc"),
        ("ALLOWED_GUILD_IDS", "-1"),
        ("DATABASE_PATH", "relative.sqlite"),
        ("ADMIN_WEBHOOK_URL", "https://example.com/webhook"),
        ("ADMIN_WEBHOOK_URL", "http://discord.com/api/webhooks/123/" + "x" * 32),
        (
            "ADMIN_WEBHOOK_URL",
            "https://discord.com.evil.example/api/webhooks/123/" + "x" * 32,
        ),
        (
            "ADMIN_WEBHOOK_URL",
            "https://discord.com/api/webhooks/123/" + "x" * 32 + "?x=y",
        ),
    ],
)
def test_invalid_settings(env, key, value):
    env[key] = value
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_all_class_overrides(env):
    env["ENABLED_CLASSES"] = ",".join(REGISTRY)
    for key in REGISTRY:
        env[f"ROLE_NAME_{key.upper()}"] = key + " customized"
        env[f"ROLE_COLOR_{key.upper()}"] = "ABCDEF"
    settings = Settings.from_env(env)
    assert len(settings.classes) == 13
    assert all(item.color == 0xABCDEF for item in settings.classes)


def test_allowlist_and_dev_guild(env):
    env.update(
        ALLOWED_GUILD_IDS="123,456",
        DEVELOPMENT_GUILD_ID="123",
        COMMAND_SYNC_SCOPE="guild",
    )
    settings = Settings.from_env(env)
    assert settings.allowed(123) and not settings.allowed(789)
    env["DEVELOPMENT_GUILD_ID"] = "789"
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_embed_total_limit(env):
    env.update(
        PANEL_TITLE="x" * 256, PANEL_DESCRIPTION="x" * 4096, PANEL_FOOTER="x" * 2048
    )
    with pytest.raises(ConfigError, match="6000"):
        Settings.from_env(env)


def test_secret_redaction(env):
    webhook = "https://discord.com/api/webhooks/123/" + "X" * 32
    env["ADMIN_WEBHOOK_URL"] = webhook
    settings = Settings.from_env(env)
    assert TEST_TOKEN not in repr(settings) and webhook not in repr(settings)
    redact = Redactor((settings.token, webhook))
    assert TEST_TOKEN not in redact("error " + TEST_TOKEN)
    assert webhook not in redact(webhook)
    assert "X" * 32 not in Redactor()(webhook)
    assert "/members/456" not in redact("/guilds/123/members/456/roles/789")
