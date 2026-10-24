"""Serialized SQLite storage with durable, non-destructive schema migrations."""

import asyncio
import fcntl
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO


class StorageError(RuntimeError):
    """A safe error suitable for logs and administrator responses."""


@dataclass(frozen=True, slots=True)
class Panel:
    guild_id: int
    channel_id: int
    message_id: int


def migrate_gameplay_role_tags(db: sqlite3.Connection) -> None:
    """Add independent Discord associations without changing characters or journals."""
    db.execute(
        "CREATE TABLE gameplay_roles ("
        "guild_id INTEGER NOT NULL REFERENCES guilds(guild_id), "
        "gameplay_role TEXT NOT NULL CHECK(gameplay_role_known(gameplay_role)), "
        "role_id INTEGER NOT NULL, "
        "PRIMARY KEY(guild_id, gameplay_role), "
        "UNIQUE(guild_id, role_id))"
    )


class Store:
    def __init__(self, path: Path):
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._file_lock: IO[bytes] | None = None
        self._lock = asyncio.Lock()

    async def _run[T](self, operation: Callable[[], T]) -> T:
        async with self._lock:
            task = asyncio.create_task(asyncio.to_thread(operation))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A worker thread cannot be cancelled: finish before releasing the lock.
                try:
                    await task
                finally:
                    raise
            except (sqlite3.Error, OSError):
                raise StorageError(
                    "SQLite is unavailable. Check DATABASE_PATH, free space and /data"
                    " ownership; restore a backup if corrupt. Never delete a database"
                    " to fix permissions."
                ) from None

    @property
    def db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StorageError("SQLite is not open.")
        return self._connection

    async def open(self) -> None:
        def initialize() -> None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.with_suffix(self.path.suffix + ".lock").open("a+b")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                raise StorageError(
                    "Another process is using this database. Run exactly one bot"
                    " container."
                ) from None
            self._file_lock = handle
            try:
                self._connection = sqlite3.connect(
                    self.path, timeout=10, check_same_thread=False
                )
                self.db.execute("PRAGMA foreign_keys=ON")
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.execute("PRAGMA synchronous=FULL")
                from .character_store import (
                    configure_character_sql,
                    migrate_character_roles,
                    migrate_characters,
                )

                configure_character_sql(self.db)
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version > 5:
                    raise StorageError(
                        "The database schema is newer than this application. Upgrade"
                        " the image or restore a compatible backup."
                    )
                with self.db:
                    self.db.execute("BEGIN IMMEDIATE")
                    if version < 1:
                        self.db.execute(
                            "CREATE TABLE guilds (guild_id INTEGER PRIMARY KEY)"
                        )
                        self.db.execute(
                            "CREATE TABLE roles ("
                            "guild_id INTEGER NOT NULL REFERENCES guilds(guild_id), "
                            "class_id TEXT NOT NULL, "
                            "role_id INTEGER NOT NULL, "
                            "PRIMARY KEY(guild_id, class_id), "
                            "UNIQUE(guild_id, role_id))"
                        )
                        self.db.execute(
                            "CREATE TABLE panels ("
                            "guild_id INTEGER PRIMARY KEY REFERENCES guilds(guild_id), "
                            "channel_id INTEGER NOT NULL, "
                            "message_id INTEGER NOT NULL)"
                        )
                        self.db.execute("PRAGMA user_version=1")
                    if version < 2:
                        self.db.execute(
                            "CREATE TABLE metadata ("
                            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                        )
                        self.db.execute(
                            "CREATE TABLE retired_panels ("
                            "guild_id INTEGER NOT NULL, "
                            "channel_id INTEGER NOT NULL, "
                            "message_id INTEGER PRIMARY KEY)"
                        )
                        self.db.execute("PRAGMA user_version=2")
                    if version < 3:
                        migrate_characters(self.db)
                        self.db.execute("PRAGMA user_version=3")
                    if version < 4:
                        migrate_character_roles(self.db)
                        self.db.execute("PRAGMA user_version=4")
                    if version < 5:
                        migrate_gameplay_role_tags(self.db)
                        if self.db.execute("PRAGMA foreign_key_check").fetchone():
                            raise StorageError(
                                "SQLite migration found inconsistent relationships. The"
                                " upgrade was rolled back; restore a verified backup or"
                                " have the existing database repaired before retrying."
                            )
                        if self.db.execute("PRAGMA quick_check").fetchall() != [
                            ("ok",)
                        ]:
                            raise StorageError(
                                "SQLite migration found database integrity problems."
                                " The upgrade was rolled back; restore a verified"
                                " backup before retrying."
                            )
                        self.db.execute("PRAGMA user_version=5")
            except BaseException:
                if self._connection is not None:
                    self._connection.close()
                    self._connection = None
                handle.close()
                self._file_lock = None
                raise

        await self._run(initialize)

    async def close(self) -> None:
        def finish() -> None:
            try:
                if self._connection is not None:
                    self._connection.close()
            finally:
                self._connection = None
                if self._file_lock is not None:
                    self._file_lock.close()
                    self._file_lock = None

        await self._run(finish)

    async def check_health(self) -> int:
        """Check integrity and write access without committing any application data."""

        def check() -> int:
            # Serialized with other operations, including cancellation. Never emit
            # SQLite's detailed integrity report: it may contain roster data.
            if self.db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise StorageError(
                    "SQLite startup integrity check failed. Stop the bot and restore a"
                    " verified backup; do not delete the database."
                )
            if self.db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise StorageError(
                    "SQLite startup relationship check failed. Stop the bot and restore"
                    " a verified backup or have the database repaired."
                )
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            # A read alone would miss query-only databases and lost write access.
            # The upsert is rolled back even if a previous diagnostic key exists.
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "INSERT INTO metadata VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET"
                    " value=excluded.value",
                    ("startup:write-probe", "check"),
                )
            finally:
                self.db.rollback()
            return version

        return await self._run(check)

    async def roles(self, guild_id: int) -> dict[str, int]:
        return await self._run(
            lambda: dict(
                self.db.execute(
                    "SELECT class_id, role_id FROM roles WHERE guild_id=?", (guild_id,)
                ).fetchall()
            )
        )

    async def associate(self, guild_id: int, class_id: str, role_id: int) -> None:
        def save() -> None:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                if self.db.execute(
                    "SELECT 1 FROM gameplay_roles WHERE guild_id=? AND role_id=?",
                    (guild_id, role_id),
                ).fetchone():
                    raise StorageError(
                        "Class roles and gameplay tags must use separate Discord roles."
                    )
                self.db.execute("INSERT OR IGNORE INTO guilds VALUES (?)", (guild_id,))
                self.db.execute(
                    "INSERT INTO roles VALUES (?, ?, ?) ON CONFLICT(guild_id, class_id)"
                    " DO UPDATE SET role_id=excluded.role_id",
                    (guild_id, class_id, role_id),
                )

        await self._run(save)

    async def gameplay_roles(self, guild_id: int) -> dict[str, int]:
        return await self._run(
            lambda: dict(
                self.db.execute(
                    "SELECT gameplay_role, role_id FROM gameplay_roles WHERE"
                    " guild_id=?",
                    (guild_id,),
                ).fetchall()
            )
        )

    async def associate_gameplay_role(
        self, guild_id: int, gameplay_role: str, role_id: int
    ) -> None:
        def save() -> None:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                if self.db.execute(
                    "SELECT 1 FROM roles WHERE guild_id=? AND role_id=?",
                    (guild_id, role_id),
                ).fetchone():
                    raise StorageError(
                        "Class roles and gameplay tags must use separate Discord roles."
                    )
                self.db.execute("INSERT OR IGNORE INTO guilds VALUES (?)", (guild_id,))
                self.db.execute(
                    "INSERT INTO gameplay_roles VALUES (?, ?, ?) ON CONFLICT(guild_id,"
                    " gameplay_role) DO UPDATE SET role_id=excluded.role_id",
                    (guild_id, gameplay_role, role_id),
                )

        await self._run(save)

    async def panel(self, guild_id: int) -> Panel | None:
        def read() -> Panel | None:
            row = self.db.execute(
                "SELECT guild_id, channel_id, message_id FROM panels WHERE guild_id=?",
                (guild_id,),
            ).fetchone()
            return Panel(*row) if row else None

        return await self._run(read)

    async def save_panel(self, panel: Panel) -> None:
        def save() -> None:
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO guilds VALUES (?)", (panel.guild_id,)
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO retired_panels SELECT guild_id, channel_id,"
                    " message_id FROM panels WHERE guild_id=? AND message_id != ?",
                    (panel.guild_id, panel.message_id),
                )
                self.db.execute(
                    "INSERT INTO panels VALUES (?, ?, ?) ON CONFLICT(guild_id) DO"
                    " UPDATE SET channel_id=excluded.channel_id,"
                    " message_id=excluded.message_id",
                    (panel.guild_id, panel.channel_id, panel.message_id),
                )

        await self._run(save)

    async def retired_panels(self, guild_id: int) -> list[Panel]:
        return await self._run(
            lambda: [
                Panel(*row)
                for row in self.db.execute(
                    "SELECT guild_id, channel_id, message_id FROM retired_panels WHERE"
                    " guild_id=?",
                    (guild_id,),
                ).fetchall()
            ]
        )

    async def forget_retired(self, message_id: int) -> None:
        def delete() -> None:
            with self.db:
                self.db.execute(
                    "DELETE FROM retired_panels WHERE message_id=?", (message_id,)
                )

        await self._run(delete)

    async def get_meta(self, key: str) -> str | None:
        def read() -> str | None:
            row = self.db.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else None

        return await self._run(read)

    async def set_meta(self, key: str, value: str) -> None:
        def save() -> None:
            with self.db:
                self.db.execute(
                    "INSERT INTO metadata VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET"
                    " value=excluded.value",
                    (key, value),
                )

        await self._run(save)
