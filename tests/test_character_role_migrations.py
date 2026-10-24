"""Upgrade real version-3 rosters without inferring roles or losing recovery intent."""

import json
import sqlite3
from contextlib import closing

import pytest
from test_character_storage import change, named

import wow_class_manager.character_store as character_storage
from wow_class_manager.character_store import (
    CharacterStore,
    configure_character_sql,
    migrate_characters,
)
from wow_class_manager.characters import CharacterManager
from wow_class_manager.members import MemberManager
from wow_class_manager.roles import RoleManager
from wow_class_manager.storage import Panel, StorageError, Store


@pytest.fixture
def v3_database(tmp_path):
    path = tmp_path / "version3.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        configure_character_sql(db)
        db.executescript(
            "CREATE TABLE guilds(guild_id INTEGER PRIMARY KEY);"
            "CREATE TABLE roles(guild_id INTEGER, class_id TEXT, role_id INTEGER,"
            " PRIMARY KEY(guild_id,class_id));"
            "CREATE TABLE panels(guild_id INTEGER PRIMARY KEY,channel_id INTEGER,"
            "message_id INTEGER);"
            "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);"
            "CREATE TABLE retired_panels(guild_id INTEGER,channel_id INTEGER,"
            "message_id INTEGER PRIMARY KEY);"
            "INSERT INTO guilds VALUES(123);"
            "INSERT INTO roles VALUES(123,'priest',11);"
            "INSERT INTO panels VALUES(123,22,33);"
            "INSERT INTO metadata VALUES('config','preserved');"
        )
        with db:
            db.execute("BEGIN IMMEDIATE")
            migrate_characters(db)
            db.execute("INSERT INTO rosters VALUES(123,456,42)")
            db.executemany(
                "INSERT INTO characters(id,guild_id,user_id,primary_name,"
                "secondary_name,name_key,class_id,created_at,updated_at) "
                "VALUES(?,123,456,?,?,?,?,?,?)",
                [
                    (
                        42,
                        "John",
                        "Stormwind",
                        "john stormwind",
                        "priest",
                        "created",
                        "updated",
                    ),
                    (
                        43,
                        "Sam",
                        "Ironforge",
                        "sam ironforge",
                        "hunter",
                        "created",
                        "updated",
                    ),
                ],
            )
            payload = named(kind="edit", character_id=42, class_id="warrior")
            payload.pop("gameplay_role")
            db.execute(
                "INSERT INTO character_operations"
                " VALUES(123,456,'interrupted',?,'[11]','[22]','prepared')",
                (json.dumps(payload),),
            )
            db.execute("PRAGMA user_version=3")
    return path


def snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        return {
            name: db.execute(f"SELECT * FROM {name}").fetchall()
            for name in (
                "guilds",
                "roles",
                "panels",
                "metadata",
                "retired_panels",
                "characters",
                "rosters",
                "character_operations",
                "sqlite_sequence",
            )
        }


async def test_v3_upgrade_preserves_records_ids_main_and_pending_journal(v3_database):
    before = snapshot(v3_database)
    store = Store(v3_database)
    await store.open()
    try:
        assert await store.check_health() == 5
        after = snapshot(v3_database)
        assert after.pop("characters") == [
            (*row, None) for row in before.pop("characters")
        ]
        assert after == before
        records = CharacterStore(store)
        roster = await records.roster(123, 456)
        assert roster.main.id == 42 and roster.alts[0].id == 43
        assert all(entry.gameplay_role is None for entry in roster.characters)
        assert await store.roles(123) == {"priest": 11}
        assert await store.panel(123) == Panel(123, 22, 33)
        pending = await records.pending(123, 456)
        assert pending.op_id == "interrupted" and "gameplay_role" not in pending.payload
        await records.abort(123, 456, pending.op_id)
        saved = await change(
            records,
            named(
                kind="edit", character_id=42, class_id="priest", gameplay_role="healing"
            ),
        )
        assert saved.main.id == 42 and saved.main.gameplay_role == "healing"
        assert saved.main.created_at == "created" and saved.alts == roster.alts
    finally:
        await store.close()
    await store.open()
    try:
        assert await CharacterStore(store).roster(123, 456) == saved
        assert await store.check_health() == 5
    finally:
        await store.close()


async def test_v4_migration_failure_rolls_back_schema_and_preserves_data(
    v3_database, monkeypatch
):
    before = snapshot(v3_database)
    migrate = character_storage.migrate_character_roles

    def failed_migration(db):
        migrate(db)
        raise StorageError("Simulated migration interruption")

    monkeypatch.setattr(character_storage, "migrate_character_roles", failed_migration)
    with pytest.raises(StorageError, match="Simulated"):
        await Store(v3_database).open()
    assert snapshot(v3_database) == before
    with closing(sqlite3.connect(v3_database)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "gameplay_role" not in {
            row[1] for row in db.execute("PRAGMA table_info(characters)")
        }


async def test_old_pending_operation_recovers_without_inventing_gameplay_roles(
    v3_database, settings, locks, guild
):
    store = Store(v3_database)
    await store.open()
    try:
        await RoleManager(settings, store, locks).reconcile(guild)
        member = guild.member()
        manager = CharacterManager(
            settings, store, locks, MemberManager(settings, store, locks)
        )
        saved = await manager.roster(guild, member.id)
        assert saved.main.id == 42 and saved.main.class_id == "priest"
        assert saved.main.gameplay_role is None
        assert await manager.records.pending(guild.id, member.id) is None
        await manager.promote(guild, member.id, 43)
        promoted = await manager.records.roster(guild.id, member.id)
        assert promoted.main.id == 43 and promoted.main.gameplay_role is None
        assert promoted.alts[0].id == 42
    finally:
        await store.close()
