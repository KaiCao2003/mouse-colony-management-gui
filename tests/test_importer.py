from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from openpyxl import Workbook

from app.database import Database
from app.importer import seed_if_empty

WORKBOOK_HEADERS = [
    "Cage Card #",
    "mouse#",
    "User",
    "Strain",
    "male",
    "female",
    "DOB",
    "Date surgery #1",
    "surg#1 procedure",
    "Seperated From",
    "Note",
]


def _append_workbook_record(worksheet: object, **values: object) -> None:
    worksheet.append([values.get(header) for header in WORKBOOK_HEADERS])  # type: ignore[attr-defined]


def test_csv_is_authoritative_for_cage_status_and_counts(tmp_path: Path) -> None:
    csv_path = tmp_path / "cages.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "Cage Card ID",
                "Status",
                "Protocol",
                "# Animals",
                "Room",
                "On Census Date",
                "Off Census Date",
            ],
        )
        writer.writeheader()
        writer.writerows(
            [
                {
                    "Cage Card ID": "CC00000001",
                    "Status": "Active",
                    "Protocol": "P-1",
                    "# Animals": "3",
                    "Room": "R-1",
                    "On Census Date": "07/01/2026",
                    "Off Census Date": "",
                },
                {
                    "Cage Card ID": "CC00000002",
                    "Status": "Deactivated",
                    "Protocol": "P-1",
                    "# Animals": "2",
                    "Room": "R-1",
                    "On Census Date": "06/01/2026",
                    "Off Census Date": "7/2/2026 1:34 PM",
                },
            ]
        )

    database = Database(tmp_path / "seed.db")
    database.initialize()
    try:
        report = seed_if_empty(
            database,
            csv_path=csv_path,
            xlsx_path=tmp_path / "missing.xlsx",
        )
        cages = database.list_cages()
        active = next(cage for cage in cages if cage["status"] == "active")
        inactive = next(cage for cage in cages if cage["status"] == "inactive")

        assert report.seeded is True
        assert report.cages == 2
        assert report.animals == 5
        assert active["active_count"] == 3
        assert inactive["active_count"] == 0
        assert inactive["total_count"] == 2
        assert inactive["off_census_date"] == "2026-07-02"
        assert all(
            mouse["status"] == "inactive"
            for mouse in database.list_animals(inactive["id"], include_inactive=True)
        )

        second_report = seed_if_empty(
            database,
            csv_path=csv_path,
            xlsx_path=tmp_path / "missing.xlsx",
        )
        assert second_report.seeded is False
        assert database.count_cages() == 2
    finally:
        database.close()


def test_workbook_matching_lineage_and_enrichment_are_csv_scoped(tmp_path: Path) -> None:
    csv_path = tmp_path / "cages.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "Cage Card ID",
                "Status",
                "Protocol",
                "# Animals",
                "Room",
                "On Census Date",
                "Off Census Date",
            ],
        )
        writer.writeheader()
        for cage_card_id, count in (
            ("CC00000001", 1),
            ("CC00000002", 1),
            ("CC00000003", 2),
            ("CC00000004", 1),
        ):
            writer.writerow(
                {
                    "Cage Card ID": cage_card_id,
                    "Status": "Active",
                    "Protocol": "P-1",
                    "# Animals": str(count),
                    "Room": "R-1",
                    "On Census Date": "7/1/2026",
                    "Off Census Date": "",
                }
            )

    workbook_path = tmp_path / "mice.xlsx"
    workbook = Workbook()
    main = workbook.active
    main.title = "Main"
    main.append(WORKBOOK_HEADERS)
    _append_workbook_record(main, **{"Cage Card #": "CC00000001", "Strain": "wT", "male": 2})
    _append_workbook_record(main, **{"Cage Card #": "CC00000003", "Strain": "WT", "male": 1})

    operator_sheet = workbook.create_sheet("Operator Sheet")
    operator_sheet.append(WORKBOOK_HEADERS)
    _append_workbook_record(
        operator_sheet,
        **{
            "Cage Card #": "CC000000022",
            "mouse#": "Mouse-Typo",
            "User": "OP-1",
            "Strain": "wt",
            "male": 1,
            "DOB": date(2026, 1, 2),
            "Date surgery #1": date(2026, 6, 3),
            "surg#1 procedure": "Probe implant in SC & ADN",
            "Seperated From": "CC000000011",
        },
    )
    _append_workbook_record(
        operator_sheet,
        **{
            "Cage Card #": "CC99999999",
            "mouse#": "Must-Not-Fallback",
            "male": 1,
            "Seperated From": "CC00000004",
        },
    )
    workbook.save(workbook_path)
    workbook.close()

    database = Database(tmp_path / "seed.db")
    database.initialize()
    try:
        report = seed_if_empty(database, csv_path=csv_path, xlsx_path=workbook_path)
        cages = {cage["cage_card_id"]: cage for cage in database.list_cages()}
        source = cages["CC00000001"]
        destination = cages["CC00000002"]
        destination_mouse = database.list_animals(destination["id"], include_inactive=True)[0]

        assert destination["source_cage_card_id"] == "CC00000001"
        assert destination["family_letter"] == source["family_letter"]
        assert destination_mouse["family_letter"] == source["family_letter"]
        assert destination_mouse["public_id"].startswith(f"{source['family_letter']}-")
        assert destination_mouse["legacy_id"] == "Mouse-Typo"
        assert destination_mouse["genotype"] == "WT"
        assert destination_mouse["surgeries"][0]["surgery_type"] == "Probe implant in SC & ADN"

        all_legacy_ids = {
            mouse["legacy_id"]
            for cage in cages.values()
            for mouse in database.list_animals(cage["id"], include_inactive=True)
            if mouse["legacy_id"]
        }
        assert "Must-Not-Fallback" not in all_legacy_ids
        assert any(
            "CC00000001 Excel sex total 2 differs from authoritative CSV count 1" in warning
            for warning in report.warnings
        )
        assert any(
            "CC00000003 Excel sex total 1 differs from authoritative CSV count 2" in warning
            for warning in report.warnings
        )
        assert any(
            "Operator Sheet row 3 has no matching CSV cage" in warning
            for warning in report.warnings
        )
    finally:
        database.close()
