"""Character rosters and a durable journal for coordinating Discord role changes."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .character_names import normalized_name_key, valid_component
from .character_roles import validate_gameplay_role
from .classes import REGISTRY, supports_gameplay_role
from .roles import UserError
from .storage import StorageError, Store


@dataclass(frozen=True, slots=True)
class Character:
    id: int
    guild_id: int
    user_id: int
    primary_name: str
    secondary_name: str
    class_id: str
    created_at: str
    updated_at: str
    is_main: bool
    legacy_name: str | None = None
    gameplay_role: str | None = None

    @property
    def full_name(self) -> str:
        return (
            f"{self.primary_name} {self.secondary_name}"
            if self.secondary_name
            else self.primary_name
        )

    @property
    def needs_completion(self) -> bool:
        return not self.secondary_name


@dataclass(frozen=True, slots=True)
class Roster:
    guild_id: int
    user_id: int
    characters: tuple[Character, ...]
    main_character_id: int | None

    @property
    def main(self) -> Character | None:
        return next(
            (
                character
                for character in self.characters
                if character.id == self.main_character_id
            ),
            None,
        )

    @property
    def alts(self) -> tuple[Character, ...]:
        return tuple(
            character for character in self.characters if not character.is_main
        )


@dataclass(frozen=True, slots=True)
class Operation:
    op_id: str
    guild_id: int
    user_id: int
    payload: dict[str, Any]
    previous_roles: frozenset[int]
    wanted_roles: frozenset[int]
    created_at: str


def configure_character_sql(db: sqlite3.Connection) -> None:
    """Register deterministic validation used by CHECK constraints on every open."""
    db.create_function("character_name_key", 2, normalized_name_key, deterministic=True)
    db.create_function("character_name_valid", 1, valid_component, deterministic=True)
    db.create_function(
        "character_role_valid", 2, supports_gameplay_role, deterministic=True
    )


def migrate_character_roles(db: sqlite3.Connection) -> None:
    """Keep existing characters and pending recovery plans; never infer a role."""
    db.execute(
        "ALTER TABLE characters ADD COLUMN gameplay_role TEXT "
        "CHECK(gameplay_role IS NULL OR character_role_valid(class_id, gameplay_role))"
    )


def migrate_characters(db: sqlite3.Connection) -> None:
    """Run within Store's existing migration transaction; never discard data."""
    legacy_rows: list[dict[str, Any]] = []
    existing = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='characters'"
    ).fetchone()
    if existing:
        columns = {row[1] for row in db.execute("PRAGMA table_info(characters)")}
        required = {"id", "guild_id", "user_id", "name", "class_id"}
        if not required <= columns or "primary_name" in columns:
            raise StorageError(
                "An unrecognized legacy characters schema was found. No character data was changed; restore a backup and use a compatible migration."
            )
        reserved = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('legacy_characters_v2', 'rosters', 'character_operations')"
        ).fetchone()
        if reserved:
            raise StorageError(
                "Legacy character tables conflict with the new schema. No data was changed; request a migration for this schema."
            )
        cursor = db.execute("SELECT * FROM characters")
        keys = [column[0] for column in cursor.description]
        legacy_rows = [dict(zip(keys, row, strict=True)) for row in cursor.fetchall()]
        seen_names: set[tuple[int, int, str]] = set()
        for row in legacy_rows:
            if (
                not isinstance(row["name"], str)
                or not row["name"]
                or not isinstance(row["class_id"], str)
                or not row["class_id"]
                or any(
                    not isinstance(row[key], int) or row[key] <= 0
                    for key in ("id", "guild_id", "user_id")
                )
            ):
                raise StorageError(
                    "Legacy characters contain unsupported identifiers or empty names. Migration was rolled back without deleting records."
                )
            key = (
                row["guild_id"],
                row["user_id"],
                normalized_name_key(row["name"], ""),
            )
            if key in seen_names:
                raise StorageError(
                    "Legacy characters contain duplicate normalized names in one roster. Migration was rolled back without deleting records; request a compatible migration or correct the original names from a backup."
                )
            seen_names.add(key)
        db.execute("ALTER TABLE characters RENAME TO legacy_characters_v2")
    db.execute("""CREATE TABLE rosters (
            guild_id INTEGER NOT NULL REFERENCES guilds(guild_id),
            user_id INTEGER NOT NULL CHECK(user_id > 0),
            main_character_id INTEGER,
            PRIMARY KEY(guild_id, user_id),
            FOREIGN KEY(main_character_id, guild_id, user_id)
                REFERENCES characters(id, guild_id, user_id)
                DEFERRABLE INITIALLY DEFERRED
        )""")
    db.execute("""CREATE TABLE characters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL REFERENCES guilds(guild_id),
            user_id INTEGER NOT NULL CHECK(user_id > 0),
            primary_name TEXT NOT NULL CHECK(length(primary_name) > 0),
            secondary_name TEXT NOT NULL,
            name_key TEXT NOT NULL,
            class_id TEXT NOT NULL CHECK(length(class_id) > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            legacy_name TEXT,
            UNIQUE(id, guild_id, user_id),
            UNIQUE(guild_id, user_id, name_key),
            CHECK(name_key = character_name_key(primary_name, secondary_name)),
            CHECK((secondary_name = '' AND legacy_name IS NOT NULL)
                OR (character_name_valid(primary_name) AND character_name_valid(secondary_name))),
            FOREIGN KEY(guild_id, user_id) REFERENCES rosters(guild_id, user_id)
                DEFERRABLE INITIALLY DEFERRED
        )""")
    db.execute(
        "CREATE INDEX characters_guild_class ON characters(guild_id, class_id, name_key)"
    )
    db.execute("""CREATE TABLE character_operations (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            op_id TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL,
            previous_roles TEXT NOT NULL,
            wanted_roles TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(guild_id, user_id),
            FOREIGN KEY(guild_id, user_id) REFERENCES rosters(guild_id, user_id)
                ON DELETE CASCADE
        )""")
    imported: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for row in legacy_rows:
        owner = (row["guild_id"], row["user_id"])
        db.execute("INSERT OR IGNORE INTO guilds VALUES (?)", (owner[0],))
        db.execute("INSERT OR IGNORE INTO rosters VALUES (?, ?, NULL)", owner)
        now = datetime.now(UTC).isoformat()
        db.execute(
            "INSERT INTO characters (id,guild_id,user_id,primary_name,secondary_name,name_key,class_id,created_at,updated_at,legacy_name) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"],
                *owner,
                row["name"],
                "",
                normalized_name_key(row["name"], ""),
                row["class_id"],
                row.get("created_at") or now,
                row.get("updated_at") or now,
                row["name"],
            ),
        )
        imported.setdefault(owner, []).append(row)
    for owner, rows in imported.items():
        mains = [row for row in rows if row.get("is_main") == 1]
        if len(mains) == 1:
            db.execute(
                "UPDATE rosters SET main_character_id=? WHERE guild_id=? AND user_id=?",
                (mains[0]["id"], *owner),
            )


class CharacterStore:
    def __init__(self, store: Store):
        self.store = store

    def _characters(self, where: str, parameters: tuple[Any, ...]) -> list[Character]:
        rows = self.store.db.execute(
            "SELECT c.id,c.guild_id,c.user_id,c.primary_name,c.secondary_name,c.class_id,c.created_at,c.updated_at,COALESCE(c.id=r.main_character_id,0),c.legacy_name,c.gameplay_role FROM characters c JOIN rosters r ON r.guild_id=c.guild_id AND r.user_id=c.user_id WHERE "
            + where
            + " ORDER BY c.id",
            parameters,
        ).fetchall()
        return [Character(*row[:8], bool(row[8]), row[9], row[10]) for row in rows]

    def _roster(self, guild_id: int, user_id: int) -> Roster:
        row = self.store.db.execute(
            "SELECT main_character_id FROM rosters WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
        characters = self._characters(
            "c.guild_id=? AND c.user_id=?", (guild_id, user_id)
        )
        return Roster(guild_id, user_id, tuple(characters), row[0] if row else None)

    async def roster(self, guild_id: int, user_id: int) -> Roster:
        return await self.store._run(lambda: self._roster(guild_id, user_id))

    def _character(self, guild_id: int, user_id: int, character_id: int) -> Character:
        matches = self._characters(
            "c.guild_id=? AND c.user_id=? AND c.id=?", (guild_id, user_id, character_id)
        )
        if not matches:
            raise UserError(
                "That character is not in your roster in this server. Refresh your character list."
            )
        return matches[0]

    async def character(
        self, guild_id: int, user_id: int, character_id: int
    ) -> Character:
        return await self.store._run(
            lambda: self._character(guild_id, user_id, character_id)
        )

    async def list_guild(
        self,
        guild_id: int,
        user_id: int | None = None,
        class_id: str | None = None,
        kind: str = "all",
    ) -> list[Character]:
        where, parameters = ["c.guild_id=?"], [guild_id]
        if user_id is not None:
            where.append("c.user_id=?")
            parameters.append(user_id)
        if class_id is not None:
            where.append("c.class_id=?")
            parameters.append(class_id)
        if kind in {"main", "mains"}:
            where.append("c.id=r.main_character_id")
        elif kind in {"alt", "alts"}:
            where.append("(r.main_character_id IS NULL OR c.id<>r.main_character_id)")
        elif kind != "all":
            raise UserError("Choose main, alt or all for the roster filter.")
        return await self.store._run(
            lambda: self._characters(" AND ".join(where), tuple(parameters))
        )

    def _pending(self, guild_id: int, user_id: int) -> Operation | None:
        row = self.store.db.execute(
            "SELECT op_id,payload,previous_roles,wanted_roles,created_at FROM character_operations WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
        if row is None:
            return None
        return Operation(
            row[0],
            guild_id,
            user_id,
            json.loads(row[1]),
            frozenset(json.loads(row[2])),
            frozenset(json.loads(row[3])),
            row[4],
        )

    async def pending(self, guild_id: int, user_id: int) -> Operation | None:
        return await self.store._run(lambda: self._pending(guild_id, user_id))

    def _validate(self, guild_id: int, user_id: int, payload: dict[str, Any]) -> None:
        roster = self._roster(guild_id, user_id)
        kind = payload.get("kind")
        if kind not in {"add", "edit", "promote", "remove", "purge"}:
            raise UserError("Unsupported character operation.")
        character = None
        if kind in {"edit", "promote", "remove"}:
            character = self._character(guild_id, user_id, payload.get("character_id"))
        if kind in {"add", "edit"}:
            primary, secondary = payload.get("primary_name"), payload.get(
                "secondary_name"
            )
            if (
                not isinstance(primary, str)
                or not isinstance(secondary, str)
                or not valid_component(primary)
                or not valid_component(secondary)
            ):
                raise UserError(
                    "Both name components must contain letters and optional accents."
                )
            if (
                unicodedata.normalize("NFC", primary) != primary
                or unicodedata.normalize("NFC", secondary) != secondary
            ):
                raise UserError(
                    "Character names must be safely normalized before saving."
                )
            if payload.get("name_key") != normalized_name_key(primary, secondary):
                raise UserError("The character name comparison key is invalid.")
            if payload.get("class_id") not in REGISTRY:
                raise UserError("That character class is unknown.")
            validate_gameplay_role(payload["class_id"], payload.get("gameplay_role"))
            if any(
                normalized_name_key(item.primary_name, item.secondary_name)
                == payload["name_key"]
                and (character is None or item.id != character.id)
                for item in roster.characters
            ):
                raise UserError(
                    "A character with that full name already exists in your roster."
                )
        if kind == "add":
            maximum = payload.get("max_characters", 0)
            if not isinstance(maximum, int) or maximum < 0:
                raise UserError("The character limit is invalid.")
            if maximum and len(roster.characters) >= maximum:
                raise UserError(
                    f"Your roster has reached its limit of {maximum} characters."
                )
        if kind == "promote" and character.needs_completion:
            raise UserError(
                "Complete this legacy character's two name fields with Edit Character before selecting it as main."
            )
        if kind == "remove" and character.is_main and len(roster.characters) > 1:
            replacement = self._character(
                guild_id, user_id, payload.get("replacement_id")
            )
            if replacement.id == character.id or replacement.needs_completion:
                raise UserError(
                    "Choose another completed character as the replacement main."
                )
        if (
            kind == "remove"
            and payload.get("replacement_id") is not None
            and (not character.is_main or len(roster.characters) == 1)
        ):
            raise UserError(
                "A replacement is only valid when removing a main that has alts."
            )

    async def prepare(
        self,
        guild_id: int,
        user_id: int,
        op_id: str,
        payload: dict[str, Any],
        previous_roles: set[int],
        wanted_roles: set[int],
    ) -> None:
        def save() -> None:
            with self.store.db:
                self.store.db.execute("BEGIN IMMEDIATE")
                existing = self._pending(guild_id, user_id)
                if existing:
                    if (
                        existing.op_id == op_id
                        and existing.payload == payload
                        and existing.previous_roles == previous_roles
                        and existing.wanted_roles == wanted_roles
                    ):
                        return
                    raise UserError(
                        "An interrupted character operation must be recovered before making another change. Open Manage Characters to retry recovery."
                    )
                self._validate(guild_id, user_id, payload)
                self.store.db.execute(
                    "INSERT OR IGNORE INTO guilds VALUES (?)", (guild_id,)
                )
                self.store.db.execute(
                    "INSERT OR IGNORE INTO rosters VALUES (?, ?, NULL)",
                    (guild_id, user_id),
                )
                self.store.db.execute(
                    "INSERT INTO character_operations VALUES (?,?,?,?,?,?,?)",
                    (
                        guild_id,
                        user_id,
                        op_id,
                        json.dumps(payload, ensure_ascii=False),
                        json.dumps(sorted(previous_roles)),
                        json.dumps(sorted(wanted_roles)),
                        datetime.now(UTC).isoformat(),
                    ),
                )

        await self.store._run(save)

    async def commit(self, guild_id: int, user_id: int, op_id: str) -> Roster:
        def save() -> Roster:
            with self.store.db:
                self.store.db.execute("BEGIN IMMEDIATE")
                operation = self._pending(guild_id, user_id)
                if operation is None:
                    # A committed operation can safely be acknowledged again.
                    return self._roster(guild_id, user_id)
                if operation.op_id != op_id:
                    raise UserError(
                        "A different character operation is pending. Refresh your roster before retrying."
                    )
                payload = operation.payload
                self._validate(guild_id, user_id, payload)
                roster = self._roster(guild_id, user_id)
                kind = payload["kind"]
                now = datetime.now(UTC).isoformat()
                if kind == "add":
                    cursor = self.store.db.execute(
                        "INSERT INTO characters(guild_id,user_id,primary_name,secondary_name,name_key,class_id,gameplay_role,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            guild_id,
                            user_id,
                            payload["primary_name"],
                            payload["secondary_name"],
                            payload["name_key"],
                            payload["class_id"],
                            payload["gameplay_role"],
                            now,
                            now,
                        ),
                    )
                    if roster.main is None:
                        self.store.db.execute(
                            "UPDATE rosters SET main_character_id=? WHERE guild_id=? AND user_id=?",
                            (cursor.lastrowid, guild_id, user_id),
                        )
                elif kind == "promote":
                    self.store.db.execute(
                        "UPDATE rosters SET main_character_id=? WHERE guild_id=? AND user_id=?",
                        (payload["character_id"], guild_id, user_id),
                    )
                elif kind == "edit":
                    self.store.db.execute(
                        "UPDATE characters SET primary_name=?, secondary_name=?, name_key=?, class_id=?, gameplay_role=?, updated_at=? WHERE id=? AND guild_id=? AND user_id=?",
                        (
                            payload["primary_name"],
                            payload["secondary_name"],
                            payload["name_key"],
                            payload["class_id"],
                            payload["gameplay_role"],
                            now,
                            payload["character_id"],
                            guild_id,
                            user_id,
                        ),
                    )
                elif kind == "remove":
                    if roster.main_character_id == payload["character_id"]:
                        self.store.db.execute(
                            "UPDATE rosters SET main_character_id=? WHERE guild_id=? AND user_id=?",
                            (payload.get("replacement_id"), guild_id, user_id),
                        )
                    self.store.db.execute(
                        "DELETE FROM characters WHERE id=? AND guild_id=? AND user_id=?",
                        (payload["character_id"], guild_id, user_id),
                    )
                    self._erase_legacy(guild_id, user_id, payload["character_id"])
                elif kind == "purge":
                    self.store.db.execute(
                        "UPDATE rosters SET main_character_id=NULL WHERE guild_id=? AND user_id=?",
                        (guild_id, user_id),
                    )
                    self.store.db.execute(
                        "DELETE FROM characters WHERE guild_id=? AND user_id=?",
                        (guild_id, user_id),
                    )
                    self._erase_legacy(guild_id, user_id)
                self.store.db.execute(
                    "DELETE FROM character_operations WHERE guild_id=? AND user_id=? AND op_id=?",
                    (guild_id, user_id, op_id),
                )
                self._delete_empty_roster(guild_id, user_id)
                result = self._roster(guild_id, user_id)
            return result

        return await self.store._run(save)

    def _delete_empty_roster(self, guild_id: int, user_id: int) -> None:
        self.store.db.execute(
            "DELETE FROM rosters WHERE guild_id=? AND user_id=? AND NOT EXISTS(SELECT 1 FROM characters WHERE guild_id=? AND user_id=?) AND NOT EXISTS(SELECT 1 FROM character_operations WHERE guild_id=? AND user_id=?)",
            (guild_id, user_id, guild_id, user_id, guild_id, user_id),
        )

    async def abort(self, guild_id: int, user_id: int, op_id: str) -> None:
        def discard() -> None:
            with self.store.db:
                self.store.db.execute(
                    "DELETE FROM character_operations WHERE guild_id=? AND user_id=? AND op_id=?",
                    (guild_id, user_id, op_id),
                )
                self._delete_empty_roster(guild_id, user_id)

        await self.store._run(discard)

    def _erase_legacy(
        self, guild_id: int, user_id: int, character_id: int | None = None
    ) -> None:
        if self.store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_characters_v2'"
        ).fetchone():
            if character_id is None:
                self.store.db.execute(
                    "DELETE FROM legacy_characters_v2 WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                )
            else:
                self.store.db.execute(
                    "DELETE FROM legacy_characters_v2 WHERE guild_id=? AND user_id=? AND id=?",
                    (guild_id, user_id, character_id),
                )

    async def erase(self, guild_id: int, user_id: int) -> None:
        """Authorized privacy deletion; intentionally does not perform Discord I/O."""

        def delete() -> None:
            with self.store.db:
                self.store.db.execute("BEGIN IMMEDIATE")
                self.store.db.execute(
                    "DELETE FROM character_operations WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                )
                self.store.db.execute(
                    "UPDATE rosters SET main_character_id=NULL WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                )
                self.store.db.execute(
                    "DELETE FROM characters WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                )
                self.store.db.execute(
                    "DELETE FROM rosters WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                )
                self._erase_legacy(guild_id, user_id)

        await self.store._run(delete)
