from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from app.database import SCHEMA, Database

TEST_ROOM_ALIASES = {
    "ROOM-REGULAR": "Regular Cycle room",
    "ROOM-REVERSE": "Reverse Cycle room",
    "ROOM-BREEDING": "Breeding Core",
}
TEST_BREEDING_ROOMS = {"ROOM-BREEDING", "Breeding Core"}


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(
        tmp_path / "mouseline.db",
        room_aliases=TEST_ROOM_ALIASES,
        breeding_rooms=TEST_BREEDING_ROOMS,
    )
    value.initialize()
    try:
        yield value
    finally:
        value.close()


def test_create_cage_generates_individual_ids_with_one_family_letter(
    database: Database,
) -> None:
    cage_id = database.create_cage(
        cage_card_id="CC00000001",
        animal_count=4,
        sex="F",
        dob="2026-06-01",
        genotype="WT",
    )

    cage = database.get_cage(cage_id)
    animals = database.list_animals(cage_id, include_inactive=True)

    assert cage is not None
    assert cage["active_count"] == 4
    assert cage["total_count"] == 4
    assert {animal["family_letter"] for animal in animals} == {cage["family_letter"]}
    assert len({animal["public_id"] for animal in animals}) == 4
    assert all(re.fullmatch(r"[A-Z]-\d{4}", animal["public_id"]) for animal in animals)
    assert {animal["sex"] for animal in animals} == {"F"}


def test_cage_active_sex_counts_distinguish_unknown_from_mixed_sexes(
    database: Database,
) -> None:
    female_unknown_id = database.create_cage(
        cage_card_id="SEX-FEMALE-UNKNOWN",
        animal_count=3,
    )
    female_unknown_animals = database.list_animals(female_unknown_id)
    database.update_animal(female_unknown_animals[0]["id"], sex="F")
    database.update_animal(female_unknown_animals[2]["id"], sex="M")
    database.toggle_animal(female_unknown_animals[2]["id"])

    male_female_id = database.create_cage(
        cage_card_id="SEX-MALE-FEMALE",
        animal_count=2,
    )
    male_female_animals = database.list_animals(male_female_id)
    database.update_animal(male_female_animals[0]["id"], sex="M")
    database.update_animal(male_female_animals[1]["id"], sex="F")

    female_unknown = database.get_cage(female_unknown_id)
    male_female = database.get_cage(male_female_id)
    assert female_unknown is not None and male_female is not None
    assert female_unknown["sex"] == "Mixed"
    assert {
        "male": female_unknown["male_count"],
        "female": female_unknown["female_count"],
        "unknown": female_unknown["unknown_count"],
    } == {"male": 0, "female": 1, "unknown": 1}
    assert male_female["sex"] == "Mixed"
    assert {
        "male": male_female["male_count"],
        "female": male_female["female_count"],
        "unknown": male_female["unknown_count"],
    } == {"male": 1, "female": 1, "unknown": 0}


def test_room_aliases_and_breeding_pair_rules_preserve_protocol(database: Database) -> None:
    regular_id = database.create_cage(
        cage_card_id="ROOM-REGULAR",
        room="ROOM-REGULAR",
        protocol="HIDDEN-PROTOCOL",
    )
    reverse_id = database.create_cage(cage_card_id="CAGE-REVERSE", room="ROOM-REVERSE")
    core_id = database.create_cage(cage_card_id="ROOM-CORE", room="ROOM-BREEDING")
    alias_id = database.create_cage(cage_card_id="ROOM-ALIAS", room="breeding core")
    manual_id = database.create_cage(
        cage_card_id="ROOM-MANUAL",
        room="Elsewhere",
        is_breeding_pair=True,
    )

    regular = database.get_cage(regular_id)
    reverse = database.get_cage(reverse_id)
    core = database.get_cage(core_id)
    alias = database.get_cage(alias_id)
    manual = database.get_cage(manual_id)
    assert regular is not None and reverse is not None and core is not None
    assert alias is not None and manual is not None
    assert regular["room_alias"] == "Regular Cycle room"
    assert reverse["room_alias"] == "Reverse Cycle room"
    assert core["room_alias"] == "Breeding Core"
    assert alias["room_alias"] == "Breeding Core"
    assert core["is_breeding_pair"] is True
    assert alias["is_breeding_pair"] is True
    assert manual["is_breeding_pair"] is True

    forced = database.update_cage(
        regular_id,
        room="ROOM-BREEDING",
        is_breeding_pair=False,
    )
    assert forced["is_breeding_pair"] is True
    assert forced["protocol"] == "HIDDEN-PROTOCOL"
    unmarked = database.update_cage(
        regular_id,
        room="ROOM-REGULAR",
        is_breeding_pair=False,
    )
    assert unmarked["is_breeding_pair"] is False
    assert unmarked["protocol"] == "HIDDEN-PROTOCOL"


def test_cage_details_can_be_revised_without_changing_lineage_or_protocol(
    database: Database,
) -> None:
    original_id = database.create_cage(
        cage_card_id="DETAILS-ORIGINAL",
        room="ROOM-REGULAR",
        protocol="HIDDEN-PROTOCOL",
        note="old note",
        on_census_date="2026-01-01",
        animal_count=1,
    )
    database.create_cage(cage_card_id="DETAILS-TAKEN")
    before = database.get_cage(original_id)
    assert before is not None

    updated = database.update_cage(
        original_id,
        cage_card_id="DETAILS-REVISED",
        room="ROOM-REVERSE",
        note="new note",
        on_census_date="2026-02-03",
        off_census_date="2026-04-05",
        is_breeding_pair=True,
    )

    assert updated["cage_card_id"] == "DETAILS-REVISED"
    assert updated["room"] == "ROOM-REVERSE"
    assert updated["note"] == "new note"
    assert updated["on_census_date"] == "2026-02-03"
    assert updated["off_census_date"] == "2026-04-05"
    assert updated["is_breeding_pair"] is True
    assert updated["protocol"] == "HIDDEN-PROTOCOL"
    assert updated["family_letter"] == before["family_letter"]
    assert updated["source_cage_id"] == before["source_cage_id"]

    with pytest.raises(ValueError, match="already exists"):
        database.update_cage(original_id, cage_card_id="details-taken")
    with pytest.raises(ValueError, match="On census date must be a valid date"):
        database.update_cage(original_id, on_census_date="not-a-date")
    with pytest.raises(ValueError, match="Off census date cannot be earlier"):
        database.update_cage(original_id, on_census_date="2026-05-01")
    with pytest.raises(ValueError, match="Room must be 100 characters or fewer"):
        database.update_cage(original_id, room="R" * 101)
    with pytest.raises(ValueError, match="Note must be 4000 characters or fewer"):
        database.update_cage(original_id, note="N" * 4001)

    cleared = database.update_cage(
        original_id,
        cage_card_id="details-revised",
        room=None,
        note=None,
        on_census_date=None,
        off_census_date=None,
        is_breeding_pair=False,
    )
    assert cleared["cage_card_id"] == "details-revised"
    assert cleared["room"] is None
    assert cleared["note"] is None
    assert cleared["on_census_date"] is None
    assert cleared["off_census_date"] is None
    assert cleared["is_breeding_pair"] is False


def test_protocol_is_not_searchable(database: Database) -> None:
    database.create_cage(
        cage_card_id="PROTOCOL-HIDDEN",
        protocol="PRIVATE-SEARCH-TOKEN",
    )

    assert database.list_cages(search="PRIVATE-SEARCH-TOKEN") == []


def test_initialize_migrates_existing_breeding_core_cages(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    old_schema = SCHEMA.replace(
        "    is_breeding_pair INTEGER NOT NULL DEFAULT 0 CHECK(is_breeding_pair IN (0, 1)),\n",
        "",
    )
    connection = sqlite3.connect(path)
    connection.executescript(old_schema)
    connection.execute(
        """
        INSERT INTO cages(cage_card_id, family_letter, status, room, protocol)
        VALUES ('LEGACY-CORE', 'A', 'active', 'ROOM-BREEDING', 'KEEP-ME')
        """
    )
    connection.execute(
        """
        INSERT INTO cages(cage_card_id, family_letter, status, room)
        VALUES ('LEGACY-REGULAR', 'B', 'active', 'ROOM-REGULAR')
        """
    )
    connection.commit()
    connection.close()

    migrated = Database(
        path,
        room_aliases=TEST_ROOM_ALIASES,
        breeding_rooms=TEST_BREEDING_ROOMS,
    )
    migrated.initialize()
    try:
        cages = {cage["cage_card_id"]: cage for cage in migrated.list_cages()}
        columns = {row["name"] for row in migrated.connection.execute("PRAGMA table_info(cages)")}
        assert "is_breeding_pair" in columns
        assert cages["LEGACY-CORE"]["is_breeding_pair"] is True
        assert cages["LEGACY-CORE"]["protocol"] == "KEEP-ME"
        assert cages["LEGACY-REGULAR"]["is_breeding_pair"] is False
    finally:
        migrated.close()


def test_stock_summary_and_cage_views_use_current_active_counts(database: Database) -> None:
    stock_id = database.create_cage(cage_card_id="VIEW-STOCK", animal_count=3)
    single_id = database.create_cage(cage_card_id="VIEW-SINGLE", animal_count=1)
    breeding_id = database.create_cage(
        cage_card_id="VIEW-BREEDING",
        room="ROOM-BREEDING",
        animal_count=2,
    )
    manual_breeding_id = database.create_cage(
        cage_card_id="VIEW-MANUAL-BREEDING",
        room="ROOM-REGULAR",
        animal_count=3,
        is_breeding_pair=True,
    )
    inactive_id = database.create_cage(
        cage_card_id="VIEW-INACTIVE",
        status="inactive",
        animal_count=5,
    )
    stock_animals = database.list_animals(stock_id)
    database.update_animal(stock_animals[0]["id"], sex="M")
    database.update_animal(stock_animals[1]["id"], sex="F")

    summary = database.summary()
    assert {
        "stock_mice": summary["stock_mice"],
        "stock_male": summary["stock_male"],
        "stock_female": summary["stock_female"],
        "stock_unknown": summary["stock_unknown"],
    } == {
        "stock_mice": 3,
        "stock_male": 1,
        "stock_female": 1,
        "stock_unknown": 1,
    }
    assert {cage["id"] for cage in database.list_cages(view="stock")} == {stock_id}
    using_ids = {cage["id"] for cage in database.list_cages(view="using")}
    single_alias_ids = {cage["id"] for cage in database.list_cages(view="single")}
    assert using_ids == single_alias_ids == {single_id}
    assert {cage["id"] for cage in database.list_cages(view="breeding")} == {
        breeding_id,
        manual_breeding_id,
    }
    assert {cage["id"] for cage in database.list_cages(view="all")} >= {
        stock_id,
        single_id,
        breeding_id,
        manual_breeding_id,
        inactive_id,
    }

    for animal in database.list_animals(stock_id)[:2]:
        database.toggle_animal(animal["id"])
    reduced_summary = database.summary()
    assert {
        reduced_summary["stock_mice"],
        reduced_summary["stock_male"],
        reduced_summary["stock_female"],
        reduced_summary["stock_unknown"],
    } == {0}
    assert stock_id not in {cage["id"] for cage in database.list_cages(view="stock")}
    using_ids = {cage["id"] for cage in database.list_cages(view="using")}
    single_alias_ids = {cage["id"] for cage in database.list_cages(view="single")}
    assert using_ids == single_alias_ids == {single_id, stock_id}

    with pytest.raises(ValueError, match="view is invalid"):
        database.list_cages(view="unknown")


def test_active_home_cages_use_every_letter_before_reusing(database: Database) -> None:
    cage_ids = [database.create_cage(cage_card_id=f"HOME-{index:02d}") for index in range(27)]
    letters = [database.get_cage(cage_id)["family_letter"] for cage_id in cage_ids]  # type: ignore[index]

    assert letters[:26] == list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    assert letters[26] == "A"


def test_active_split_cage_keeps_family_letter_reserved(database: Database) -> None:
    source_id = database.create_cage(cage_card_id="ROOT-A", animal_count=2)
    original_letter = database.get_cage(source_id)["family_letter"]  # type: ignore[index]
    animals = database.list_animals(source_id)

    split_id = database.split_cage(
        source_id,
        animal_ids=[animal["id"] for animal in animals],
        cage_card_id="SPLIT-A",
    )
    unrelated_id = database.create_cage(cage_card_id="UNRELATED")

    split = database.get_cage(split_id)
    unrelated = database.get_cage(unrelated_id)
    assert split is not None and unrelated is not None
    assert split["family_letter"] == original_letter
    assert unrelated["family_letter"] != original_letter


def test_split_retains_ids_and_family_and_updates_counts(database: Database) -> None:
    source_id = database.create_cage(cage_card_id="CC00000002", animal_count=3)
    source = database.get_cage(source_id)
    animals = database.list_animals(source_id)
    selected = animals[:2]

    destination_id = database.split_cage(
        source_id,
        animal_ids=[animal["id"] for animal in selected],
        cage_card_id="CC00000003",
    )

    source_after = database.get_cage(source_id)
    destination = database.get_cage(destination_id)
    moved = database.list_animals(destination_id)
    assert source is not None and source_after is not None and destination is not None
    assert source_after["active_count"] == 1
    assert destination["active_count"] == 2
    assert destination["family_letter"] == source["family_letter"]
    assert {mouse["public_id"] for mouse in moved} == {mouse["public_id"] for mouse in selected}


def test_split_all_mice_marks_source_inactive(database: Database) -> None:
    source_id = database.create_cage(cage_card_id="CC00000004", animal_count=2)
    animals = database.list_animals(source_id)
    database.split_cage(
        source_id,
        animal_ids=[animal["id"] for animal in animals],
        cage_card_id="CC00000005",
    )
    source = database.get_cage(source_id)
    assert source is not None
    assert source["status"] == "inactive"
    assert source["active_count"] == 0


def test_wean_creates_new_family_without_changing_source_count(database: Database) -> None:
    source_id = database.create_cage(cage_card_id="BREED-1", animal_count=2)
    source = database.get_cage(source_id)

    destination_id = database.wean_cage(
        source_id,
        count=5,
        sex="M",
        dob="2026-07-01",
        genotype="WT",
        cage_card_id="WEAN-1",
    )

    source_after = database.get_cage(source_id)
    destination = database.get_cage(destination_id)
    pups = database.list_animals(destination_id)
    assert source is not None and source_after is not None and destination is not None
    assert source_after["active_count"] == 2
    assert destination["active_count"] == 5
    assert destination["family_letter"] != source["family_letter"]
    assert {pup["family_letter"] for pup in pups} == {destination["family_letter"]}
    assert {pup["dob"] for pup in pups} == {"2026-07-01"}


def test_imported_split_lineage_preserves_suffixes_and_is_idempotent(
    database: Database,
) -> None:
    source_id = database.create_cage(cage_card_id="IMPORT-SOURCE", animal_count=1)
    destination_id = database.create_cage(cage_card_id="IMPORT-DEST", animal_count=2)
    source = database.get_cage(source_id)
    destination_before = database.get_cage(destination_id)
    animals_before = database.list_animals(destination_id, include_inactive=True)
    assert source is not None and destination_before is not None
    assert source["family_letter"] != destination_before["family_letter"]
    suffixes = {animal["id"]: animal["public_id"][-4:] for animal in animals_before}
    source_animal = database.list_animals(source_id)[0]
    source_suffix = next(
        f"{number:04d}" for number in range(10_000) if f"{number:04d}" not in suffixes.values()
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE animals SET public_id = ? WHERE id = ?",
            (f"{source['family_letter']}-{source_suffix}", source_animal["id"]),
        )

    destination = database.set_imported_split_lineage(destination_id, source_id)
    animals_after = database.list_animals(destination_id, include_inactive=True)
    first_ids = {animal["id"]: animal["public_id"] for animal in animals_after}

    assert destination["source_cage_id"] == source_id
    assert destination["creation_type"] == "split"
    assert destination["family_letter"] == source["family_letter"]
    assert {animal["family_letter"] for animal in animals_after} == {source["family_letter"]}
    assert {animal["public_id"][-4:] for animal in animals_after} == set(suffixes.values())
    assert all(re.fullmatch(r"[A-Z]-\d{4}", animal["public_id"]) for animal in animals_after)

    database.set_imported_split_lineage(destination_id, source_id)
    animals_repeated = database.list_animals(destination_id, include_inactive=True)
    repeated_ids = {animal["id"]: animal["public_id"] for animal in animals_repeated}
    movements = database.connection.execute(
        """
        SELECT animal_id, COUNT(*) AS count FROM movements
        WHERE from_cage_id = ? AND to_cage_id = ? AND movement_type = 'imported_split'
        GROUP BY animal_id
        """,
        (source_id, destination_id),
    ).fetchall()
    assert repeated_ids == first_ids
    assert {row["animal_id"]: row["count"] for row in movements} == {
        animal["id"]: 1 for animal in animals_after
    }


def test_imported_split_lineage_reallocates_only_colliding_public_id(
    database: Database,
) -> None:
    source_id = database.create_cage(cage_card_id="COLLISION-SOURCE", animal_count=1)
    destination_id = database.create_cage(cage_card_id="COLLISION-DEST", animal_count=1)
    source = database.get_cage(source_id)
    destination = database.get_cage(destination_id)
    source_animal = database.list_animals(source_id)[0]
    destination_animal = database.list_animals(destination_id)[0]
    assert source is not None and destination is not None
    colliding_suffix = source_animal["public_id"][-4:]
    with database.transaction() as connection:
        connection.execute(
            "UPDATE animals SET public_id = ? WHERE id = ?",
            (f"{destination['family_letter']}-{colliding_suffix}", destination_animal["id"]),
        )

    database.set_imported_split_lineage(destination_id, source_id)
    updated = database.get_animal(destination_animal["id"])

    assert updated is not None
    assert updated["family_letter"] == source["family_letter"]
    assert re.fullmatch(rf"{source['family_letter']}-\d{{4}}", updated["public_id"])
    assert updated["public_id"] != source_animal["public_id"]


def test_imported_split_lineage_rejects_same_or_missing_cages(database: Database) -> None:
    cage_id = database.create_cage(cage_card_id="LINEAGE-VALIDATE", animal_count=1)

    with pytest.raises(ValueError, match="must be different"):
        database.set_imported_split_lineage(cage_id, cage_id)
    with pytest.raises(ValueError, match="Source cage not found"):
        database.set_imported_split_lineage(cage_id, 999_999)
    with pytest.raises(ValueError, match="Destination cage not found"):
        database.set_imported_split_lineage(999_999, cage_id)


def test_inactive_is_a_simple_reversible_toggle(database: Database) -> None:
    cage_id = database.create_cage(cage_card_id="CC00000006", animal_count=2)
    animal_id = database.list_animals(cage_id)[0]["id"]

    assert database.toggle_animal(animal_id)["status"] == "inactive"
    assert database.get_cage(cage_id)["active_count"] == 1  # type: ignore[index]
    assert database.toggle_animal(animal_id)["status"] == "active"

    assert database.toggle_cage(cage_id)["status"] == "inactive"
    assert database.get_cage(cage_id)["active_count"] == 0  # type: ignore[index]
    assert database.toggle_cage(cage_id)["status"] == "active"
    assert database.get_cage(cage_id)["active_count"] == 2  # type: ignore[index]


def test_cage_reactivation_preserves_individually_inactive_mice(database: Database) -> None:
    cage_id = database.create_cage(cage_card_id="PRESERVE-INACTIVE", animal_count=2)
    animals = database.list_animals(cage_id)
    independently_inactive_id = animals[0]["id"]
    other_id = animals[1]["id"]

    database.toggle_animal(independently_inactive_id)
    database.toggle_cage(cage_id)
    database.toggle_cage(cage_id)

    independently_inactive = database.get_animal(independently_inactive_id)
    other = database.get_animal(other_id)
    cage = database.get_cage(cage_id)
    assert independently_inactive is not None and other is not None and cage is not None
    assert independently_inactive["status"] == "inactive"
    assert other["status"] == "active"
    assert cage["active_count"] == 1


def test_activating_imported_inactive_cage_does_not_revive_animals(
    database: Database,
) -> None:
    cage_id = database.create_cage(
        cage_card_id="IMPORTED-INACTIVE",
        status="inactive",
        animal_count=2,
        creation_type="import",
    )

    cage = database.toggle_cage(cage_id)
    animals = database.list_animals(cage_id, include_inactive=True)

    assert cage["status"] == "active"
    assert cage["active_count"] == 0
    assert {animal["status"] for animal in animals} == {"inactive"}


def test_surgery_is_simple_shared_and_limited_to_four(database: Database) -> None:
    cage_id = database.create_cage(cage_card_id="CC00000007", animal_count=1)
    animal_id = database.list_animals(cage_id)[0]["id"]

    for index in range(4):
        database.add_surgery(
            animal_id,
            surgery_date=f"2026-07-{index + 1:02d}",
            surgery_time="09:30",
            operator="Test Operator",
            surgery_type="Headplate" if index < 3 else "Custom type",
        )

    with pytest.raises(ValueError, match="maximum 4"):
        database.add_surgery(
            animal_id,
            surgery_date="2026-07-05",
            surgery_time=None,
            operator="Test Operator",
            surgery_type="Probe implant",
        )

    animal = database.get_animal(animal_id)
    assert animal is not None
    assert len(animal["surgeries"]) == 4
    assert {item["name"] for item in database.list_operators()} == {"Test Operator"}
    assert "Custom type" in {item["name"] for item in database.list_surgery_types()}


def test_tags_are_shared_and_filter_cages(database: Database) -> None:
    first = database.create_cage(cage_card_id="CC00000008")
    database.create_cage(cage_card_id="CC00000009")
    database.add_tag(first, "experiment-a")

    cages = database.list_cages(tag="experiment-a")
    assert [cage["id"] for cage in cages] == [first]
