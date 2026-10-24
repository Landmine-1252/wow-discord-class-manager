"""Version-5 gameplay tag associations preserve existing rosters and recovery plans."""

import json
import sqlite3
from contextlib import closing

import pytest
from test_character_role_migrations import snapshot
from test_character_role_migrations import v3_database as v3_database

import wow_class_manager.storage as storage
from wow_class_manager.character_store import (
    CharacterStore,
    configure_character_sql,
    migrate_character_roles,
)
from wow_class_manager.storage import StorageError, Store


@pytest.fixture
def v4_database(v3_database):
    with closing(sqlite3.connect(v3_database)) as db:
        configure_character_sql(db)
        with db:
            db.execute("BEGIN IMMEDIATE")
            migrate_character_roles(db)
            db.execute(
                "UPDATE characters SET gameplay_role=CASE WHEN id=42 THEN 'healing'"
                " ELSE 'dps' END"
            )
            payload = json.loads(
                db.execute("SELECT payload FROM character_operations").fetchone()[0]
            )
            payload["gameplay_role"] = "tank"
            db.execute(
                "UPDATE character_operations SET payload=?", (json.dumps(payload),)
            )
            db.execute("PRAGMA user_version=4")
    return v3_database


async def test_v4_to_v5_upgrade_preserves_all_rows_and_persists_scoped_tags(
    v4_database,
):
    before = snapshot(v4_database)
    store = Store(v4_database)
    await store.open()
    try:
        assert await store.check_health() == 5
        assert snapshot(v4_database) == before
        assert await store.gameplay_roles(123) == {}
        records = CharacterStore(store)
        roster = await records.roster(123, 456)
        pending = await records.pending(123, 456)
        assert roster.main.id == 42 and roster.main.gameplay_role == "healing"
        assert pending.payload["gameplay_role"] == "tank"
        await store.associate_gameplay_role(123, "healing", 55)
        await store.associate_gameplay_role(999, "tank", 55)
    finally:
        await store.close()
    await store.open()
    try:
        assert await store.gameplay_roles(123) == {"healing": 55}
        assert await store.gameplay_roles(999) == {"tank": 55}
        assert await CharacterStore(store).roster(123, 456) == roster
        assert await CharacterStore(store).pending(123, 456) == pending
    finally:
        await store.close()


async def test_v5_migration_failure_rolls_back_schema_and_data(
    v4_database, monkeypatch
):
    before = snapshot(v4_database)
    migrate = storage.migrate_gameplay_role_tags

    def fail(db):
        migrate(db)
        raise StorageError("Simulated migration failure")

    monkeypatch.setattr(storage, "migrate_gameplay_role_tags", fail)
    with pytest.raises(StorageError, match="Simulated"):
        await Store(v4_database).open()
    assert snapshot(v4_database) == before
    with closing(sqlite3.connect(v4_database)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='gameplay_roles'"
            ).fetchone()
            is None
        )


async def test_tag_associations_reject_cross_class_collisions_and_unknown_roles(store):
    await store.associate(123, "priest", 11)
    with pytest.raises(StorageError, match="separate Discord roles"):
        await store.associate_gameplay_role(123, "healing", 11)
    assert await store.gameplay_roles(123) == {}
    await store.associate_gameplay_role(123, "healing", 22)
    with pytest.raises(StorageError, match="separate Discord roles"):
        await store.associate(123, "priest", 22)
    assert await store.roles(123) == {"priest": 11}
    with pytest.raises(StorageError):
        await store.associate_gameplay_role(123, "tank", 22)
    with pytest.raises(StorageError):
        await store.associate_gameplay_role(123, "unknown", 33)
    assert await store.gameplay_roles(123) == {"healing": 22}
    assert not store.db.in_transaction
