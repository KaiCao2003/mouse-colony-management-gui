from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.database import Database


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    value = Database(
        tmp_path / "variables.db",
        room_aliases={"ROOM-BREEDING": "Breeding Core", "ROOM-REGULAR": "Regular Cycle"},
        breeding_rooms={"ROOM-BREEDING", "Breeding Core"},
    )
    value.initialize()
    try:
        yield value
    finally:
        value.close()


def option_id(database: Database, category: str, name: str) -> int:
    return next(
        option["id"]
        for option in database.list_variable_options(category)
        if option["name"] == name
    )


def test_catalog_seeds_existing_values_once_and_deletes_only_options(database: Database) -> None:
    cage_id = database.create_cage(
        cage_card_id="CATALOG", animal_count=1, genotype="+", mouse_user="Alice", room="Other room"
    )
    mouse = database.list_animals(cage_id)[0]
    database.add_surgery(
        mouse["id"],
        surgery_date="2026-09-01",
        surgery_time=None,
        operator="Surgeon",
        surgery_type="Headplate",
    )

    assert [row["name"] for row in database.list_variable_options("genotype")] == ["+"]
    assert [row["name"] for row in database.list_variable_options("mouse_user")] == ["Alice"]
    assert [row["name"] for row in database.list_variable_options("operator")] == ["Surgeon"]
    assert {row["name"] for row in database.list_variable_options("room")} == {
        "Other room",
        "ROOM-BREEDING",
        "ROOM-REGULAR",
    }

    genotype_id = option_id(database, "genotype", "+")
    database.delete_variable_option(genotype_id)
    database.initialize_variable_options()
    assert database.list_variable_options("genotype") == []
    assert database.get_animal(mouse["id"])["genotype"] == "+"
    assert (
        database.connection.execute(
            "SELECT 1 FROM variable_options WHERE id = ?", (genotype_id,)
        ).fetchone()
        is None
    )
    database.close()
    database.initialize()
    assert database.list_variable_options("genotype") == []
    replacement_id = database.add_variable_option("genotype", "+")
    assert replacement_id != genotype_id
    assert database.list_variable_options("genotype")[0]["id"] == replacement_id


def test_genotype_rename_is_case_sensitive_and_updates_inactive_mice(database: Database) -> None:
    first_cage = database.create_cage(animal_count=2, genotype="Cre+")
    first, second = database.list_animals(first_cage)
    database.toggle_animal(second["id"])
    second_cage = database.create_cage(animal_count=1, genotype="cre+")
    untouched = database.list_animals(second_cage)[0]
    genotype_id = option_id(database, "genotype", "Cre+")

    database.rename_variable_option(genotype_id, "Cre positive")

    assert database.get_animal(first["id"])["genotype"] == "Cre positive"
    assert database.get_animal(second["id"])["genotype"] == "Cre positive"
    assert database.get_animal(untouched["id"])["genotype"] == "cre+"
    assert {row["name"] for row in database.list_variable_options("genotype")} == {
        "Cre positive",
        "cre+",
    }


def test_user_rename_updates_case_variants_and_room_rename_survives_restart(
    database: Database,
) -> None:
    cage_id = database.create_cage(animal_count=1, mouse_user="Alice", room="ROOM-BREEDING")
    second_cage = database.create_cage(animal_count=1, mouse_user="alice", room="room-breeding")
    database.rename_variable_option(option_id(database, "mouse_user", "Alice"), "A. Smith")
    database.rename_variable_option(option_id(database, "room", "ROOM-BREEDING"), "Breeding annex")
    for current_cage in (cage_id, second_cage):
        assert database.list_animals(current_cage)[0]["mouse_user"] == "A. Smith"
        cage = database.get_cage(current_cage)
        assert cage["room"] == "Breeding annex"
        assert cage["room_alias"] == "Breeding Core"
        assert cage["is_breeding_pair"] == 1

    database.close()
    database.initialize()
    added = database.create_cage(room="Breeding annex")
    assert database.get_cage(added)["is_breeding_pair"] == 1
    assert database.get_cage(added)["room_alias"] == "Breeding Core"
    assert "ROOM-BREEDING" not in {row["name"] for row in database.list_variable_options("room")}


def test_surgery_renames_keep_lookup_ids_and_do_not_recreate_defaults(database: Database) -> None:
    cage_id = database.create_cage(animal_count=1)
    mouse = database.list_animals(cage_id)[0]
    surgery_id = database.add_surgery(
        mouse["id"],
        surgery_date="2026-09-01",
        surgery_time="10:00",
        operator="Alice",
        surgery_type="Headplate",
    )
    before = dict(
        database.connection.execute(
            "SELECT * FROM surgeries WHERE id = ?", (surgery_id,)
        ).fetchone()
    )
    database.rename_variable_option(option_id(database, "operator", "Alice"), "A. Smith")
    database.rename_variable_option(
        option_id(database, "surgery_type", "Headplate"), "Headplate placement"
    )
    after = dict(
        database.connection.execute(
            "SELECT * FROM surgeries WHERE id = ?", (surgery_id,)
        ).fetchone()
    )
    assert before == after
    assert database.get_surgery(surgery_id)["operator"] == "A. Smith"
    assert database.get_surgery(surgery_id)["surgery_type"] == "Headplate placement"

    database.close()
    database.initialize()
    assert "Headplate" not in {row["name"] for row in database.list_surgery_types()}


def test_add_validation_and_duplicate_rename_are_atomic(database: Database) -> None:
    positive_id = database.add_variable_option("genotype", " + ")
    negative_id = database.add_variable_option("genotype", "-")
    assert positive_id != negative_id
    database.add_variable_option("genotype", "WT")
    database.add_variable_option("genotype", "wt")
    database.add_variable_option("operator", "Alice")
    assert {row["name"] for row in database.list_operators()} == {"Alice"}

    for category, name in (("genotype", "+"), ("operator", "alice")):
        with pytest.raises(ValueError, match="already exists"):
            database.add_variable_option(category, name)
    with pytest.raises(ValueError, match="already exists"):
        database.rename_variable_option(positive_id, "-")
    assert option_id(database, "genotype", "+") == positive_id
    for name in ("", " " * 5, "x" * 101):
        with pytest.raises(ValueError):
            database.add_variable_option("genotype", name)
    with pytest.raises(ValueError, match="Unknown variable category"):
        database.add_variable_option("unknown", "name")
    with pytest.raises(ValueError, match="not found"):
        database.rename_variable_option(999999, "name")
    with pytest.raises(ValueError, match="not found"):
        database.delete_variable_option(999999)


def test_saving_same_name_keeps_colony_timestamp(
    database: Database,
) -> None:
    cage_id = database.create_cage(animal_count=1, genotype="WT")
    mouse = database.list_animals(cage_id)[0]
    genotype_id = option_id(database, "genotype", "WT")
    database.connection.execute(
        "UPDATE animals SET updated_at = '2020-01-01 00:00:00' WHERE id = ?", (mouse["id"],)
    )

    database.rename_variable_option(genotype_id, "WT")
    assert database.get_animal(mouse["id"])["updated_at"] == "2020-01-01 00:00:00"
    assert database.get_animal(mouse["id"])["genotype"] == "WT"


def test_reusing_old_room_name_does_not_inherit_the_renamed_room_settings(
    database: Database,
) -> None:
    database.rename_variable_option(option_id(database, "room", "ROOM-BREEDING"), "Breeding annex")
    database.add_variable_option("room", "ROOM-BREEDING")
    new_cage = database.create_cage(room="ROOM-BREEDING")
    renamed_cage = database.create_cage(room="Breeding annex")
    assert database.get_cage(new_cage)["room_alias"] is None
    assert database.get_cage(new_cage)["is_breeding_pair"] == 0
    assert database.get_cage(renamed_cage)["room_alias"] == "Breeding Core"
    assert database.get_cage(renamed_cage)["is_breeding_pair"] == 1
    database.close()
    reopened = Database(
        database.path,
        room_aliases={"ROOM-BREEDING": "Breeding Core"},
        breeding_rooms={"ROOM-BREEDING", "Breeding Core"},
    )
    reopened.initialize()
    try:
        assert reopened.get_cage(new_cage)["is_breeding_pair"] == 0
        assert reopened.get_cage(renamed_cage)["is_breeding_pair"] == 1
    finally:
        reopened.close()


def test_deleting_all_options_keeps_history_and_stays_empty_after_restart(
    database: Database,
) -> None:
    cage_id = database.create_cage(
        animal_count=1, genotype="WT", mouse_user="Alice", room="ROOM-BREEDING"
    )
    mouse_id = database.list_animals(cage_id)[0]["id"]
    surgery_id = database.add_surgery(
        mouse_id,
        surgery_date="2026-09-01",
        surgery_time=None,
        operator="Alice",
        surgery_type="Headplate",
    )
    database.rename_variable_option(option_id(database, "room", "ROOM-BREEDING"), "Breeding annex")
    tables = ("cages", "animals", "surgeries", "operators", "surgery_types")
    before = {
        table: [
            tuple(row) for row in database.connection.execute(f"SELECT * FROM {table} ORDER BY id")
        ]
        for table in tables
    }
    for category in ("genotype", "mouse_user", "surgery_type", "operator", "room"):
        for option in database.list_variable_options(category):
            database.delete_variable_option(option["id"])
    assert database.connection.execute("SELECT COUNT(*) FROM variable_options").fetchone()[0] == 0
    database.close()

    reopened = Database(
        database.path,
        room_aliases={"ROOM-BREEDING": "Breeding Core"},
        breeding_rooms={"ROOM-BREEDING", "Breeding Core"},
    )
    reopened.initialize()
    try:
        reopened.initialize_variable_options()
        assert (
            reopened.connection.execute("SELECT COUNT(*) FROM variable_options").fetchone()[0] == 0
        )
        after = {
            table: [
                tuple(row)
                for row in reopened.connection.execute(f"SELECT * FROM {table} ORDER BY id")
            ]
            for table in tables
        }
        assert after == before
        assert reopened.get_surgery(surgery_id)["operator"] == "Alice"
        assert reopened.get_cage(cage_id)["room_alias"] == "Breeding Core"
        assert reopened.get_cage(cage_id)["is_breeding_pair"] == 1
        reopened.add_variable_option("room", "Breeding annex")
        added = reopened.create_cage(room="Breeding annex")
        assert reopened.get_cage(added)["room_alias"] == "Breeding Core"
        assert reopened.get_cage(added)["is_breeding_pair"] == 1
    finally:
        reopened.close()


@pytest.mark.parametrize("all_disabled", [False, True])
def test_legacy_disabled_options_migrate_to_deletion_and_keep_room_history(
    database: Database, all_disabled: bool
) -> None:
    cage_id = database.create_cage(animal_count=1, genotype="WT", room="ROOM-BREEDING")
    database.rename_variable_option(option_id(database, "room", "ROOM-BREEDING"), "Breeding annex")
    room_id = option_id(database, "room", "Breeding annex")
    database.connection.execute(
        "UPDATE variable_options SET room_alias = 'Breeding Core', is_breeding_room = 1 "
        "WHERE id = ?",
        (room_id,),
    )
    if all_disabled:
        database.connection.execute("UPDATE variable_options SET enabled = 0")
    else:
        database.connection.execute(
            "UPDATE variable_options SET enabled = 0 WHERE id = ?", (room_id,)
        )
    # Recreate the previous release's catalog storage before opening the upgrade.
    database.connection.execute("DROP TABLE variable_catalog_state")
    database.connection.execute("DROP TABLE room_settings")
    database.close()
    database.initialize()
    assert (
        database.connection.execute(
            "SELECT 1 FROM variable_options WHERE id = ?", (room_id,)
        ).fetchone()
        is None
    )
    assert database.get_cage(cage_id)["room_alias"] == "Breeding Core"
    assert database.get_cage(cage_id)["is_breeding_pair"] == 1
    assert database.list_animals(cage_id)[0]["genotype"] == "WT"
    database.close()
    database.initialize()
    database.initialize_variable_options()
    if all_disabled:
        assert (
            database.connection.execute("SELECT COUNT(*) FROM variable_options").fetchone()[0] == 0
        )
    else:
        assert database.list_variable_options("genotype")[0]["name"] == "WT"
    assert database.get_cage(cage_id)["room_alias"] == "Breeding Core"


def test_room_rename_cannot_overwrite_deleted_room_history(database: Database) -> None:
    breeding_id = option_id(database, "room", "ROOM-BREEDING")
    regular_id = option_id(database, "room", "ROOM-REGULAR")
    database.delete_variable_option(breeding_id)
    with pytest.raises(ValueError, match="already has saved settings"):
        database.rename_variable_option(regular_id, "ROOM-BREEDING")
    assert option_id(database, "room", "ROOM-REGULAR") == regular_id
