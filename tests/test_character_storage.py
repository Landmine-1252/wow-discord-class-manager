import asyncio
import sqlite3
import uuid
from contextlib import closing

import pytest

from wow_class_manager.character_names import normalized_name_key
from wow_class_manager.character_store import CharacterStore
from wow_class_manager.roles import UserError
from wow_class_manager.storage import Panel, StorageError, Store


def named(
    kind="add", primary="John", secondary="Stormwind", class_id="paladin", **extra
):
    return {
        "kind": kind,
        "primary_name": primary,
        "secondary_name": secondary,
        "name_key": normalized_name_key(primary, secondary),
        "class_id": class_id,
        "gameplay_role": "dps",
        **extra,
    }


async def change(characters, payload, guild_id=123, user_id=456):
    op_id = uuid.uuid4().hex
    await characters.prepare(guild_id, user_id, op_id, payload, set(), set())
    return await characters.commit(guild_id, user_id, op_id)


@pytest.fixture
def characters(store):
    return CharacterStore(store)


async def test_first_character_is_main_and_names_separate(characters, store):
    roster = await change(characters, named())
    assert roster.main.primary_name == "John"
    assert roster.main.secondary_name == "Stormwind"
    assert roster.main.full_name == "John Stormwind"
    assert roster.main.class_id == "paladin"
    assert roster.main.is_main
    assert roster.main.created_at and roster.main.updated_at
    assert roster.alts == ()
    row = await store._run(
        lambda: store.db.execute(
            "SELECT primary_name,secondary_name FROM characters"
        ).fetchone()
    )
    assert row == ("John", "Stormwind")


async def test_main_switch_demotes_without_deleting_and_edit_preserves_ids(characters):
    john = (await change(characters, named())).main
    roster = await change(
        characters, named(primary="Sam", secondary="Ironforge", class_id="warrior")
    )
    sam = roster.alts[0]
    assert roster.main.id == john.id
    roster = await change(characters, {"kind": "promote", "character_id": sam.id})
    assert roster.main.id == sam.id and roster.alts[0].id == john.id
    assert len(roster.characters) == 2
    roster = await change(characters, {"kind": "promote", "character_id": john.id})
    assert roster.main.id == john.id and roster.alts[0].id == sam.id
    roster = await change(
        characters,
        named(
            kind="edit",
            primary="Sam",
            secondary="Ironforge",
            class_id="hunter",
            character_id=sam.id,
        ),
    )
    assert roster.main.id == john.id and roster.alts[0].class_id == "hunter"
    roster = await change(
        characters,
        named(
            kind="edit",
            primary="John",
            secondary="Ironforge",
            class_id="warrior",
            character_id=john.id,
        ),
    )
    assert roster.main.id == john.id and roster.main.full_name == "John Ironforge"
    assert roster.main.class_id == "warrior"
    assert roster.main.created_at == john.created_at
    assert roster.alts[0].id == sam.id


@pytest.mark.parametrize(
    "primary,secondary", [("John", "Stormwind"), ("john", "stormwind")]
)
async def test_duplicate_full_name_rejected(characters, primary, secondary):
    await change(characters, named())
    with pytest.raises(UserError, match="already exists"):
        await change(characters, named(primary=primary, secondary=secondary))
    assert len((await characters.roster(123, 456)).characters) == 1


async def test_shared_components_and_user_names_allowed(characters):
    for primary, secondary in [
        ("John", "Stormwind"),
        ("John", "Ironforge"),
        ("Sam", "Ironforge"),
    ]:
        await change(characters, named(primary=primary, secondary=secondary))
    assert len((await characters.roster(123, 456)).characters) == 3
    assert (
        await change(characters, named(), user_id=789)
    ).main.full_name == "John Stormwind"


async def test_unicode_duplicate_key(characters):
    await change(characters, named(primary="Élodie", secondary="Cœur"))
    with pytest.raises(UserError, match="already exists"):
        await change(characters, named(primary="éLODIE", secondary="cŒUR"))


async def test_guild_user_isolation_and_ownership(characters):
    john = (await change(characters, named())).main
    other_guild = (await change(characters, named(), guild_id=999)).main
    other_user = (await change(characters, named(), user_id=789)).main
    assert other_guild.id != john.id != other_user.id
    for guild_id, user_id in [(999, 456), (123, 789)]:
        with pytest.raises(UserError, match="not in your roster"):
            await characters.character(guild_id, user_id, john.id)
        with pytest.raises(UserError, match="not in your roster"):
            await change(
                characters,
                {"kind": "promote", "character_id": john.id},
                guild_id=guild_id,
                user_id=user_id,
            )
    assert len(await characters.list_guild(123)) == 2
    assert len(await characters.list_guild(999)) == 1


async def test_roster_filters(characters):
    await change(characters, named())
    await change(
        characters, named(primary="Sam", secondary="Ironforge", class_id="warrior")
    )
    await change(characters, named(), user_id=789)
    assert len(await characters.list_guild(123, kind="main")) == 2
    assert len(await characters.list_guild(123, kind="alt")) == 1
    assert len(await characters.list_guild(123, user_id=456)) == 2
    assert (await characters.list_guild(123, class_id="warrior"))[
        0
    ].full_name == "Sam Ironforge"
    with pytest.raises(UserError):
        await characters.list_guild(123, kind="unknown")


async def test_remove_alt_and_replace_main(characters):
    john = (await change(characters, named())).main
    sam = (
        await change(
            characters, named(primary="Sam", secondary="Ironforge", class_id="warrior")
        )
    ).alts[0]
    with pytest.raises(UserError):
        await change(characters, {"kind": "remove", "character_id": john.id})
    assert (await characters.roster(123, 456)).main.id == john.id
    roster = await change(
        characters,
        {"kind": "remove", "character_id": john.id, "replacement_id": sam.id},
    )
    assert len(roster.characters) == 1 and roster.main.id == sam.id
    roster = await change(characters, {"kind": "remove", "character_id": sam.id})
    assert roster.characters == () and roster.main is None
    assert (await change(characters, named())).main.id > sam.id


async def test_alt_removal_and_purge_are_owner_scoped(characters):
    john = (await change(characters, named())).main
    sam = (await change(characters, named(primary="Sam", secondary="Ironforge"))).alts[
        0
    ]
    other = await change(characters, named(), user_id=789)
    roster = await change(characters, {"kind": "remove", "character_id": sam.id})
    assert roster.main.id == john.id and len(roster.characters) == 1
    assert not (await change(characters, {"kind": "purge"})).characters
    assert (await characters.roster(123, 789)).main.id == other.main.id


async def test_journal_persists_without_changing_main_and_abort(characters):
    john = (await change(characters, named())).main
    sam = (await change(characters, named(primary="Sam", secondary="Ironforge"))).alts[
        0
    ]
    payload = {"kind": "promote", "character_id": sam.id}
    await characters.prepare(123, 456, "test-op", payload, {11}, {22})
    pending = await characters.pending(123, 456)
    assert (
        pending.op_id == "test-op"
        and pending.previous_roles == {11}
        and pending.wanted_roles == {22}
    )
    assert (await characters.roster(123, 456)).main.id == john.id
    await characters.prepare(123, 456, "test-op", payload, {11}, {22})
    with pytest.raises(UserError, match="interrupted"):
        await characters.prepare(123, 456, "another", payload, {11}, {22})
    await characters.abort(123, 456, "wrong-id")
    assert await characters.pending(123, 456) is not None
    await characters.abort(123, 456, "test-op")
    assert await characters.pending(123, 456) is None
    assert (await characters.roster(123, 456)).main.id == john.id


async def test_commit_idempotent_and_no_second_main(characters):
    await characters.prepare(123, 456, "first", named(), set(), {11})
    first = await characters.commit(123, 456, "first")
    second = await characters.commit(123, 456, "first")
    assert first == second
    assert len(second.characters) == 1
    assert await characters.pending(123, 456) is None


async def test_concurrent_preparations_and_limit(characters):
    results = await asyncio.gather(
        characters.prepare(123, 456, "one", named(max_characters=1), set(), {11}),
        characters.prepare(
            123,
            456,
            "two",
            named(primary="Sam", secondary="Ironforge", max_characters=1),
            set(),
            {22},
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, UserError) for result in results) == 1
    pending = await characters.pending(123, 456)
    await characters.commit(123, 456, pending.op_id)
    with pytest.raises(UserError, match="limit"):
        await change(
            characters,
            named(primary="Another", secondary="Character", max_characters=1),
        )


async def test_more_than_25_characters_without_default_limit(characters):
    for index in range(30):
        await change(
            characters,
            named(
                primary="Name"
                + chr(ord("A") + index // 26)
                + chr(ord("A") + index % 26),
                max_characters=0,
            ),
        )
    roster = await characters.roster(123, 456)
    assert len(roster.characters) == 30 and len(roster.alts) == 29


async def test_database_constraints_directly(characters, store):
    john = (await change(characters, named())).main
    other = (await change(characters, named(), user_id=789)).main

    def foreign_main():
        with store.db:
            store.db.execute(
                "UPDATE rosters SET main_character_id=? WHERE guild_id=123 AND"
                " user_id=456",
                (other.id,),
            )

    with pytest.raises(StorageError):
        await store._run(foreign_main)
    assert (await characters.roster(123, 456)).main.id == john.id

    def duplicate_main_row():
        with store.db:
            store.db.execute("INSERT INTO rosters VALUES (123,456,?)", (john.id,))

    with pytest.raises(StorageError):
        await store._run(duplicate_main_row)

    def duplicate_name():
        with store.db:
            store.db.execute(
                "INSERT INTO characters(guild_id,user_id,primary_name,"
                "secondary_name,name_key,class_id,created_at,updated_at) "
                "SELECT guild_id,user_id,primary_name,secondary_name,name_key,"
                "class_id,created_at,updated_at "
                "FROM characters WHERE id=?",
                (john.id,),
            )

    with pytest.raises(StorageError):
        await store._run(duplicate_name)

    def delete_main_directly():
        with store.db:
            store.db.execute("DELETE FROM characters WHERE id=?", (john.id,))

    with pytest.raises(StorageError):
        await store._run(delete_main_directly)


@pytest.mark.parametrize(
    "primary,secondary,name_key",
    [
        ("John", "Stormwind", "unrelated key"),
        ("@everyone", "Stormwind", "@everyone stormwind"),
        ("Jo\u034fhn", "Stormwind", "jo\u034fhn stormwind"),
        ("John", "", "john"),
    ],
)
async def test_database_rejects_invalid_names_without_service_validation(
    characters, store, primary, secondary, name_key
):
    john = (await change(characters, named())).main

    def invalid_name():
        with store.db:
            store.db.execute(
                "UPDATE characters SET primary_name=?,secondary_name=?,name_key=? WHERE"
                " id=?",
                (primary, secondary, name_key, john.id),
            )

    with pytest.raises(StorageError):
        await store._run(invalid_name)
    assert (await characters.roster(123, 456)).main.full_name == "John Stormwind"


async def test_commit_failure_rolls_back_and_leaves_journal(characters, store):
    john = (await change(characters, named())).main
    sam = (await change(characters, named(primary="Sam", secondary="Ironforge"))).alts[
        0
    ]
    await characters.prepare(
        123, 456, "fail", {"kind": "promote", "character_id": sam.id}, {11}, {22}
    )

    def fail_delete():
        store.db.execute(
            "CREATE TRIGGER simulate_disk_failure BEFORE DELETE ON character_operations"
            " BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
        )

    await store._run(fail_delete)
    with pytest.raises(StorageError):
        await characters.commit(123, 456, "fail")
    assert (await characters.roster(123, 456)).main.id == john.id
    assert (await characters.pending(123, 456)).op_id == "fail"
    await store._run(lambda: store.db.execute("DROP TRIGGER simulate_disk_failure"))
    assert (await characters.commit(123, 456, "fail")).main.id == sam.id


async def test_data_and_pending_operation_survive_reopen(tmp_path):
    path = tmp_path / "persistent.sqlite3"
    store = Store(path)
    await store.open()
    characters = CharacterStore(store)
    john = (await change(characters, named())).main
    await characters.prepare(
        123, 456, "waiting", named(primary="Sam", secondary="Ironforge"), {11}, {11}
    )
    await store.close()
    await store.open()
    assert (await characters.roster(123, 456)).main.id == john.id
    assert (await characters.pending(123, 456)).op_id == "waiting"
    assert len((await characters.commit(123, 456, "waiting")).characters) == 2
    await store.close()


async def test_v2_migration_preserves_guild_roles_panel_and_metadata(tmp_path):
    path = tmp_path / "v2.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE guilds(guild_id INTEGER PRIMARY KEY); "
            "CREATE TABLE roles(guild_id INTEGER, class_id TEXT, role_id INTEGER,"
            " PRIMARY KEY(guild_id,class_id)); "
            "CREATE TABLE panels(guild_id INTEGER PRIMARY KEY,channel_id INTEGER,"
            "message_id INTEGER); "
            "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT); "
            "CREATE TABLE retired_panels(guild_id INTEGER,channel_id INTEGER,"
            "message_id INTEGER PRIMARY KEY); "
            "INSERT INTO guilds VALUES(123); "
            "INSERT INTO roles VALUES(123,'paladin',11); "
            "INSERT INTO panels VALUES(123,22,33); "
            "INSERT INTO metadata VALUES('config','preserved'); "
            "PRAGMA user_version=2;"
        )
    store = Store(path)
    await store.open()
    assert await store.roles(123) == {"paladin": 11}
    assert await store.panel(123) == Panel(123, 22, 33)
    assert await store.get_meta("config") == "preserved"
    assert not (await CharacterStore(store).roster(123, 456)).characters
    assert (
        await store._run(
            lambda: store.db.execute("PRAGMA foreign_key_check").fetchall()
        )
        == []
    )
    assert await store._run(
        lambda: store.db.execute("PRAGMA quick_check").fetchall()
    ) == [("ok",)]
    await store.close()
    await store.open()
    assert await store.roles(123) == {"paladin": 11}
    await store.close()


async def test_legacy_single_names_preserved_and_can_be_completed(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE characters(id INTEGER PRIMARY KEY,guild_id INTEGER,"
            "user_id INTEGER,name TEXT,class_id TEXT,is_main INTEGER); "
            "INSERT INTO characters VALUES(42,123,456,'John Stormwind','paladin',1); "
            "INSERT INTO characters VALUES(43,123,456,'Sam','warrior',0);"
        )
    store = Store(path)
    await store.open()
    characters = CharacterStore(store)
    roster = await characters.roster(123, 456)
    assert roster.main.id == 42 and roster.main.primary_name == "John Stormwind"
    assert (
        roster.main.secondary_name == "" and roster.main.legacy_name == "John Stormwind"
    )
    assert roster.main.needs_completion
    with pytest.raises(UserError, match="Complete this legacy"):
        await change(characters, {"kind": "promote", "character_id": 43})
    roster = await change(characters, named(kind="edit", character_id=42))
    assert roster.main.id == 42 and roster.main.full_name == "John Stormwind"
    assert not roster.main.needs_completion
    assert roster.main.legacy_name == "John Stormwind"
    original = await store._run(
        lambda: store.db.execute(
            "SELECT name FROM legacy_characters_v2 WHERE id=42"
        ).fetchone()[0]
    )
    assert original == "John Stormwind"
    assert (
        await store._run(
            lambda: store.db.execute("PRAGMA foreign_key_check").fetchall()
        )
        == []
    )
    assert await store._run(
        lambda: store.db.execute("PRAGMA quick_check").fetchall()
    ) == [("ok",)]
    await store.close()


@pytest.mark.parametrize("flags", [(0, 0), (1, 1)])
async def test_legacy_ambiguous_main_is_never_guessed(tmp_path, flags):
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE characters(id INTEGER PRIMARY KEY,guild_id INTEGER,"
            "user_id INTEGER,name TEXT,class_id TEXT,is_main INTEGER);"
        )
        db.executemany(
            "INSERT INTO characters VALUES(?,123,456,?,'paladin',?)",
            [(42, "John", flags[0]), (43, "Sam", flags[1])],
        )
        db.commit()
    store = Store(path)
    await store.open()
    roster = await CharacterStore(store).roster(123, 456)
    assert len(roster.characters) == 2 and roster.main is None
    await store.close()


async def test_unknown_legacy_schema_rolls_back_without_data_loss(tmp_path):
    path = tmp_path / "unknown.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE characters(id INTEGER PRIMARY KEY,name TEXT); "
            "INSERT INTO characters VALUES(7,'Original Name');"
        )
    with pytest.raises(StorageError, match="unrecognized"):
        await Store(path).open()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT * FROM characters").fetchall() == [
            (7, "Original Name")
        ]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='rosters'"
            ).fetchone()
            is None
        )


async def test_duplicate_legacy_names_roll_back_without_guessing(tmp_path):
    path = tmp_path / "duplicates.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE characters(id INTEGER PRIMARY KEY,guild_id INTEGER,"
            "user_id INTEGER,name TEXT,class_id TEXT); "
            "INSERT INTO characters VALUES(1,123,456,'John','paladin'); "
            "INSERT INTO characters VALUES(2,123,456,'john','warrior');"
        )
    with pytest.raises(StorageError, match="duplicate normalized names"):
        await Store(path).open()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT name FROM characters ORDER BY id").fetchall() == [
            ("John",),
            ("john",),
        ]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0


async def test_migration_integrity_failure_rolls_back_without_repairing_data(tmp_path):
    path = tmp_path / "invalid-v2.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE guilds(guild_id INTEGER PRIMARY KEY); "
            "CREATE TABLE roles(guild_id INTEGER REFERENCES guilds(guild_id),"
            "class_id TEXT,role_id INTEGER,PRIMARY KEY(guild_id,class_id)); "
            "CREATE TABLE panels(guild_id INTEGER PRIMARY KEY,channel_id INTEGER,"
            "message_id INTEGER); "
            "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT); "
            "CREATE TABLE retired_panels(guild_id INTEGER,channel_id INTEGER,"
            "message_id INTEGER PRIMARY KEY); "
            "INSERT INTO roles VALUES(123,'paladin',11); "
            "PRAGMA user_version=2;"
        )
    with pytest.raises(StorageError, match="inconsistent relationships"):
        await Store(path).open()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT * FROM roles").fetchall() == [(123, "paladin", 11)]
        assert db.execute("SELECT * FROM guilds").fetchall() == []
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN"
            " ('characters','rosters','character_operations')"
        ).fetchall()


async def test_privacy_erase_includes_pending_intent_and_is_scoped(characters, store):
    await change(characters, named())
    await change(characters, named(), user_id=789)
    await change(characters, named(), guild_id=999)
    await characters.prepare(
        123, 456, "waiting", named(primary="Sam", secondary="Ironforge"), {11}, {22}
    )
    await characters.prepare(123, 888, "first-add", named(), {11}, {22})
    await store.associate(123, "paladin", 11)
    await store.save_panel(Panel(123, 22, 33))
    await characters.erase(123, 456)
    await characters.erase(123, 888)
    assert not (await characters.roster(123, 456)).characters
    assert await characters.pending(123, 456) is None
    assert await characters.pending(123, 888) is None
    assert (await characters.roster(123, 789)).main
    assert (await characters.roster(999, 456)).main
    assert await store.roles(123) == {"paladin": 11}
    assert await store.panel(123) == Panel(123, 22, 33)
    assert (
        await store._run(
            lambda: store.db.execute(
                "SELECT COUNT(*) FROM rosters WHERE guild_id=123 AND user_id IN"
                " (456,888)"
            ).fetchone()[0]
        )
        == 0
    )


async def test_explicit_removal_and_erasure_remove_matching_legacy_archive(tmp_path):
    path = tmp_path / "legacy-privacy.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE characters(id INTEGER PRIMARY KEY,guild_id INTEGER,"
            "user_id INTEGER,name TEXT,class_id TEXT,is_main INTEGER); "
            "INSERT INTO characters VALUES(42,123,456,'John','paladin',1); "
            "INSERT INTO characters VALUES(43,123,456,'Sam','warrior',0); "
            "INSERT INTO characters VALUES(44,123,789,'Other','warrior',1);"
        )
    store = Store(path)
    await store.open()
    characters = CharacterStore(store)
    await change(characters, {"kind": "remove", "character_id": 43})
    assert (
        await store._run(
            lambda: store.db.execute(
                "SELECT COUNT(*) FROM legacy_characters_v2 WHERE id=43"
            ).fetchone()[0]
        )
        == 0
    )
    await characters.erase(123, 456)
    assert await store._run(
        lambda: store.db.execute("SELECT id FROM legacy_characters_v2").fetchall()
    ) == [(44,)]
    assert (await characters.roster(123, 789)).main.id == 44
    await store.close()
