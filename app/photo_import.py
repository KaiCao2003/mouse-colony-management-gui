"""Read colony cage cards with OCR running on the application server."""

from __future__ import annotations

import re
from contextlib import suppress
from datetime import datetime
from threading import Lock
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from app.card_ocr import read_photo_lines

MAX_PHOTO_BYTES = 20 * 1024 * 1024
router = APIRouter()
_ocr_lock = Lock()
_DATE = re.compile(r"\b(\d{1,2}/\d{1,2}/(?:\d{4}|\d{2}))\b")


def _center(line: dict[str, Any]) -> tuple[float, float]:
    points = line["box"]
    return sum(p[0] for p in points) / 4, sum(p[1] for p in points) / 4


def parse_card_lines(lines: list[dict[str, Any]]) -> dict[str, Any]:
    cage_ids = set()
    for item in lines:
        cage_ids.update(re.findall(r"\bCC\d{8}\b", item["text"].upper()))
    result: dict[str, Any] = {
        "cage_card_id": next(iter(cage_ids)) if len(cage_ids) == 1 else None,
        "line": None,
        "rows": [],
    }
    for item in lines:
        if re.search(r"\bline\s+\d+\b", item["text"], re.IGNORECASE):
            y = _center(item)[1]
            height = max(p[1] for p in item["box"]) - min(p[1] for p in item["box"])
            result["line"] = " ".join(
                part["text"]
                for part in sorted(lines, key=lambda part: _center(part)[0])
                if abs(_center(part)[1] - y) < height / 2
            )
            break
    id_headers = [item for item in lines if item["text"].strip().upper() == "ID"]
    genotype_headers = [item for item in lines if item["text"].strip().lower() == "genotype"]
    if len(id_headers) != 1 or len(genotype_headers) != 1:
        return result
    id_header, genotype_header = id_headers[0], genotype_headers[0]
    header_x, header_y = _center(id_header)
    header_height = max(p[1] for p in id_header["box"]) - min(p[1] for p in id_header["box"])
    genotype_x = min(p[0] for p in genotype_header["box"])
    ids = sorted(
        (
            item
            for item in lines
            if re.fullmatch(r"\d{3,10}", item["text"].strip())
            and item["confidence"] >= 0.8
            and abs(min(p[0] for p in item["box"]) - header_x) < header_height * 2
            and _center(item)[1] > header_y + header_height / 2
        ),
        key=lambda item: _center(item)[1],
    )
    for index, item in enumerate(ids):
        x, y = _center(item)
        height = max(p[1] for p in item["box"]) - min(p[1] for p in item["box"])
        neighboring_gaps = [
            abs(_center(ids[other])[1] - y)
            for other in (index - 1, index + 1)
            if 0 <= other < len(ids)
        ]
        tolerance = min(neighboring_gaps) * 0.48 if neighboring_gaps else height * 0.65
        cells = sorted(
            (
                part
                for part in lines
                if _center(part)[0] > x and abs(_center(part)[1] - y) <= tolerance
            ),
            key=lambda part: _center(part)[0],
        )
        metadata = " ".join(part["text"] for part in cells if _center(part)[0] < genotype_x)
        sex_match = re.search(r"(?:^|\s)([MF])(?=\s|\d|$)", metadata.upper())
        dob = None
        date_match = _DATE.search(metadata)
        if date_match:
            value = date_match[1]
            with suppress(ValueError):
                dob = (
                    datetime.strptime(
                        value, "%m/%d/%Y" if len(value.split("/")[-1]) == 4 else "%m/%d/%y"
                    )
                    .date()
                    .isoformat()
                )
        genotype = " ".join(
            part["text"]
            for part in cells
            if min(p[0] for p in part["box"]) >= genotype_x - header_height / 4
        ).strip()
        genotype = {"十": "+", "一": "-", "−": "-", "—": "-", "–": "-"}.get(genotype, genotype)
        result["rows"].append(
            {
                "mouse_id": item["text"].strip(),
                "sex": sex_match[1] if sex_match else None,
                "dob": dob,
                "genotype": genotype or None,
            }
        )
    return result


def recognize_photo(payload: bytes) -> dict[str, Any]:
    return parse_card_lines(read_photo_lines(payload))


@router.post("/photos/recognize")
def read_cage_photo(
    request: Request,
    photo_file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    if not _ocr_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=429, detail="Another photo is being read. Try again shortly."
        )
    try:
        payload = photo_file.file.read(MAX_PHOTO_BYTES + 1)
        if len(payload) > MAX_PHOTO_BYTES:
            raise HTTPException(status_code=413, detail="Photo must be at most 20 MB.")
        try:
            result = recognize_photo(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (ImportError, RuntimeError) as exc:
            raise HTTPException(
                status_code=503, detail="Photo recognition is unavailable."
            ) from exc
    finally:
        _ocr_lock.release()
    result["cage_id"] = None
    if result["cage_card_id"]:
        database = request.app.state.database
        matches = [
            cage
            for cage in database.list_cages(search=result["cage_card_id"])
            if cage["cage_card_id"].casefold() == result["cage_card_id"].casefold()
        ]
        if len(matches) == 1:
            result["cage_id"] = matches[0]["id"]
    return result
