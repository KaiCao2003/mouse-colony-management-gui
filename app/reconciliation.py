"""Strict, side-effect-free parsing for official AOPS cage-card exports."""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

MAX_AOPS_CSV_BYTES: Final = 2 * 1024 * 1024
MAX_AOPS_CSV_ROWS: Final = 10_000

REQUIRED_AOPS_HEADERS: Final[tuple[str, ...]] = (
    "Cage Card ID",
    "Status",
    "# Animals",
    "Room",
    "Protocol",
    "On Census Date",
    "Off Census Date",
)

_CAGE_CARD_ID = re.compile(r"^CC\d{8}$", flags=re.IGNORECASE)
_NONNEGATIVE_INTEGER = re.compile(r"^\d+$")
_DATE_FORMATS: Final[tuple[str, ...]] = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m/%d/%Y %I:%M %p",
    "%Y-%m-%d %H:%M:%S",
)

type CageStatus = Literal["active", "inactive", "on_order"]


class AopsCsvValidationError(ValueError):
    """Raised when an uploaded AOPS CSV cannot be reconciled safely."""


@dataclass(frozen=True, slots=True)
class AopsCageCard:
    """One normalized official cage-card record."""

    source_row: int
    cage_card_id: str
    status: CageStatus
    animal_count: int
    room: str | None
    protocol: str | None
    on_census_date: str | None
    off_census_date: str | None


@dataclass(frozen=True, slots=True)
class AopsCageCards:
    """A fully validated AOPS export ready for read-only comparison."""

    headers: tuple[str, ...]
    records: tuple[AopsCageCard, ...]

    @property
    def by_cage_card_id(self) -> dict[str, AopsCageCard]:
        """Return a fresh identifier mapping so callers cannot mutate the parsed result."""

        return {record.cage_card_id: record for record in self.records}


def _error(row_number: int, message: str) -> AopsCsvValidationError:
    return AopsCsvValidationError(f"CSV row {row_number}: {message}")


def _optional_text(value: str) -> str | None:
    cleaned = value.strip()
    return cleaned or None


def _parse_status(value: str, row_number: int) -> CageStatus:
    normalized = " ".join(value.strip().casefold().split())
    statuses: dict[str, CageStatus] = {
        "active": "active",
        "deactivated": "inactive",
        "inactive": "inactive",
        "on order": "on_order",
    }
    try:
        return statuses[normalized]
    except KeyError as exc:
        raise _error(
            row_number,
            "Status must be Active, Deactivated, Inactive, or On Order.",
        ) from exc


def _parse_animal_count(value: str, row_number: int) -> int:
    cleaned = value.strip()
    if not _NONNEGATIVE_INTEGER.fullmatch(cleaned):
        raise _error(row_number, "# Animals must be a whole number from 0 to 1000.")
    count = int(cleaned)
    if count > 1000:
        raise _error(row_number, "# Animals must be a whole number from 0 to 1000.")
    return count


def _parse_date(value: str, field: str, row_number: int) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, pattern).date().isoformat()
        except ValueError:
            continue
    raise _error(row_number, f"{field} has an unsupported or invalid date.")


def _parse_record(
    values: list[str],
    indexes: dict[str, int],
    row_number: int,
) -> AopsCageCard:
    cage_card_id = values[indexes["Cage Card ID"]].strip().upper()
    if not _CAGE_CARD_ID.fullmatch(cage_card_id):
        raise _error(row_number, "Cage Card ID must use the form CC followed by 8 digits.")

    on_census_date = _parse_date(
        values[indexes["On Census Date"]],
        "On Census Date",
        row_number,
    )
    off_census_date = _parse_date(
        values[indexes["Off Census Date"]],
        "Off Census Date",
        row_number,
    )
    if on_census_date and off_census_date and off_census_date < on_census_date:
        raise _error(row_number, "Off Census Date cannot be earlier than On Census Date.")

    return AopsCageCard(
        source_row=row_number,
        cage_card_id=cage_card_id,
        status=_parse_status(values[indexes["Status"]], row_number),
        animal_count=_parse_animal_count(values[indexes["# Animals"]], row_number),
        room=_optional_text(values[indexes["Room"]]),
        protocol=_optional_text(values[indexes["Protocol"]]),
        on_census_date=on_census_date,
        off_census_date=off_census_date,
    )


def parse_aops_cage_cards(payload: bytes) -> AopsCageCards:
    """Validate and normalize an uploaded official AOPS cage-card CSV.

    Parsing is all-or-nothing and does not open files or mutate database state.
    Additional export columns are accepted but deliberately not retained.
    """

    if not payload:
        raise AopsCsvValidationError("The uploaded CSV is empty.")
    if len(payload) > MAX_AOPS_CSV_BYTES:
        raise AopsCsvValidationError(
            f"The uploaded CSV exceeds the {MAX_AOPS_CSV_BYTES}-byte limit."
        )
    if b"\x00" in payload:
        raise AopsCsvValidationError("The uploaded CSV contains a null byte.")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AopsCsvValidationError("The uploaded CSV must use UTF-8 encoding.") from exc

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        headers = tuple(next(reader))
    except StopIteration as exc:
        raise AopsCsvValidationError("The uploaded CSV is empty.") from exc
    except csv.Error as exc:
        raise AopsCsvValidationError(f"The CSV header is malformed: {exc}.") from exc

    duplicate_headers = sorted(name for name, count in Counter(headers).items() if count > 1)
    if duplicate_headers:
        raise AopsCsvValidationError(
            f"The CSV header contains duplicate columns: {', '.join(duplicate_headers)}."
        )
    if any(not header for header in headers):
        raise AopsCsvValidationError("The CSV header contains an unnamed column.")
    missing = [header for header in REQUIRED_AOPS_HEADERS if header not in headers]
    if missing:
        raise AopsCsvValidationError(f"The CSV is missing required columns: {', '.join(missing)}.")

    indexes = {header: index for index, header in enumerate(headers)}
    records: list[AopsCageCard] = []
    first_rows: dict[str, int] = {}
    try:
        for values in reader:
            row_number = reader.line_num
            if len(records) >= MAX_AOPS_CSV_ROWS:
                raise AopsCsvValidationError(
                    f"The CSV contains more than {MAX_AOPS_CSV_ROWS} cage records."
                )
            if len(values) != len(headers):
                raise _error(
                    row_number,
                    f"expected {len(headers)} columns but found {len(values)}.",
                )
            if not any(value.strip() for value in values):
                raise _error(row_number, "blank records are not allowed.")

            record = _parse_record(values, indexes, row_number)
            previous_row = first_rows.get(record.cage_card_id)
            if previous_row is not None:
                raise _error(
                    row_number,
                    f"duplicate Cage Card ID {record.cage_card_id} (first seen on row "
                    f"{previous_row}).",
                )
            first_rows[record.cage_card_id] = row_number
            records.append(record)
    except csv.Error as exc:
        raise AopsCsvValidationError(
            f"CSV row {reader.line_num}: malformed CSV data: {exc}."
        ) from exc

    if not records:
        raise AopsCsvValidationError("The CSV contains no cage records.")
    return AopsCageCards(headers=headers, records=tuple(records))
