from __future__ import annotations

import csv
import io

import pytest

import app.reconciliation as reconciliation
from app.reconciliation import AopsCsvValidationError, parse_aops_cage_cards

AOPS_HEADERS = [
    "Cage Card ID",
    "Crash Number",
    "Status",
    "Protocol",
    "PI",
    "Protocol Group",
    "# Animals",
    "Room",
    "On Census Date",
    "Off Census Date",
]


def _csv_bytes(
    *rows: list[str],
    headers: list[str] | None = None,
    bom: bool = False,
) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(headers or AOPS_HEADERS)
    writer.writerows(rows)
    payload = output.getvalue().encode()
    return b"\xef\xbb\xbf" + payload if bom else payload


def _row(
    cage_card_id: str,
    status: str = "Active",
    animal_count: str = "2",
    on_census_date: str = "8/3/2026",
    off_census_date: str = "",
) -> list[str]:
    return [
        cage_card_id,
        "",
        status,
        "PROTO-1",
        "Example, PI",
        "Group-1",
        animal_count,
        "ROOM-1",
        on_census_date,
        off_census_date,
    ]


def test_parse_normalizes_supplied_aops_schema_and_all_statuses() -> None:
    payload = _csv_bytes(
        _row("cc00000001"),
        _row("CC00000002", "Deactivated", "1", "7/1/2026", "8/4/2026 9:50 AM"),
        _row("CC00000003", "Inactive", "0", "2026-07-01", "2026-08-04 09:50:00"),
        _row("CC00000004", "On Order", "3", "", ""),
        bom=True,
    )

    parsed = parse_aops_cage_cards(payload)

    assert parsed.headers == tuple(AOPS_HEADERS)
    assert [record.status for record in parsed.records] == [
        "active",
        "inactive",
        "inactive",
        "on_order",
    ]
    first = parsed.by_cage_card_id["CC00000001"]
    second = parsed.by_cage_card_id["CC00000002"]
    assert first.cage_card_id == "CC00000001"
    assert first.animal_count == 2
    assert first.room == "ROOM-1"
    assert first.protocol == "PROTO-1"
    assert first.on_census_date == "2026-08-03"
    assert first.off_census_date is None
    assert second.off_census_date == "2026-08-04"


def test_parse_accepts_declared_extra_columns_without_retaining_them() -> None:
    headers = [*AOPS_HEADERS, "Future AOPS Field"]
    payload = _csv_bytes([*_row("CC00000001"), "future value"], headers=headers)

    parsed = parse_aops_cage_cards(payload)

    assert parsed.headers == tuple(headers)
    assert not hasattr(parsed.records[0], "Future AOPS Field")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("Status", "Archived", "Status must be"),
        ("# Animals", "1.5", "whole number"),
        ("# Animals", "1001", "whole number"),
        ("On Census Date", "02/30/2026", "unsupported or invalid date"),
        ("Off Census Date", "tomorrow", "unsupported or invalid date"),
    ],
)
def test_parse_rejects_invalid_canonical_values(field: str, value: str, message: str) -> None:
    row = _row("CC00000001")
    row[AOPS_HEADERS.index(field)] = value

    with pytest.raises(AopsCsvValidationError, match=message):
        parse_aops_cage_cards(_csv_bytes(row))


def test_parse_rejects_nonstandard_and_duplicate_cage_ids_case_insensitively() -> None:
    with pytest.raises(AopsCsvValidationError, match="form CC followed by 8 digits"):
        parse_aops_cage_cards(_csv_bytes(_row("CAGE-1")))

    with pytest.raises(AopsCsvValidationError, match="duplicate Cage Card ID CC00000001"):
        parse_aops_cage_cards(_csv_bytes(_row("CC00000001"), _row("cc00000001")))


def test_parse_rejects_missing_or_duplicate_headers() -> None:
    missing = [header for header in AOPS_HEADERS if header != "Room"]
    missing_row = [value for index, value in enumerate(_row("CC00000001")) if index != 7]
    with pytest.raises(AopsCsvValidationError, match="missing required columns: Room"):
        parse_aops_cage_cards(_csv_bytes(missing_row, headers=missing))

    duplicate = [*AOPS_HEADERS, "Status"]
    with pytest.raises(AopsCsvValidationError, match="duplicate columns: Status"):
        parse_aops_cage_cards(_csv_bytes([*_row("CC00000001"), "Active"], headers=duplicate))


def test_parse_rejects_malformed_rows_and_reversed_dates() -> None:
    malformed = _csv_bytes(_row("CC00000001")[:-1])
    with pytest.raises(AopsCsvValidationError, match="expected 10 columns but found 9"):
        parse_aops_cage_cards(malformed)

    reversed_dates = _row("CC00000001", on_census_date="8/4/2026")
    reversed_dates[-1] = "8/3/2026 9:50 AM"
    with pytest.raises(AopsCsvValidationError, match="cannot be earlier"):
        parse_aops_cage_cards(_csv_bytes(reversed_dates))


def test_parse_rejects_empty_non_utf8_null_and_oversized_uploads() -> None:
    with pytest.raises(AopsCsvValidationError, match="empty"):
        parse_aops_cage_cards(b"")
    with pytest.raises(AopsCsvValidationError, match="UTF-8"):
        parse_aops_cage_cards(b"\xff\xfe")
    with pytest.raises(AopsCsvValidationError, match="null byte"):
        parse_aops_cage_cards(b"Cage Card ID\x00")
    with pytest.raises(AopsCsvValidationError, match="exceeds"):
        parse_aops_cage_cards(b"x" * (reconciliation.MAX_AOPS_CSV_BYTES + 1))


def test_parse_enforces_record_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reconciliation, "MAX_AOPS_CSV_ROWS", 1)

    with pytest.raises(AopsCsvValidationError, match="more than 1 cage records"):
        parse_aops_cage_cards(_csv_bytes(_row("CC00000001"), _row("CC00000002")))


def test_parse_rejects_header_only_and_blank_records() -> None:
    with pytest.raises(AopsCsvValidationError, match="contains no cage records"):
        parse_aops_cage_cards(_csv_bytes())

    with pytest.raises(AopsCsvValidationError, match="blank records are not allowed"):
        parse_aops_cage_cards(_csv_bytes([""] * len(AOPS_HEADERS)))
