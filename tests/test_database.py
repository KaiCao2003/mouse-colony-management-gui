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


def test_mouse_user_threads_through_create_add_update_and_wean(database: Database) -> None:
    cage_id = database.create_cage(
        cage_card_id="USER-CREATE",
        animal_count=2,
        mouse_user="  Alice  ",
    )
    created = database.list_animals(cage_id, include_inactive=True)
    assert {animal["mouse_user"] for animal in created} == {"Alice"}
    cage = database.get_cage(cage_id)
    assert cage is not None
    assert cage["mouse_users"] == ["Alice"]
    assert cage["mouse_user"] == "Alice"

    added = database.add_animals(cage_id, count=1, mouse_user=" Bob ")
    assert len(added) == 1 and added[0]["mouse_user"] == "Bob"
    mixed = database.get_cage(cage_id)
    assert mixed is not None
    assert mixed["mouse_users"] == ["Alice", "Bob"]
    assert mixed["mouse_user"] == "Mixed"

    updated = database.update_animal(created[0]["id"], mouse_user=" Carol ")
    assert updated["mouse_user"] == "Carol"
    cleared = database.update_animal(created[0]["id"], mouse_user="   ")
    assert cleared["mouse_user"] is None

    wean_id = database.wean_cage(
        cage_id,
        count=2,
        sex="F",
        dob="2026-07-01",
        genotype="WT",
        mouse_user="Dana",
        cage_card_id="USER-WEAN",
    )
    assert {
        animal["mouse_user"] for animal in database.list_animals(wean_id, include_inactive=True)
    } == {"Dana"}

    with pytest.raises(ValueError, match="Mouse user must be 100 characters or fewer"):
        database.create_cage(animal_count=1, mouse_user="U" * 101)
    with pytest.raises(ValueError, match="Mouse user must be 100 characters or fewer"):
        database.update_animal(created[1]["id"], mouse_user="U" * 101)


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


def test_initialize_migrates_legacy_animals_with_mouse_user_and_index(tmp_path: Path) -> None:
    path = tmp_path / "legacy-mouse-user.db"
    old_schema = SCHEMA.replace("    mouse_user TEXT,\n", "")
    connection = sqlite3.connect(path)
    connection.executescript(old_schema)
    cage_id = connection.execute(
        """
        INSERT INTO cages(cage_card_id, family_letter, status)
        VALUES ('LEGACY-USER', 'A', 'active')
        """
    ).lastrowid
    connection.execute(
        """
        INSERT INTO animals(public_id, cage_id, family_letter, sex, status)
        VALUES ('A-0001', ?, 'A', 'F', 'active')
        """,
        (cage_id,),
    )
    connection.commit()
    connection.close()

    migrated = Database(path)
    migrated.initialize()
    try:
        columns = {row["name"] for row in migrated.connection.execute("PRAGMA table_info(animals)")}
        indexes = {row["name"] for row in migrated.connection.execute("PRAGMA index_list(animals)")}
        animal = migrated.get_animal(1)
        assert "mouse_user" in columns
        assert "animals_mouse_user_idx" in indexes
        assert animal is not None and animal["mouse_user"] is None
        assert migrated.update_animal(1, mouse_user="Legacy owner")["mouse_user"] == (
            "Legacy owner"
        )
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


def test_cage_user_and_room_filters_include_inactive_history(database: Database) -> None:
    active_id = database.create_cage(
        cage_card_id="FILTER-ACTIVE",
        room="Room Beta",
        animal_count=2,
        mouse_user="Alice",
    )
    active_animals = database.list_animals(active_id)
    database.update_animal(active_animals[1]["id"], mouse_user="Bob")
    database.toggle_animal(active_animals[0]["id"])
    inactive_id = database.create_cage(
        cage_card_id="FILTER-INACTIVE",
        status="inactive",
        room="Room Alpha",
        animal_count=1,
        mouse_user="alice",
    )
    blank_id = database.create_cage(
        cage_card_id="FILTER-BLANK",
        room=None,
        animal_count=1,
        mouse_user="   ",
    )

    assert {cage["id"] for cage in database.list_cages(mouse_user=" ALICE ")} == {
        active_id,
        inactive_id,
    }
    assert [cage["id"] for cage in database.list_cages(mouse_user="alice", status="inactive")] == [
        inactive_id
    ]
    assert [cage["id"] for cage in database.list_cages(room=" room beta ")] == [active_id]
    assert [cage["id"] for cage in database.list_cages(search="Bob")] == [active_id]
    assert database.list_cages(mouse_user="   ") == database.list_cages()
    assert database.list_cages(room="   ") == database.list_cages()

    active = database.get_cage(active_id)
    inactive = database.get_cage(inactive_id)
    blank = database.get_cage(blank_id)
    assert active is not None and active["mouse_users"] == ["Alice", "Bob"]
    assert active["mouse_user"] == "Mixed"
    assert inactive is not None and inactive["mouse_users"] == ["alice"]
    assert blank is not None and blank["mouse_users"] == [] and blank["mouse_user"] is None
    assert database.list_mouse_users() == ["Alice", "Bob"]
    assert database.list_rooms() == ["Room Alpha", "Room Beta"]


def test_cage_sorting_is_allowlisted_directional_and_deterministic(database: Database) -> None:
    database.create_cage(cage_card_id="C-THIRD", status="inactive", room="Room B")
    database.create_cage(cage_card_id="A-FIRST", status="active", room="Room C")
    database.create_cage(cage_card_id="B-SECOND", status="on_order", room="Room A")
    database.create_cage(cage_card_id="D-NO-ROOM", status="active", room=None)
    database.create_cage(cage_card_id="E-ROOM-B", status="active", room="Room B")

    def identifiers(**kwargs: str | None) -> list[str]:
        return [cage["cage_card_id"] for cage in database.list_cages(**kwargs)]

    assert identifiers() == ["A-FIRST", "D-NO-ROOM", "E-ROOM-B", "B-SECOND", "C-THIRD"]
    assert identifiers(sort="cage_card_id", direction="asc") == [
        "A-FIRST",
        "B-SECOND",
        "C-THIRD",
        "D-NO-ROOM",
        "E-ROOM-B",
    ]
    assert identifiers(sort="cage_card_id", direction="desc") == [
        "E-ROOM-B",
        "D-NO-ROOM",
        "C-THIRD",
        "B-SECOND",
        "A-FIRST",
    ]
    assert identifiers(sort="room", direction="asc") == [
        "B-SECOND",
        "C-THIRD",
        "E-ROOM-B",
        "A-FIRST",
        "D-NO-ROOM",
    ]
    assert identifiers(sort="room", direction="desc") == [
        "A-FIRST",
        "C-THIRD",
        "E-ROOM-B",
        "B-SECOND",
        "D-NO-ROOM",
    ]
    assert identifiers(sort="status", direction="asc") == [
        "A-FIRST",
        "D-NO-ROOM",
        "E-ROOM-B",
        "B-SECOND",
        "C-THIRD",
    ]
    assert identifiers(sort="status", direction="desc") == [
        "C-THIRD",
        "B-SECOND",
        "A-FIRST",
        "D-NO-ROOM",
        "E-ROOM-B",
    ]

    with pytest.raises(ValueError, match="sort field is invalid"):
        database.list_cages(sort="protocol")
    with pytest.raises(ValueError, match="sort direction is invalid"):
        database.list_cages(sort="room", direction="sideways")


def test_active_home_cages_use_every_letter_before_reusing(database: Database) -> None:
    cage_ids = [database.create_cage(cage_card_id=f"HOME-{index:02d}") for index in range(27)]
    letters = [database.get_cage(cage_id)["family_letter"] for cage_id in cage_ids]  # type: ignore[index]

    assert letters[:26] == list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    assert letters[26] == "A"


def test_batch_update_animals_changes_one_field_for_active_and_inactive_mice(
    database: Database,
) -> None:
    cage_id = database.create_cage(
        cage_card_id="BATCH-TARGET",
        animal_count=3,
        sex="M",
        dob="2026-01-02",
        genotype="Original",
    )
    target_animals = database.list_animals(cage_id)
    inactive_id = int(target_animals[0]["id"])
    database.toggle_animal(inactive_id)
    other_cage_id = database.create_cage(
        cage_card_id="BATCH-OTHER",
        animal_count=1,
        sex="M",
        dob="2025-12-31",
        genotype="Other",
    )
    other_animal_id = int(database.list_animals(other_cage_id)[0]["id"])
    database.connection.execute(
        "UPDATE animals SET updated_at = '2000-01-01 00:00:00' WHERE cage_id = ?",
        (cage_id,),
    )

    assert database.batch_update_animals(cage_id, field="sex", value=" f ") == 3
    after_sex = database.list_animals(cage_id, include_inactive=True)
    assert {animal["sex"] for animal in after_sex} == {"F"}
    assert {animal["dob"] for animal in after_sex} == {"2026-01-02"}
    assert {animal["genotype"] for animal in after_sex} == {"Original"}
    assert {animal["status"] for animal in after_sex} == {"active", "inactive"}
    assert all(animal["updated_at"] != "2000-01-01 00:00:00" for animal in after_sex)

    assert (
        database.batch_update_animals(
            cage_id,
            field="genotype",
            value=" Batch Het ",
        )
        == 3
    )
    after_genotype = database.list_animals(cage_id, include_inactive=True)
    assert {animal["genotype"] for animal in after_genotype} == {"Batch Het"}
    assert {animal["sex"] for animal in after_genotype} == {"F"}

    assert database.batch_update_animals(cage_id, field="dob", value=None) == 3
    after_dob = database.list_animals(cage_id, include_inactive=True)
    assert {animal["dob"] for animal in after_dob} == {None}
    assert {animal["genotype"] for animal in after_dob} == {"Batch Het"}
    assert (
        database.batch_update_animals(
            cage_id,
            field="mouse_user",
            value=" Batch owner ",
        )
        == 3
    )
    after_user = database.list_animals(cage_id, include_inactive=True)
    assert {animal["mouse_user"] for animal in after_user} == {"Batch owner"}
    assert database.batch_update_animals(cage_id, field="mouse_user", value=" ") == 3
    assert {
        animal["mouse_user"] for animal in database.list_animals(cage_id, include_inactive=True)
    } == {None}
    other_animal = database.get_animal(other_animal_id)
    assert other_animal is not None
    assert other_animal["sex"] == "M"
    assert other_animal["dob"] == "2025-12-31"
    assert other_animal["genotype"] == "Other"


def test_batch_update_animals_validates_cage_field_and_value(database: Database) -> None:
    cage_id = database.create_cage(
        cage_card_id="BATCH-VALIDATION",
        animal_count=1,
        sex="M",
        dob="2026-02-03",
        genotype="Keep",
    )
    empty_cage_id = database.create_cage(cage_card_id="BATCH-EMPTY")

    assert database.batch_update_animals(empty_cage_id, field="genotype", value="WT") == 0
    with pytest.raises(ValueError, match="Cage not found"):
        database.batch_update_animals(999_999, field="genotype", value="WT")
    with pytest.raises(
        ValueError,
        match="Mouse field must be sex, genotype, dob, or mouse_user",
    ):
        database.batch_update_animals(cage_id, field="note", value="not allowed")
    with pytest.raises(ValueError, match="Sex must be M, F, or U"):
        database.batch_update_animals(cage_id, field="sex", value="X")
    with pytest.raises(ValueError, match="Sex must be M, F, or U"):
        database.batch_update_animals(cage_id, field="sex", value=None)
    with pytest.raises(ValueError, match="DOB must be a valid date"):
        database.batch_update_animals(cage_id, field="dob", value="not-a-date")
    with pytest.raises(ValueError, match="Mouse user must be 100 characters or fewer"):
        database.batch_update_animals(cage_id, field="mouse_user", value="U" * 101)

    animal = database.list_animals(cage_id)[0]
    assert animal["sex"] == "M"
    assert animal["dob"] == "2026-02-03"
    assert animal["genotype"] == "Keep"


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


def test_existing_surgery_can_be_updated_without_using_another_record(
    database: Database,
) -> None:
    cage_id = database.create_cage(cage_card_id="SURGERY-EDIT", animal_count=1)
    animal_id = int(database.list_animals(cage_id)[0]["id"])
    surgery_ids: list[int] = []
    for index in range(4):
        surgery_id = database.add_surgery(
            animal_id,
            surgery_date=f"2026-05-{index + 1:02d}",
            surgery_time="09:15",
            operator="Original Operator",
            surgery_type="Headplate",
        )
        assert surgery_id is not None
        surgery_ids.append(surgery_id)

    updated = database.update_surgery(
        surgery_ids[0],
        surgery_date="2026-06-07",
        surgery_time="14:05",
        operator=" Revised Operator ",
        surgery_type=" Probe implant ",
    )
    animal = database.get_animal(animal_id)

    assert updated == {
        "id": surgery_ids[0],
        "animal_id": animal_id,
        "cage_id": cage_id,
        "surgery_date": "2026-06-07",
        "surgery_time": "14:05",
        "operator": "Revised Operator",
        "surgery_type": "Probe implant",
    }
    assert animal is not None
    assert len(animal["surgeries"]) == 4
    assert {surgery["id"] for surgery in animal["surgeries"]} == set(surgery_ids)

    with pytest.raises(ValueError, match="Surgery record not found"):
        database.update_surgery(
            999999,
            surgery_date="2026-06-08",
            surgery_time=None,
            operator="Operator",
            surgery_type="Headplate",
        )
    with pytest.raises(ValueError, match="Surgery date is required"):
        database.update_surgery(
            surgery_ids[0],
            surgery_date="",
            surgery_time=None,
            operator="Operator",
            surgery_type="Headplate",
        )


def test_tags_are_shared_and_filter_cages(database: Database) -> None:
    first = database.create_cage(cage_card_id="CC00000008")
    database.create_cage(cage_card_id="CC00000009")
    database.add_tag(first, "experiment-a")

    cages = database.list_cages(tag="experiment-a")
    assert [cage["id"] for cage in cages] == [first]
