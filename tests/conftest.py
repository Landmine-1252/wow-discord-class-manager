from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from wow_class_manager.config import Settings
from wow_class_manager.roles import Locks
from wow_class_manager.storage import Store

TEST_TOKEN = "A" * 24 + "." + "B" * 6 + "." + "C" * 30


@pytest.fixture
def env(tmp_path):
    return {
        "DISCORD_BOT_TOKEN": TEST_TOKEN,
        "DATABASE_PATH": str(tmp_path / "classes.sqlite3"),
        "HEALTH_PATH": str(tmp_path / "health.json"),
        "ROLE_CHANGE_COOLDOWN_SECONDS": "0",
    }


@pytest.fixture
def settings(env):
    return Settings.from_env(env)


@pytest.fixture
async def store(tmp_path):
    db = Store(tmp_path / "classes.sqlite3")
    await db.open()
    yield db
    await db.close()


@pytest.fixture
def locks():
    return Locks()


@dataclass
class FakeRole:
    id: int
    name: str
    position: int = 1
    colour: discord.Colour = field(default_factory=discord.Colour.default)
    permissions: discord.Permissions = field(default_factory=discord.Permissions.none)
    managed: bool = False
    hoist: bool = False
    mentionable: bool = False
    edits: list = field(default_factory=list)

    def is_default(self):
        return self.id == 1

    def __ge__(self, other):
        return self.position >= other.position

    def __gt__(self, other):
        return self.position > other.position

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        for key, value in kwargs.items():
            if key != "reason":
                setattr(self, key, value)
        return self


def http_error(kind=discord.Forbidden):
    status = (
        404 if kind is discord.NotFound else 403 if kind is discord.Forbidden else 500
    )
    return kind(
        SimpleNamespace(status=status, reason="test"), "simulated Discord error"
    )


class FakeGuild:
    def __init__(self, guild_id=123):
        self.id = guild_id
        self.unavailable = False
        self.roles = [FakeRole(1, "@everyone", 0)]
        self.me = SimpleNamespace(
            id=900,
            guild_permissions=discord.Permissions(manage_roles=True),
            top_role=FakeRole(900, "Bot", 100),
        )
        self.channels = []
        self.members = {}
        self.fetch_roles = AsyncMock(side_effect=lambda: list(self.roles))
        self.fetch_member = AsyncMock(side_effect=self._fetch_member)
        self.create_role = AsyncMock(side_effect=self._create_role)
        self.fetch_channel = AsyncMock(side_effect=self._fetch_channel)

    @property
    def default_role(self):
        return self.roles[0] if self.roles else None

    async def _create_role(self, **kwargs):
        role = FakeRole(
            max([1000] + [role.id for role in self.roles]) + 1, kwargs.pop("name")
        )
        kwargs.pop("reason", None)
        for key, value in kwargs.items():
            setattr(role, key, value)
        self.roles.append(role)
        return role

    async def _fetch_member(self, member_id):
        if member_id not in self.members:
            raise http_error(discord.NotFound)
        return self.members[member_id]

    async def _fetch_channel(self, channel_id):
        for channel in self.channels:
            if channel.id == channel_id:
                return channel
        raise http_error(discord.NotFound)

    def member(self, member_id=456, roles=()):
        member = MagicMock(spec=discord.Member)
        member.id, member.guild, member.bot = member_id, self, False
        member.roles = list(roles)

        async def add(*items, **kwargs):
            assert kwargs["atomic"] is True
            for role in items:
                if role.id not in {item.id for item in member.roles}:
                    member.roles.append(role)

        async def remove(*items, **kwargs):
            assert kwargs["atomic"] is True
            member.roles = [
                item
                for item in member.roles
                if item.id not in {role.id for role in items}
            ]

        member.add_roles = AsyncMock(side_effect=add)
        member.remove_roles = AsyncMock(side_effect=remove)
        member.edit = AsyncMock(
            side_effect=AssertionError("Never replace a member's entire role list")
        )
        self.members[member_id] = member
        return member

    def channel(self, channel_id=777):
        channel = MagicMock(spec=discord.TextChannel)
        channel.id, channel.guild = channel_id, self
        channel.permissions_for.return_value = discord.Permissions(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            read_message_history=True,
        )
        channel.overwrites_for.return_value = discord.PermissionOverwrite()
        channel.messages = {}

        async def send(**kwargs):
            message = SimpleNamespace(
                id=max([channel_id * 100] + list(channel.messages)) + 1,
                author=SimpleNamespace(id=900),
                edit=AsyncMock(),
                **kwargs,
            )
            channel.messages[message.id] = message
            return message

        async def fetch(message_id):
            if message_id not in channel.messages:
                raise discord.NotFound(
                    SimpleNamespace(status=404, reason="Not Found"),
                    {"code": 10008, "message": "Unknown Message"},
                )
            return channel.messages[message_id]

        channel.send = AsyncMock(side_effect=send)
        channel.fetch_message = AsyncMock(side_effect=fetch)
        self.channels.append(channel)
        return channel


@pytest.fixture
def guild():
    return FakeGuild()


def interaction(guild, member, channel=None, *, admin=False):
    value = MagicMock(spec=discord.Interaction)
    value.type = discord.InteractionType.application_command
    value.guild, value.guild_id = guild, guild.id
    value.client.get_guild.return_value = guild
    value.user = member
    value.channel = channel
    value.channel_id = channel.id if channel else None
    value.permissions = discord.Permissions(
        manage_guild=admin, manage_roles=admin, view_channel=True, send_messages=True
    )
    value.response = MagicMock()
    value.response.type = None
    value.response.is_done.return_value = False
    value.response.send_message = AsyncMock()

    async def defer(**kwargs):
        value.response.is_done.return_value = True
        value.response.type = (
            discord.InteractionResponseType.deferred_message_update
            if not kwargs.get("thinking", False)
            and value.type
            in (discord.InteractionType.component, discord.InteractionType.modal_submit)
            else discord.InteractionResponseType.deferred_channel_message
        )

    value.response.defer = AsyncMock(side_effect=defer)
    value.followup = SimpleNamespace(send=AsyncMock())
    value.edit_original_response = AsyncMock()
    value.message = None
    return value
