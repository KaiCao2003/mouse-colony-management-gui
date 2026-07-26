"""SQLite persistence and domain operations for cages and individual mice."""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import string
import threading
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

_UNSET: Final[object] = object()
_SEXES: Final[frozenset[str]] = frozenset({"M", "F", "U"})
_CAGE_STATUSES: Final[frozenset[str]] = frozenset({"active", "inactive", "on_order"})
_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z]-\d{4}$")
_CAGE_VIEWS: Final[frozenset[str]] = frozenset({"all", "stock", "using", "single", "breeding"})
SCHEMA = """
CREATE TABLE IF NOT EXISTS cages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cage_card_id TEXT NOT NULL UNIQUE COLLATE NOCASE,
    family_letter TEXT NOT NULL CHECK(length(family_letter) = 1),
    status TEXT NOT NULL CHECK(status IN ('active', 'inactive', 'on_order')),
    room TEXT,
    is_breeding_pair INTEGER NOT NULL DEFAULT 0 CHECK(is_breeding_pair IN (0, 1)),
    protocol TEXT,
    note TEXT,
    source_cage_id INTEGER REFERENCES cages(id),
    creation_type TEXT NOT NULL DEFAULT 'manual',
    on_census_date TEXT,
    off_census_date TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS animals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    legacy_id TEXT,
    cage_id INTEGER NOT NULL REFERENCES cages(id),
    family_letter TEXT NOT NULL CHECK(length(family_letter) = 1),
    sex TEXT NOT NULL DEFAULT 'U' CHECK(sex IN ('M', 'F', 'U')),
    dob TEXT,
    genotype TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'inactive')),
    inactive_by_cage INTEGER NOT NULL DEFAULT 0 CHECK(inactive_by_cage IN (0, 1)),
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS animals_cage_status_idx ON animals(cage_id, status);
CREATE INDEX IF NOT EXISTS animals_legacy_id_idx ON animals(legacy_id);

CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    animal_id INTEGER NOT NULL REFERENCES animals(id),
    from_cage_id INTEGER REFERENCES cages(id),
    to_cage_id INTEGER REFERENCES cages(id),
    movement_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS cage_tags (
    cage_id INTEGER NOT NULL REFERENCES cages(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (cage_id, tag_id)
);

CREATE TABLE IF NOT EXISTS operators (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS surgery_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS surgeries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    animal_id INTEGER NOT NULL REFERENCES animals(id) ON DELETE CASCADE,
    surgery_date TEXT NOT NULL,
    surgery_time TEXT,
    operator_id INTEGER NOT NULL REFERENCES operators(id),
    surgery_type_id INTEGER NOT NULL REFERENCES surgery_types(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS surgeries_animal_date_idx
ON surgeries(animal_id, surgery_date, surgery_time);

CREATE TRIGGER IF NOT EXISTS surgeries_max_four
BEFORE INSERT ON surgeries
WHEN (SELECT COUNT(*) FROM surgeries WHERE animal_id = NEW.animal_id) >= 4
BEGIN
    SELECT RAISE(ABORT, 'A mouse can have at most 4 surgery records.');
END;
"""


class Database:
    """Thread-safe local database facade returning plain dictionaries."""

    def __init__(
        self,
        path: Path | str,
        *,
        room_aliases: Mapping[str, str] | None = None,
        breeding_rooms: Collection[str] | None = None,
    ) -> None:
        self.path = Path(path)
        self._room_aliases = {
            room.strip(): alias.strip()
            for room, alias in (room_aliases or {}).items()
            if room.strip() and alias.strip()
        }
        self._breeding_rooms = {
            room.strip().casefold() for room in (breeding_rooms or ()) if room.strip()
        }
        self._breeding_rooms.update(
            alias.casefold()
            for room, alias in self._room_aliases.items()
            if room.casefold() in self._breeding_rooms
        )
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def initialize(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            connection = sqlite3.connect(
                self.path,
                check_same_thread=False,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            for database_file in (
                self.path,
                Path(f"{self.path}-wal"),
                Path(f"{self.path}-shm"),
            ):
                if database_file.exists():
                    os.chmod(database_file, 0o600)
            connection.executescript(SCHEMA)
            cage_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(cages)").fetchall()
            }
            if "is_breeding_pair" not in cage_columns:
                connection.execute(
                    """
                    ALTER TABLE cages
                    ADD COLUMN is_breeding_pair INTEGER NOT NULL DEFAULT 0
                    CHECK(is_breeding_pair IN (0, 1))
                    """
                )
            breeding_cage_ids = [
                int(row["id"])
                for row in connection.execute(
                    "SELECT id, room FROM cages WHERE room IS NOT NULL"
                ).fetchall()
                if self._is_breeding_room(row["room"])
            ]
            connection.executemany(
                "UPDATE cages SET is_breeding_pair = 1 WHERE id = ?",
                ((cage_id,) for cage_id in breeding_cage_ids),
            )
            animal_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(animals)").fetchall()
            }
            if "inactive_by_cage" not in animal_columns:
                # Existing inactive records predate this marker, so leave them
                # independently inactive instead of risking an incorrect revival.
                connection.execute(
                    """
                    ALTER TABLE animals
                    ADD COLUMN inactive_by_cage INTEGER NOT NULL DEFAULT 0
                    CHECK(inactive_by_cage IN (0, 1))
                    """
                )
            for name in ("Headplate", "Probe implant", "Headplate + probe implant"):
                connection.execute(
                    "INSERT OR IGNORE INTO surgery_types(name) VALUES (?)",
                    (name,),
                )
            self._connection = connection

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Database.initialize() must be called first.")
        return self._connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self.connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _validate_date(value: str | None, field: str) -> str | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise ValueError(f"{field} must be a valid date.") from exc

    @staticmethod
    def _validate_time(value: str | None) -> str | None:
        if not value:
            return None
        candidate = value.strip()
        try:
            parsed = datetime.strptime(candidate, "%H:%M")
        except ValueError as exc:
            raise ValueError("Surgery time must use HH:MM.") from exc
        return parsed.strftime("%H:%M")

    @staticmethod
    def _validate_sex(value: str) -> str:
        candidate = value.strip().upper()
        if candidate not in _SEXES:
            raise ValueError("Sex must be M, F, or U.")
        return candidate

    @staticmethod
    def _clean(value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    def _is_breeding_room(self, room: str | None) -> bool:
        cleaned = self._clean(room)
        return cleaned is not None and cleaned.casefold() in self._breeding_rooms

    def _room_alias(self, room: str | None) -> str | None:
        cleaned = self._clean(room)
        if cleaned is None:
            return None
        for canonical_room, alias in self._room_aliases.items():
            if cleaned.casefold() == canonical_room.casefold():
                return alias
            if cleaned.casefold() == alias.casefold():
                return alias
        return None

    @staticmethod
    def _lastrowid(cursor: sqlite3.Cursor) -> int:
        value = cursor.lastrowid
        if value is None:
            raise RuntimeError("SQLite did not return an inserted row ID.")
        return value

    def count_cages(self) -> int:
        with self._lock:
            return int(self.connection.execute("SELECT COUNT(*) FROM cages").fetchone()[0])

    def is_empty(self) -> bool:
        return self.count_cages() == 0

    def _next_cage_card_id(self, connection: sqlite3.Connection) -> str:
        number = 1
        while True:
            candidate = f"LOCAL-{number:04d}"
            exists = connection.execute(
                "SELECT 1 FROM cages WHERE cage_card_id = ? COLLATE NOCASE",
                (candidate,),
            ).fetchone()
            if exists is None:
                return candidate
            number += 1

    def _allocate_letter(self, connection: sqlite3.Connection) -> str:
        counts = {
            row["family_letter"]: int(row["uses"])
            for row in connection.execute(
                """
                SELECT family_letter, COUNT(*) AS uses
                FROM cages
                WHERE status = 'active'
                GROUP BY family_letter
                """
            )
        }
        for letter in string.ascii_uppercase:
            if counts.get(letter, 0) == 0:
                return letter
        return min(string.ascii_uppercase, key=lambda letter: (counts.get(letter, 0), letter))

    def _public_id(self, connection: sqlite3.Connection, letter: str) -> str:
        for _ in range(200):
            candidate = f"{letter}-{secrets.randbelow(10_000):04d}"
            if not _ID_PATTERN.fullmatch(candidate):
                continue
            if (
                connection.execute(
                    "SELECT 1 FROM animals WHERE public_id = ?",
                    (candidate,),
                ).fetchone()
                is None
            ):
                return candidate
        for number in range(10_000):
            candidate = f"{letter}-{number:04d}"
            if (
                connection.execute(
                    "SELECT 1 FROM animals WHERE public_id = ?",
                    (candidate,),
                ).fetchone()
                is None
            ):
                return candidate
        raise ValueError(f"No mouse IDs remain for family {letter}.")

    def _insert_animal(
        self,
        connection: sqlite3.Connection,
        *,
        cage_id: int,
        family_letter: str,
        sex: str,
        dob: str | None,
        genotype: str | None,
        status: str,
        note: str | None,
        legacy_id: str | None = None,
        movement_type: str = "manual",
    ) -> int:
        public_id = self._public_id(connection, family_letter)
        cursor = connection.execute(
            """
            INSERT INTO animals(
                public_id, legacy_id, cage_id, family_letter, sex, dob,
                genotype, status, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                public_id,
                self._clean(legacy_id),
                cage_id,
                family_letter,
                sex,
                dob,
                self._clean(genotype),
                status,
                self._clean(note),
            ),
        )
        animal_id = self._lastrowid(cursor)
        connection.execute(
            """
            INSERT INTO movements(animal_id, from_cage_id, to_cage_id, movement_type)
            VALUES (?, NULL, ?, ?)
            """,
            (animal_id, cage_id, movement_type),
        )
        return animal_id

    def create_cage(
        self,
        *,
        cage_card_id: str | None = None,
        status: str = "active",
        animal_count: int = 0,
        sex: str = "U",
        dob: str | None = None,
        genotype: str | None = None,
        room: str | None = None,
        protocol: str | None = None,
        note: str | None = None,
        source_cage_id: int | None = None,
        creation_type: str = "manual",
        family_letter: str | None = None,
        on_census_date: str | None = None,
        off_census_date: str | None = None,
        is_breeding_pair: bool = False,
    ) -> int:
        if status not in _CAGE_STATUSES:
            raise ValueError("Cage status is invalid.")
        if animal_count < 0 or animal_count > 1000:
            raise ValueError("Animal count must be between 0 and 1000.")
        validated_sex = self._validate_sex(sex)
        validated_dob = self._validate_date(dob, "DOB")
        validated_on = self._validate_date(on_census_date, "On census date")
        validated_off = self._validate_date(off_census_date, "Off census date")
        if not isinstance(is_breeding_pair, bool):
            raise ValueError("Breeding-pair flag must be true or false.")
        cleaned_room = self._clean(room)
        breeding_pair = is_breeding_pair or self._is_breeding_room(cleaned_room)
        with self.transaction() as connection:
            identifier = self._clean(cage_card_id) or self._next_cage_card_id(connection)
            letter = family_letter or self._allocate_letter(connection)
            letter = letter.strip().upper()
            if len(letter) != 1 or letter not in string.ascii_uppercase:
                raise ValueError("Family letter must be A through Z.")
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO cages(
                        cage_card_id, family_letter, status, room, is_breeding_pair,
                        protocol, note,
                        source_cage_id, creation_type, on_census_date, off_census_date
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        letter,
                        status,
                        cleaned_room,
                        int(breeding_pair),
                        self._clean(protocol),
                        self._clean(note),
                        source_cage_id,
                        creation_type,
                        validated_on,
                        validated_off,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Cage card ID {identifier} already exists.") from exc
            cage_id = self._lastrowid(cursor)
            animal_status = "active" if status == "active" else "inactive"
            for _ in range(animal_count):
                self._insert_animal(
                    connection,
                    cage_id=cage_id,
                    family_letter=letter,
                    sex=validated_sex,
                    dob=validated_dob,
                    genotype=genotype,
                    status=animal_status,
                    note=None,
                    movement_type=creation_type,
                )
            return cage_id

    def add_animals(
        self,
        cage_id: int,
        *,
        count: int,
        sex: str = "U",
        dob: str | None = None,
        genotype: str | None = None,
        note: str | None = None,
    ) -> list[dict[str, Any]]:
        if count < 1 or count > 100:
            raise ValueError("Number of mice must be between 1 and 100.")
        validated_sex = self._validate_sex(sex)
        validated_dob = self._validate_date(dob, "DOB")
        created: list[int] = []
        with self.transaction() as connection:
            cage = connection.execute("SELECT * FROM cages WHERE id = ?", (cage_id,)).fetchone()
            if cage is None:
                raise ValueError("Cage not found.")
            if cage["status"] != "active":
                raise ValueError("Reactivate the cage before adding mice.")
            for _ in range(count):
                created.append(
                    self._insert_animal(
                        connection,
                        cage_id=cage_id,
                        family_letter=cage["family_letter"],
                        sex=validated_sex,
                        dob=validated_dob,
                        genotype=genotype,
                        status="active",
                        note=note,
                    )
                )
        results: list[dict[str, Any]] = []
        for animal_id in created:
            animal = self.get_animal(animal_id)
            if animal is not None:
                results.append(animal)
        return results

    def split_cage(
        self,
        source_cage_id: int,
        *,
        animal_ids: Sequence[int],
        cage_card_id: str | None = None,
        room: str | None = None,
        protocol: str | None = None,
        note: str | None = None,
    ) -> int:
        selected = list(dict.fromkeys(animal_ids))
        if not selected:
            raise ValueError("Select at least one active mouse.")
        with self.transaction() as connection:
            source = connection.execute(
                "SELECT * FROM cages WHERE id = ?",
                (source_cage_id,),
            ).fetchone()
            if source is None:
                raise ValueError("Source cage not found.")
            placeholders = ",".join("?" for _ in selected)
            rows = connection.execute(
                f"""
                SELECT id FROM animals
                WHERE cage_id = ? AND status = 'active' AND id IN ({placeholders})
                """,
                (source_cage_id, *selected),
            ).fetchall()
            if len(rows) != len(selected):
                raise ValueError("Every selected mouse must be active in the source cage.")
            identifier = self._clean(cage_card_id) or self._next_cage_card_id(connection)
            destination_room = self._clean(room) if room is not None else source["room"]
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO cages(
                        cage_card_id, family_letter, status, room, is_breeding_pair,
                        protocol, note,
                        source_cage_id, creation_type
                    ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, 'split')
                    """,
                    (
                        identifier,
                        source["family_letter"],
                        destination_room,
                        int(self._is_breeding_room(destination_room)),
                        self._clean(protocol) if protocol is not None else source["protocol"],
                        self._clean(note),
                        source_cage_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Cage card ID {identifier} already exists.") from exc
            destination_id = self._lastrowid(cursor)
            for animal_id in selected:
                connection.execute(
                    "UPDATE animals SET cage_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (destination_id, animal_id),
                )
                connection.execute(
                    """
                    INSERT INTO movements(
                        animal_id, from_cage_id, to_cage_id, movement_type
                    ) VALUES (?, ?, ?, 'split')
                    """,
                    (animal_id, source_cage_id, destination_id),
                )
            remaining = connection.execute(
                "SELECT COUNT(*) FROM animals WHERE cage_id = ? AND status = 'active'",
                (source_cage_id,),
            ).fetchone()[0]
            if remaining == 0:
                connection.execute(
                    """
                    UPDATE cages
                    SET status = 'inactive', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (source_cage_id,),
                )
            return destination_id

    def wean_cage(
        self,
        source_cage_id: int,
        *,
        count: int,
        sex: str,
        dob: str | None,
        genotype: str | None,
        cage_card_id: str | None = None,
        room: str | None = None,
        protocol: str | None = None,
        note: str | None = None,
    ) -> int:
        source = self.get_cage(source_cage_id)
        if source is None:
            raise ValueError("Source cage not found.")
        return self.create_cage(
            cage_card_id=cage_card_id,
            status="active",
            animal_count=count,
            sex=sex,
            dob=dob,
            genotype=genotype,
            room=room if room is not None else source.get("room"),
            protocol=protocol if protocol is not None else source.get("protocol"),
            note=note,
            source_cage_id=source_cage_id,
            creation_type="wean",
        )

    def set_imported_split_lineage(
        self,
        destination_cage_id: int,
        source_cage_id: int,
    ) -> dict[str, Any]:
        """Reconcile an imported destination cage with its split-cage lineage.

        Imported rows initially receive independent family letters because the
        CSV is authoritative for cage census data but does not encode lineage.
        This operation safely applies the later Excel relationship without
        changing an animal's four-digit suffix unless that resulting public ID
        is already in use.
        """

        if destination_cage_id == source_cage_id:
            raise ValueError("Source and destination cages must be different.")

        with self.transaction() as connection:
            cages = {
                int(row["id"]): row
                for row in connection.execute(
                    "SELECT * FROM cages WHERE id IN (?, ?)",
                    (destination_cage_id, source_cage_id),
                ).fetchall()
            }
            if source_cage_id not in cages:
                raise ValueError("Source cage not found.")
            if destination_cage_id not in cages:
                raise ValueError("Destination cage not found.")

            source_letter = str(cages[source_cage_id]["family_letter"]).upper()
            if len(source_letter) != 1 or source_letter not in string.ascii_uppercase:
                raise ValueError("Source cage family letter must be A through Z.")

            connection.execute(
                """
                UPDATE cages
                SET source_cage_id = ?, creation_type = 'split', family_letter = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (source_cage_id, source_letter, destination_cage_id),
            )

            animals = connection.execute(
                "SELECT id, public_id FROM animals WHERE cage_id = ? ORDER BY id",
                (destination_cage_id,),
            ).fetchall()
            for animal in animals:
                animal_id = int(animal["id"])
                current_public_id = str(animal["public_id"])
                match = _ID_PATTERN.fullmatch(current_public_id)
                suffix = match.group(0)[-4:] if match else None
                candidate = f"{source_letter}-{suffix}" if suffix is not None else None
                if candidate != current_public_id:
                    collision = (
                        connection.execute(
                            "SELECT 1 FROM animals WHERE public_id = ? AND id != ?",
                            (candidate, animal_id),
                        ).fetchone()
                        if candidate is not None
                        else True
                    )
                    if collision:
                        candidate = self._public_id(connection, source_letter)
                assert candidate is not None
                connection.execute(
                    """
                    UPDATE animals
                    SET public_id = ?, family_letter = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (candidate, source_letter, animal_id),
                )
                connection.execute(
                    """
                    INSERT INTO movements(
                        animal_id, from_cage_id, to_cage_id, movement_type
                    )
                    SELECT ?, ?, ?, 'imported_split'
                    WHERE NOT EXISTS (
                        SELECT 1 FROM movements
                        WHERE animal_id = ? AND from_cage_id = ? AND to_cage_id = ?
                          AND movement_type = 'imported_split'
                    )
                    """,
                    (
                        animal_id,
                        source_cage_id,
                        destination_cage_id,
                        animal_id,
                        source_cage_id,
                        destination_cage_id,
                    ),
                )

        result = self.get_cage(destination_cage_id)
        assert result is not None
        return result

    def update_cage(
        self,
        cage_id: int,
        *,
        room: str | None | object = _UNSET,
        protocol: str | None | object = _UNSET,
        note: str | None | object = _UNSET,
        is_breeding_pair: bool | object = _UNSET,
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            cage = connection.execute("SELECT * FROM cages WHERE id = ?", (cage_id,)).fetchone()
            if cage is None:
                raise ValueError("Cage not found.")
            assignments: list[str] = []
            values: list[Any] = []
            for column, value in (("room", room), ("protocol", protocol), ("note", note)):
                if value is _UNSET:
                    continue
                assignments.append(f"{column} = ?")
                values.append(self._clean(value if isinstance(value, str) else None))

            resulting_room = (
                self._clean(room if isinstance(room, str) else None)
                if room is not _UNSET
                else cage["room"]
            )
            if is_breeding_pair is not _UNSET and not isinstance(is_breeding_pair, bool):
                raise ValueError("Breeding-pair flag must be true or false.")
            if self._is_breeding_room(resulting_room):
                resulting_breeding_pair = True
            elif is_breeding_pair is not _UNSET:
                resulting_breeding_pair = bool(is_breeding_pair)
            else:
                resulting_breeding_pair = bool(cage["is_breeding_pair"])
            if int(resulting_breeding_pair) != int(cage["is_breeding_pair"]):
                assignments.append("is_breeding_pair = ?")
                values.append(int(resulting_breeding_pair))

            if assignments:
                values.append(cage_id)
                connection.execute(
                    f"""
                    UPDATE cages
                    SET {", ".join(assignments)}, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    values,
                )
        result = self.get_cage(cage_id)
        assert result is not None
        return result

    def update_animal(
        self,
        animal_id: int,
        *,
        legacy_id: str | None | object = _UNSET,
        sex: str | object = _UNSET,
        dob: str | None | object = _UNSET,
        genotype: str | None | object = _UNSET,
        note: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        assignments: list[str] = []
        values: list[Any] = []
        if legacy_id is not _UNSET:
            assignments.append("legacy_id = ?")
            values.append(self._clean(legacy_id if isinstance(legacy_id, str) else None))
        if sex is not _UNSET:
            if not isinstance(sex, str):
                raise ValueError("Sex must be M, F, or U.")
            assignments.append("sex = ?")
            values.append(self._validate_sex(sex))
        if dob is not _UNSET:
            assignments.append("dob = ?")
            values.append(self._validate_date(dob if isinstance(dob, str) else None, "DOB"))
        if genotype is not _UNSET:
            assignments.append("genotype = ?")
            values.append(self._clean(genotype if isinstance(genotype, str) else None))
        if note is not _UNSET:
            assignments.append("note = ?")
            values.append(self._clean(note if isinstance(note, str) else None))
        with self.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM animals WHERE id = ?", (animal_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("Mouse not found.")
            if assignments:
                values.append(animal_id)
                connection.execute(
                    f"""
                    UPDATE animals
                    SET {", ".join(assignments)}, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    values,
                )
        result = self.get_animal(animal_id)
        assert result is not None
        return result

    def toggle_animal(self, animal_id: int) -> dict[str, Any]:
        with self.transaction() as connection:
            animal = connection.execute(
                "SELECT * FROM animals WHERE id = ?",
                (animal_id,),
            ).fetchone()
            if animal is None:
                raise ValueError("Mouse not found.")
            status = "inactive" if animal["status"] == "active" else "active"
            if status == "active":
                connection.execute(
                    """
                    UPDATE cages
                    SET status = 'active', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (animal["cage_id"],),
                )
                # Reactivating one mouse also makes its cage active, but it must
                # not leave stale cage-level markers that could revive other
                # inactive mice during a later cage toggle.
                connection.execute(
                    "UPDATE animals SET inactive_by_cage = 0 WHERE cage_id = ?",
                    (animal["cage_id"],),
                )
            connection.execute(
                """
                UPDATE animals
                SET status = ?, inactive_by_cage = 0, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, animal_id),
            )
        result = self.get_animal(animal_id)
        assert result is not None
        return result

    def toggle_cage(self, cage_id: int) -> dict[str, Any]:
        with self.transaction() as connection:
            cage = connection.execute("SELECT * FROM cages WHERE id = ?", (cage_id,)).fetchone()
            if cage is None:
                raise ValueError("Cage not found.")
            next_status = "inactive" if cage["status"] == "active" else "active"
            connection.execute(
                "UPDATE cages SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (next_status, cage_id),
            )
            if next_status == "inactive":
                connection.execute(
                    """
                    UPDATE animals
                    SET status = 'inactive', inactive_by_cage = 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE cage_id = ? AND status = 'active'
                    """,
                    (cage_id,),
                )
            else:
                connection.execute(
                    """
                    UPDATE animals
                    SET status = 'active', inactive_by_cage = 0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE cage_id = ? AND inactive_by_cage = 1
                    """,
                    (cage_id,),
                )
        result = self.get_cage(cage_id)
        assert result is not None
        return result

    def add_tag(self, cage_id: int, name: str) -> dict[str, Any]:
        clean_name = self._clean(name)
        if not clean_name:
            raise ValueError("Tag name is required.")
        if len(clean_name) > 40:
            raise ValueError("Tag name must be 40 characters or fewer.")
        with self.transaction() as connection:
            if (
                connection.execute("SELECT 1 FROM cages WHERE id = ?", (cage_id,)).fetchone()
                is None
            ):
                raise ValueError("Cage not found.")
            connection.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (clean_name,))
            tag = connection.execute(
                "SELECT id, name FROM tags WHERE name = ? COLLATE NOCASE",
                (clean_name,),
            ).fetchone()
            connection.execute(
                "INSERT OR IGNORE INTO cage_tags(cage_id, tag_id) VALUES (?, ?)",
                (cage_id, tag["id"]),
            )
            return dict(tag)

    def remove_tag(self, cage_id: int, tag_id: int) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM cage_tags WHERE cage_id = ? AND tag_id = ?",
                (cage_id, tag_id),
            )

    def list_tags(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(row)
                for row in self.connection.execute(
                    "SELECT id, name FROM tags ORDER BY name COLLATE NOCASE"
                ).fetchall()
            ]

    @staticmethod
    def _lookup_id(connection: sqlite3.Connection, table: str, name: str) -> int:
        connection.execute(f"INSERT OR IGNORE INTO {table}(name) VALUES (?)", (name,))
        row = connection.execute(
            f"SELECT id FROM {table} WHERE name = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
        return int(row["id"])

    def add_surgery(
        self,
        animal_id: int,
        *,
        surgery_date: str,
        surgery_time: str | None,
        operator: str,
        surgery_type: str,
    ) -> int | None:
        validated_date = self._validate_date(surgery_date, "Surgery date")
        assert validated_date is not None
        validated_time = self._validate_time(surgery_time)
        operator_name = self._clean(operator)
        type_name = self._clean(surgery_type)
        if not operator_name:
            raise ValueError("Operator is required.")
        if not type_name:
            raise ValueError("Surgery type is required.")
        with self.transaction() as connection:
            if (
                connection.execute("SELECT 1 FROM animals WHERE id = ?", (animal_id,)).fetchone()
                is None
            ):
                raise ValueError("Mouse not found.")
            operator_id = self._lookup_id(connection, "operators", operator_name)
            type_id = self._lookup_id(connection, "surgery_types", type_name)
            duplicate = connection.execute(
                """
                SELECT id FROM surgeries
                WHERE animal_id = ? AND surgery_date = ?
                  AND COALESCE(surgery_time, '') = COALESCE(?, '')
                  AND operator_id = ? AND surgery_type_id = ?
                """,
                (animal_id, validated_date, validated_time, operator_id, type_id),
            ).fetchone()
            if duplicate is not None:
                return None
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO surgeries(
                        animal_id, surgery_date, surgery_time, operator_id, surgery_type_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (animal_id, validated_date, validated_time, operator_id, type_id),
                )
            except sqlite3.IntegrityError as exc:
                if "at most 4" in str(exc):
                    raise ValueError(
                        "This mouse already has the maximum 4 surgery records."
                    ) from exc
                raise
            return self._lastrowid(cursor)

    def list_operators(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(row)
                for row in self.connection.execute(
                    "SELECT id, name FROM operators ORDER BY name COLLATE NOCASE"
                ).fetchall()
            ]

    def list_surgery_types(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(row)
                for row in self.connection.execute(
                    "SELECT id, name FROM surgery_types ORDER BY name COLLATE NOCASE"
                ).fetchall()
            ]

    @staticmethod
    def _age_display(dob: str | None) -> str:
        if not dob:
            return "Unknown"
        try:
            born = date.fromisoformat(dob)
        except ValueError:
            return "Unknown"
        days = (date.today() - born).days
        if days < 0:
            return "Not born yet"
        if days < 14:
            return f"{days} days"
        if days < 84:
            return f"{days // 7} weeks"
        months = days // 30
        if months < 24:
            return f"{months} months"
        return f"{months // 12}y {months % 12}m"

    def _surgeries(self, animal_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT s.id, s.surgery_date, s.surgery_time,
                   o.name AS operator, t.name AS surgery_type
            FROM surgeries s
            JOIN operators o ON o.id = s.operator_id
            JOIN surgery_types t ON t.id = s.surgery_type_id
            WHERE s.animal_id = ?
            ORDER BY s.surgery_date, COALESCE(s.surgery_time, ''), s.id
            """,
            (animal_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_animal(self, animal_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM animals WHERE id = ?",
                (animal_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["age_display"] = self._age_display(result.get("dob"))
            result["surgeries"] = self._surgeries(animal_id)
            return result

    def list_animals(
        self,
        cage_id: int,
        *,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock:
            where = "cage_id = ?" if include_inactive else "cage_id = ? AND status = 'active'"
            rows = self.connection.execute(
                f"SELECT * FROM animals WHERE {where} ORDER BY status, public_id",
                (cage_id,),
            ).fetchall()
            results: list[dict[str, Any]] = []
            for row in rows:
                result = dict(row)
                result["age_display"] = self._age_display(result.get("dob"))
                result["surgeries"] = self._surgeries(int(result["id"]))
                results.append(result)
            return results

    def _tags_for_cage(self, cage_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT t.id, t.name FROM tags t
            JOIN cage_tags ct ON ct.tag_id = t.id
            WHERE ct.cage_id = ? ORDER BY t.name COLLATE NOCASE
            """,
            (cage_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _cage_record(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        cage_id = int(result["id"])
        result["is_breeding_pair"] = bool(result["is_breeding_pair"])
        result["room_alias"] = self._room_alias(result.get("room"))
        aggregate = self.connection.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_count,
                COUNT(*) AS total_count,
                SUM(CASE WHEN status = 'active' AND sex = 'M' THEN 1 ELSE 0 END)
                    AS male_count,
                SUM(CASE WHEN status = 'active' AND sex = 'F' THEN 1 ELSE 0 END)
                    AS female_count,
                SUM(CASE WHEN status = 'active' AND sex = 'U' THEN 1 ELSE 0 END)
                    AS unknown_count,
                COUNT(DISTINCT CASE WHEN status = 'active' THEN sex END) AS sex_count,
                MIN(CASE WHEN status = 'active' THEN sex END) AS one_sex,
                COUNT(DISTINCT CASE WHEN status = 'active' AND genotype IS NOT NULL
                                    THEN genotype END) AS genotype_count,
                MIN(CASE WHEN status = 'active' THEN genotype END) AS one_genotype
            FROM animals WHERE cage_id = ?
            """,
            (cage_id,),
        ).fetchone()
        result["active_count"] = int(aggregate["active_count"] or 0)
        result["total_count"] = int(aggregate["total_count"] or 0)
        result["male_count"] = int(aggregate["male_count"] or 0)
        result["female_count"] = int(aggregate["female_count"] or 0)
        result["unknown_count"] = int(aggregate["unknown_count"] or 0)
        if aggregate["sex_count"] == 1:
            result["sex"] = {"M": "Male", "F": "Female", "U": "Unknown"}.get(
                aggregate["one_sex"], "Unknown"
            )
        elif aggregate["sex_count"] > 1:
            result["sex"] = "Mixed"
        else:
            result["sex"] = None
        if aggregate["genotype_count"] == 1:
            result["genotype"] = aggregate["one_genotype"]
        elif aggregate["genotype_count"] > 1:
            result["genotype"] = "Mixed"
        else:
            result["genotype"] = None
        result["tags"] = self._tags_for_cage(cage_id)
        return result

    def get_cage(self, cage_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT c.*, source.cage_card_id AS source_cage_card_id
                FROM cages c LEFT JOIN cages source ON source.id = c.source_cage_id
                WHERE c.id = ?
                """,
                (cage_id,),
            ).fetchone()
            return None if row is None else self._cage_record(row)

    def list_cages(
        self,
        *,
        search: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        view: str = "all",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if view not in _CAGE_VIEWS:
            raise ValueError("Cage view is invalid.")
        canonical_view = "using" if view == "single" else view
        if canonical_view != "all":
            clauses.append("c.status = 'active'")
            if canonical_view == "breeding":
                clauses.append("c.is_breeding_pair = 1")
            else:
                clauses.append("c.is_breeding_pair = 0")
                comparator = "> 1" if canonical_view == "stock" else "= 1"
                clauses.append(
                    f"""(
                        SELECT COUNT(*) FROM animals view_animals
                        WHERE view_animals.cage_id = c.id
                          AND view_animals.status = 'active'
                    ) {comparator}"""
                )
        if status:
            if status not in _CAGE_STATUSES:
                raise ValueError("Cage status filter is invalid.")
            clauses.append("c.status = ?")
            values.append(status)
        if search:
            clauses.append(
                """(
                    c.cage_card_id LIKE ? OR c.room LIKE ? OR c.protocol LIKE ?
                    OR c.note LIKE ? OR EXISTS (
                        SELECT 1 FROM animals a WHERE a.cage_id = c.id AND (
                            a.public_id LIKE ? OR a.legacy_id LIKE ? OR a.genotype LIKE ?
                        )
                    )
                )"""
            )
            pattern = f"%{search}%"
            values.extend([pattern] * 7)
        if tag:
            clauses.append(
                """EXISTS (
                    SELECT 1 FROM cage_tags ct JOIN tags t ON t.id = ct.tag_id
                    WHERE ct.cage_id = c.id AND t.name = ? COLLATE NOCASE
                )"""
            )
            values.append(tag)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self.connection.execute(
                f"""
                SELECT c.*, source.cage_card_id AS source_cage_card_id
                FROM cages c LEFT JOIN cages source ON source.id = c.source_cage_id
                {where}
                ORDER BY CASE c.status WHEN 'active' THEN 0 WHEN 'on_order' THEN 1 ELSE 2 END,
                         c.cage_card_id COLLATE NOCASE
                """,
                values,
            ).fetchall()
            return [self._cage_record(row) for row in rows]

    def summary(self) -> dict[str, int]:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_cages,
                    SUM(CASE WHEN status = 'inactive' THEN 1 ELSE 0 END) AS inactive_cages
                FROM cages
                """
            ).fetchone()
            active_animals = self.connection.execute(
                "SELECT COUNT(*) FROM animals WHERE status = 'active'"
            ).fetchone()[0]
            surgeries = self.connection.execute("SELECT COUNT(*) FROM surgeries").fetchone()[0]
            stock = self.connection.execute(
                """
                SELECT
                    COUNT(*) AS stock_mice,
                    SUM(CASE WHEN a.sex = 'M' THEN 1 ELSE 0 END) AS stock_male,
                    SUM(CASE WHEN a.sex = 'F' THEN 1 ELSE 0 END) AS stock_female,
                    SUM(CASE WHEN a.sex = 'U' THEN 1 ELSE 0 END) AS stock_unknown
                FROM animals a
                JOIN cages c ON c.id = a.cage_id
                WHERE a.status = 'active'
                  AND c.status = 'active'
                  AND c.is_breeding_pair = 0
                  AND (
                      SELECT COUNT(*) FROM animals cage_animals
                      WHERE cage_animals.cage_id = c.id
                        AND cage_animals.status = 'active'
                  ) > 1
                """
            ).fetchone()
            return {
                "active_cages": int(row["active_cages"] or 0),
                "inactive_cages": int(row["inactive_cages"] or 0),
                "active_animals": int(active_animals),
                "stock_mice": int(stock["stock_mice"] or 0),
                "stock_male": int(stock["stock_male"] or 0),
                "stock_female": int(stock["stock_female"] or 0),
                "stock_unknown": int(stock["stock_unknown"] or 0),
                "surgeries": int(surgeries),
            }
