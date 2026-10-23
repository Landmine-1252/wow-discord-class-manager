import pytest

from wow_class_manager.storage import Panel, StorageError


async def test_startup_check_preserves_persisted_state_and_existing_probe_key(store):
    await store.associate(123, "paladin", 1001)
    await store.save_panel(Panel(123, 777, 888))
    await store.set_meta("startup:write-probe", "original value")
    before = await store._run(lambda: list(store.db.iterdump()))

    assert await store.check_health() == 3
    assert await store.check_health() == 3

    assert await store._run(lambda: list(store.db.iterdump())) == before
    assert not store.db.in_transaction
    await store.set_meta("after-check", "writable")
    assert await store.get_meta("after-check") == "writable"


async def test_startup_check_does_not_leave_probe_data(store):
    assert await store.check_health() == 3
    assert await store.get_meta("startup:write-probe") is None


async def test_startup_check_detects_read_only_database_and_rolls_back(store):
    await store._run(lambda: store.db.execute("PRAGMA query_only=ON"))
    with pytest.raises(StorageError, match="SQLite is unavailable"):
        await store.check_health()
    assert not store.db.in_transaction
    await store._run(lambda: store.db.execute("PRAGMA query_only=OFF"))
    assert await store.check_health() == 3


async def test_startup_check_detects_broken_foreign_keys_without_exposing_data(store):
    def corrupt():
        store.db.execute("PRAGMA foreign_keys=OFF")
        with store.db:
            store.db.execute(
                "INSERT INTO roles VALUES (?, ?, ?)",
                (123, "private-data-not-for-logs", 456),
            )
        store.db.execute("PRAGMA foreign_keys=ON")

    await store._run(corrupt)
    with pytest.raises(StorageError, match="relationship check failed") as caught:
        await store.check_health()
    assert "private-data" not in str(caught.value)
    assert await store.roles(123) == {"private-data-not-for-logs": 456}


async def test_startup_check_detects_integrity_failure_without_resetting_database(
    store,
):
    def corrupt():
        with store.db:
            store.db.execute("CREATE TABLE damaged (value INTEGER CHECK (value = 0))")
            store.db.execute("PRAGMA ignore_check_constraints=ON")
            store.db.execute("INSERT INTO damaged VALUES (1)")
        store.db.execute("PRAGMA ignore_check_constraints=OFF")

    await store._run(corrupt)
    with pytest.raises(StorageError, match="integrity check failed"):
        await store.check_health()
    assert (
        await store._run(
            lambda: store.db.execute("SELECT value FROM damaged").fetchone()[0]
        )
        == 1
    )


async def test_startup_check_requires_open_database(store):
    await store.close()
    with pytest.raises(StorageError, match="not open"):
        await store.check_health()
